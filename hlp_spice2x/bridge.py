# hlp-spice2x (spice2x to GP2040-CE Host Lighting bridge)
# Filename: bridge.py
# Folder: hlp_spice2x
# https://github.com/djGLiTCH/hlp-spice2x
# SPDX-FileCopyrightText: (C) 2026 Jacob Simpson (DJ GLiTCH)
# SPDX-License-Identifier: GPL-3.0-or-later
# See LICENSES/GPL-3.0-or-later.txt at the repository root.

"""The poll, resolve and publish loop.

Reads the game's lights from spice2x, turns them into a frame through the
profile, and publishes that frame to the board. Frames are only sent when
something changed. While nothing does, the claim on the board's lights is
renewed several times per keepalive timeout, which holds the takeover without
republishing an identical frame.

The host flow is gp2040ce-hostlighting's Session: the claim, the events, the
configuration pages, the frames and the reconnect. This module resolves the
profile against the board and decides what to stage.
"""
import logging
import time

from gp2040ce_hostlighting.claim import ClaimState
from gp2040ce_hostlighting.frames import Frame, publish_frame
from gp2040ce_hostlighting.protocol import (ADDRESSING_ACTION, ADDRESSING_KIND, ADDRESSING_LED_RUN, ADDRESSING_LIGHT,
                                            PAGE_CONTROLS, PAGE_IDENTITY, PAGE_LIGHTS, PAGE_SUMMARY, PIXEL_RGB,
                                            PIXEL_RGBW, TAKEOVER_OVERLAY, TAKEOVER_WHOLE_FRAME,
                                            HostLightingClaimedError, HostLightingDisconnectedError,
                                            HostLightingError, HostLightingIncompatibleError, HostLightingMagicError,
                                            HostLightingRejectedError, HostLightingUnansweredError, Settings,
                                            subtractive_white)
from gp2040ce_hostlighting.session import Session

from . import hlp, profile as profile_module

# the rate when none was asked for and the board states none
DEFAULT_FPS = 60.0
# The highest rate streamed unless --fps asks for more. hlp-bench measured 250
# frames/s on a 46-LED board at three reports per frame, and half of that leaves
# room for a longer frame, a slow poll or a retry.
MAX_STREAM_FPS = 120.0
# the name page 1 shows other applications while the bridge holds the lights
CLAIM_NAME = 'hlp-spice2x'
# how often a board that left the bus is looked for
RECONNECT_SECONDS = 1.0
# The order a frame's entries are staged in. Entries apply in order, so where
# two reach the same LED the later one wins. A kind of light goes first, then the
# controls by name, then single lights. Raw LED runs go after all of them.
ENTRY_PASSES = (ADDRESSING_KIND, ADDRESSING_ACTION, ADDRESSING_LIGHT)
# the pages whose change re-resolves the profile
RESOLVED_FROM = frozenset({PAGE_SUMMARY, PAGE_CONTROLS, PAGE_LIGHTS})


class _ReportLines(logging.Handler):
    """Pass the library's INFO and WARNING lines to the bridge's report callable."""

    def __init__(self, report):
        """Hold the callable each line goes to."""
        super().__init__(logging.INFO)
        self.report = report

    def emit(self, record: logging.LogRecord) -> None:
        """Report one line, a warning marked as one, and mark the record reported."""
        self.report(("warning: " if record.levelno >= logging.WARNING else '') + record.getMessage())
        record.reported = True


def not_reported(record: logging.LogRecord) -> bool:
    """Leave the lines the bridge has reported out of the debug log, so that -d shows each once."""
    return not getattr(record, 'reported', False)


def frame_for(frame, staging) -> Frame:
    """Turn a resolved frame into the library's Frame, in the staging order the bridge keeps.

    The passes are emitted in a fixed order that must be kept. They all write
    into one staged frame, where the last write to an LED wins. A kind of light
    and a control name reach every light carrying them. A target naming one
    light or a raw LED run is staged after them, so it wins.

    :param frame: a resolved frame
    :param staging: how each target reaches the board's lights, from staging_for
    :return: the Frame to publish
    """
    entries, runs = {}, []
    for target, colour in frame.items():
        # Which light an index names is known only against a board. A target that
        # reached here unresolved is a wiring mistake, and staging is sent without
        # replies, so it is raised here.
        if target not in staging:
            raise ValueError(f"target {target} was never resolved against a board")
        if staging[target] is None:
            continue  # held back, since the board cannot honour it as it stands
        operation, white = staging[target]
        pixel_format = PIXEL_RGBW if white else PIXEL_RGB
        pixel = subtractive_white(colour) if white else tuple(colour[:3])
        if operation[0] == 'run':
            runs.append((operation[1], operation[2], pixel, pixel_format))
        elif operation[0] == 'entry':
            entries.setdefault((operation[1], pixel_format), []).append((operation[2], pixel))
        else:
            raise ValueError(f"staging carries an unknown operation: {operation[0]!r}")
    result = Frame()
    for addressing in ENTRY_PASSES:
        for pixel_format in (PIXEL_RGB, PIXEL_RGBW):
            if entries.get((addressing, pixel_format)):
                result.entries(addressing, entries[(addressing, pixel_format)], pixel_format)
    for first, count, pixel, pixel_format in runs:
        result.run(ADDRESSING_LED_RUN, first, [pixel] * count, pixel_format)
    return result


def send_frame(device, frame, staging, clear_first=False):
    """Stage a resolved frame and publish it, outside a Session.

    :param device: an opened HostLightingDevice
    :param frame: a resolved frame
    :param staging: how each target reaches the board's lights, from staging_for
    :param clear_first: whether to clear the staged frame before staging this one
    :return: the library's FrameResult
    :raises HostLightingRejectedError: if the board refuses the frame
    """
    return publish_frame(device, frame_for(frame, staging), clear_first=clear_first)


def addressing_used(profile) -> set:
    """List the addressing modes a profile's targets are staged by.

    :param profile: a loaded profile
    :return: the ADDRESSING_* modes the board has to offer for it
    """
    modes = set()
    for target, _ in profile.values():
        if target[0] == 'button':
            modes.add(target[1][0])
        elif target[0] == 'light':
            modes.add(ADDRESSING_LIGHT)
        else:
            modes.add(ADDRESSING_LED_RUN)
    return modes


def validate_targets(profile, caps) -> tuple:
    """Report profile controls that this board cannot light.

    Staging is sent without replies in the main loop, so a control the board
    cannot resolve is skipped without a message. Each one is checked once at
    startup and reported.

    The Lights page says what STAGE will reach, and the Controls page splits
    the rest. A control it leaves out is not on the board, and one it lists is
    wired but has no LED.

    :param profile: a loaded profile
    :param caps: the board's capabilities
    :return: (controls the board has no LED for, controls the board does not have)
    """
    controls = sorted({target[1] for target, _ in profile.values() if target[0] == 'button'})
    unlit = [control for control in controls if not caps.lights_of(control)]
    absent = [control for control in unlit if caps.lacks_control(control)]
    return ([hlp.control_name(control) for control in unlit if control not in absent],
            [hlp.control_name(control) for control in absent])


def light_operation(caps, control, index) -> tuple:
    """Resolve one per-light profile target against the board's Lights page.

    :param caps: the board's capabilities
    :param control: the control being indexed into
    :param index: which of that control's lights, in the order the page lists them
    :return: the staging operation that colours that light
    :raises profile_module.ProfileError: if this board cannot offer that light
    """
    name = hlp.control_name(control)
    records = caps.lights_of(control)
    if index >= len(records):
        raise profile_module.ProfileError(
            f"{name}[{index}] is out of range: this board gives {name} {len(records)} "
            f"light{'' if len(records) == 1 else 's'}")
    return ('entry', ADDRESSING_LIGHT, records[index].ordinal)


def staging_for(profile, caps, problems=None) -> dict:
    """Work out how each of the profile's targets reaches the board's lights.

    Built at connect and again whenever the board says its configuration
    changed, never per frame.

    At startup a profile the board cannot honour raises. A caller re-resolving
    after a configuration change passes `problems`, and the entries the board
    cannot honour are held back and described there.

    :param profile: a loaded profile
    :param caps: the board's capabilities
    :param problems: if given, entries the board cannot honour are described into
        this list and staged as nothing
    :return: mapping of target to (staging operation, whether to send white),
        or to None for an entry held back
    :raises profile_module.ProfileError: if the board cannot honour the profile
        and no `problems` list was given
    """
    staging = {}
    # Whether a target carries white is decided per target. Two entries may share
    # a target, and resolve_frame merges them into the wider of the two colours,
    # so a per-entry decision would follow whichever entry the profile lists last.
    wide = {target for target, colour in profile.values() if len(colour) > 3}
    for target, _ in profile.values():
        # a white component only survives where the chain has an emitter for it
        white = target in wide and caps.host_white
        if target[0] == 'light':
            try:
                staging[target] = (light_operation(caps, target[1], target[2]), white)
            except profile_module.ProfileError as error:
                if problems is None:
                    raise
                problems.append(str(error))
                staging[target] = None
        elif target[0] == 'range':
            # An LED run reaching past the addressable limit is an invalid
            # argument that stages none of it, and it is sent without a reply.
            # The run is held to the limit here.
            count = min(target[2], caps.led_limit - target[1])
            staging[target] = (('run', target[1], count), white) if count > 0 else None
        else:
            staging[target] = (('entry',) + tuple(target[1]), white)
    return staging


def white_warnings(profile, caps) -> list:
    """Say, once at startup, where a profile's white component cannot be used.

    :param profile: a loaded profile
    :param caps: the board's capabilities
    :return: warnings to report, empty when the profile asks for no white
    """
    carrying = [target for target, colour in profile.values() if len(colour) > 3]
    if not carrying or caps.host_white:
        return []
    asking = ('1 profile entry asks' if len(carrying) == 1
              else f"{len(carrying)} profile entries ask")
    if not caps.white_channel:
        chain = hlp.LED_FORMAT_NAMES.get(caps.summary.colour_format if caps.summary else None, 'unknown')
        return [f"{asking} for a white component, but this "
                f"board's LED chain is {chain} and has no white emitter to drive it, so the "
                f"white is dropped and the colour is sent as RGB"]
    return [f"{asking} for a white component, but this board's firmware does not list the "
            f"RGBW pixel format, so the white is dropped and the colour is sent as RGB"]


def ranges_past_the_board(profile, caps) -> list:
    """Find raw ranges that reach past the last LED this board has.

    The load-time check bounds a range by what the wire can carry, which is all
    that can be known without a board. The board reports its own extent, and
    LEDs past it can be staged but are never shown.

    These are reported as a warning and the rest of each range is staged.

    :param profile: a loaded profile
    :param caps: the board's capabilities
    :return: the (start, count) pairs that reach past the board, in order
    """
    return sorted({(target[1], target[2]) for target, _ in profile.values()
                   if target[0] == 'range' and target[1] + target[2] > caps.led_extent})


def restage(profile, caps, report) -> dict:
    """Re-resolve a profile against a board whose configuration has changed.

    The entries that no longer fit the board are held back and reported once.
    The rest of the profile carries on.

    :param profile: a loaded profile
    :param caps: the board's capabilities, already refreshed
    :param report: callable used for progress output
    :return: the staging to use from here on
    """
    problems = []
    restaged = staging_for(profile, caps, problems)
    for problem in problems:
        report(f"warning: after the board's configuration changed, {problem}")
    return restaged


def resolve_stream_rate(fps, caps) -> tuple:
    """Decide how fast to stream, and why.

    A board that renders slower than the cap is matched, since frames it never
    renders use the USB bus for nothing.

    A board that renders faster is held to the cap, which leaves room for a
    slow poll or a retry.

    :param fps: the rate asked for, or None if nobody asked
    :param caps: the board's capabilities
    :return: (the rate to stream at, why it is that rate)
    """
    if fps is not None:
        return fps, 'asked for with --fps'
    if not caps.connected:
        return DEFAULT_FPS, 'no board to ask'
    rate = caps.summary.stream_rate(MAX_STREAM_FPS) if caps.summary is not None else None
    if rate is None:
        return DEFAULT_FPS, 'the board did not state a render rate'
    if caps.render_hz > MAX_STREAM_FPS:
        return rate, f"capped below the board's {caps.render_hz} Hz"
    return rate, "matching the board's render rate"


def frame_stats(frames: int, misses: int, started: float) -> str:
    """Summarise a run as frames published, rate and unacknowledged frames."""
    elapsed = max(time.monotonic() - started, 1e-6)
    return (f"{frames} frames in {elapsed:.1f}s ({frames / elapsed:.1f}/s published), "
            f"{misses} frames unacknowledged")


def start_board(session, profile, report, returning=False) -> tuple:
    """Report a started session's board, and resolve the profile against it.

    :param session: the started Session
    :param profile: a loaded profile
    :param report: callable used for progress output
    :param returning: the board left the bus mid-run and is back, so an entry
        the board cannot honour is held back
    :return: (the board's capabilities, the staging)
    :raises profile_module.ProfileError: if the board itself cannot honour the profile
    """
    caps = hlp.HostLightingCapabilities(session.hello, session.cache)
    label, firmware = (hlp.label_and_firmware(session.device) if PAGE_IDENTITY in session.hello.pages
                       else ('?', '?'))
    report(f"board: {label} ({firmware})")
    report(caps.summary_line())
    applied = session.claim.applied
    if session.claim.state is ClaimState.HELD and applied is not None:
        report(f"takeover: {'overlay' if applied.takeover == TAKEOVER_OVERLAY else 'whole frame'}, "
               f"{applied.keepalive_ms} ms keepalive, "
               f"{'applying' if applied.brightness_applied else 'ignoring'} board brightness")
    unmapped, absent = validate_targets(profile, caps)
    if absent:
        report(f"warning: {len(absent)} mapped control(s) are not on this board: "
               f"{', '.join(absent)}")
    if unmapped:
        report(f"warning: {len(unmapped)} mapped control(s) have no LED on this "
               f"board: {', '.join(unmapped)}")
    problems = [] if returning else None
    staging = staging_for(profile, caps, problems)
    for problem in problems or []:
        report(f"warning: {problem}")
    over = ranges_past_the_board(profile, caps)
    if over:
        report(f"warning: {len(over)} raw range(s) reach past this board's "
               f"{caps.led_extent} LEDs and light nothing beyond it: "
               + ', '.join(f"[{start}, {count}]" for start, count in over))
    for warning in white_warnings(profile, caps):
        report(f"warning: {warning}")
    several = sorted(hlp.control_name(target[1]) for target in staging
                     if target[0] == 'button' and target[1][0] == ADDRESSING_ACTION
                     and len(caps.lights_of(target[1])) > 1)
    if several:
        report(f"controls the board gives more than one light, all of which will be "
               f"lit: {', '.join(several)}")
    return caps, staging


def lost_line(error) -> str:
    """Say how a board was lost: it stopped answering, or it left the bus.

    :param error: the HostLightingDisconnectedError that ended the session
    :return: the line to report
    """
    if isinstance(error, HostLightingUnansweredError):
        return "board stopped answering"
    return f"board disconnected ({error})"


def back(session) -> bool:
    """Look for a board that left the bus, and start the session on it again.

    :param session: the Session whose board left
    :return: whether the board is back
    :raises HostLightingIncompatibleError: if the board returned with firmware the bridge cannot drive
    """
    try:
        return session.reconnect()
    except HostLightingIncompatibleError:
        raise
    except (HostLightingError, OSError):
        return False    # not answering yet, gone again, or the bus could not be enumerated


def run(connection, profile, device=None, fps=None, timeout_ms=2000,
        overlay=False, full_brightness=False, force=False, report=print, reopen=None):
    """Poll spice2x and stream frames until interrupted or the game closes.

    :param connection: an open SpiceConnection
    :param profile: a loaded profile
    :param device: an opened HostLightingDevice, or None for a dry run. The run closes it
    :param fps: poll rate, or None to resolve one after the handshake
    :param timeout_ms: keepalive timeout sent in CONFIGURE
    :param overlay: overlay onto the board's animations instead of taking the whole frame
    :param full_brightness: ignore the board's brightness setting
    :param force: take the board's lights over from an application that holds them
    :param report: callable used for progress output
    :param reopen: callable taking a board ID and returning that board opened
        again, or None while it is off the bus. Without one a disconnect ends the run.
    :raises HostLightingIncompatibleError: if the board's firmware cannot honour the profile
    :raises profile_module.ProfileError: if the board itself cannot honour the profile
    """
    frames = misses = 0
    started = time.monotonic()
    caps = hlp.HostLightingCapabilities.absent()
    staging = {}
    session = None
    library = logging.getLogger('gp2040ce_hostlighting')
    lines, level = _ReportLines(report), library.level
    library.addHandler(lines)
    # INFO for the lines reported, or DEBUG when -d asked for it
    library.setLevel(min(logging.INFO, library.getEffectiveLevel()))

    try:
        if device is not None:
            settings = Settings(takeover=TAKEOVER_OVERLAY if overlay else TAKEOVER_WHOLE_FRAME,
                                keepalive_ms=timeout_ms, brightness_applied=not full_brightness)
            # a holder at the start is an error, and one that takes the lights over mid-run is waited out
            session = Session(device, CLAIM_NAME, settings, force=force,
                              require=hlp.requirements(addressing_used(profile)),
                              pages=hlp.CACHE_PAGES, masks=hlp.CACHE_MASKS,
                              reopen=reopen or (lambda board_id: None))
            try:
                session.start()
            except HostLightingMagicError:
                raise HostLightingIncompatibleError(hlp.NOT_HOST_LIGHTING) from None
            except HostLightingClaimedError as error:
                raise HostLightingError(
                    f"'{error.holder.application_name}' holds this board's lights. Close it, or pass "
                    f"--force to take the lights over from it") from None
            caps, staging = start_board(session, profile, report)

        # resolved after the handshake, since the board's render rate is unknown before it
        fps, why = resolve_stream_rate(fps, caps)
        report(f"streaming at {fps:g} fps ({why})")
        period = 1.0 / fps
        last_frame = None
        reported_unmapped = False
        complaints = set()
        away = False
        started = next_tick = retry_due = time.monotonic()

        while True:
            states = connection.lights_read()
            if not reported_unmapped:
                missing = sorted(set(states) - set(profile))
                if missing:
                    report(f"{len(missing)} light(s) not in the profile, ignoring: "
                           f"{', '.join(missing[:6])}{' ...' if len(missing) > 6 else ''}")
                reported_unmapped = True

            frame = profile_module.resolve_frame(states, profile)
            now = time.monotonic()

            try:
                if away:
                    if now >= retry_due:
                        retry_due = now + RECONNECT_SECONDS
                        if back(session):
                            caps, staging = start_board(session, profile, report, returning=True)
                            away = False
                            last_frame = None
                elif session is not None:
                    # Housekeeping runs on wall-clock time, so a static game screen,
                    # which produces no new frames, still keeps the claim and still
                    # notices the board changing.
                    try:
                        update = session.poll()
                    except HostLightingRejectedError as error:
                        if str(error) not in complaints:
                            # reported once, since the request is repeated every tick it keeps failing
                            complaints.add(str(error))
                            report(f"warning: the board's state could not be read ({error}). "
                                   f"The run carries on")
                    else:
                        if update.pages_read & RESOLVED_FROM:
                            # A light's ordinal is what an index addresses, so the
                            # profile is resolved again before anything more is staged.
                            staging = restage(profile, caps, report)
                            ranges = any(target[0] == 'range' for target, _ in profile.values())
                            report("board configuration changed, controls re-resolved"
                                   + (". Check the profile's raw ranges" if ranges else ""))
                        if update.restage or update.pages_read & RESOLVED_FROM:
                            last_frame = None

                if frame != last_frame and not away:
                    if session is None:
                        departed = bool(set(last_frame) - set(frame)) if last_frame else False
                        report(f"frame:{' [clear]' if departed else ''} "
                               f"{profile_module.format_frame(frame)}")
                        last_frame = frame
                        frames += 1
                    else:
                        result = session.show(frame_for(frame, staging))
                        if result is not None and result.acknowledged:
                            last_frame = frame
                            frames += 1
                        elif result is not None and result.refused is None:
                            # not shown, so the next frame is staged whole
                            misses += 1
                            last_frame = None
            except HostLightingDisconnectedError as error:
                if reopen is None:
                    raise
                # The board left the bus, as it does when another host reboots it,
                # or stopped answering. Nothing is given up on it.
                report(f"{lost_line(error)}. Waiting for it to return")
                session.claim.let_lapse()
                away = True
                retry_due = now + RECONNECT_SECONDS

            next_tick += period
            delay = next_tick - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                next_tick = time.monotonic()  # fell behind, so the schedule starts again from now

    except KeyboardInterrupt:
        report("")
        report(frame_stats(frames, misses, started))
    except HostLightingDisconnectedError as error:
        # reported as the board, so the user does not look for a fault in spice2x
        report("")
        report(lost_line(error))
        report(frame_stats(frames, misses, started))
        if session is not None and session.claim is not None:
            session.claim.let_lapse()
    except (ConnectionError, OSError) as error:
        # The game exiting closes the API socket, which is a normal ending. The
        # claim is still given up in the finally.
        report("")
        report(f"spice2x connection lost ({error})")
        report(frame_stats(frames, misses, started))
    finally:
        try:
            if session is not None:
                session.close()
        finally:
            library.removeHandler(lines)
            library.setLevel(level)
