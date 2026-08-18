"""The poll, resolve and publish loop.

Reads the game's lights from spice2x, turns them into a frame through the
profile, and publishes that frame to the board. Frames are only sent when
something changed; quiet frames send a single PING instead, which is enough to
hold the board's takeover without republishing an identical frame.

SPDX-FileCopyrightText: © 2026 Jacob Simpson
SPDX-License-Identifier: GPL-3.0-or-later
"""
import time

from . import hlp, profile as profile_module

# used when nobody has said otherwise and the board has not either
DEFAULT_FPS = 60.0
# Streaming faster than this buys nothing. The transport's own ceiling is not far
# above it, so a board that renders quicker would leave almost no headroom for a
# slow poll or a retry, and cabinet lighting gains nothing a person can see above
# it either way. The headroom is worth more than the rate.
MAX_STREAM_FPS = 60.0


def stage_range(device, start, count, rgb) -> None:
    """Stage one colour across a run of LEDs, split into as many reports as it needs.

    :param device: an opened HostLightingDevice
    :param start: first LED index
    :param count: how many LEDs to colour
    :param rgb: the colour to repeat across them
    """
    for offset in range(0, count, hlp.SET_RANGE_MAX):
        chunk = min(hlp.SET_RANGE_MAX, count - offset)
        device.send(hlp.CMD_SET_RANGE, bytes([start + offset, chunk]) + bytes(rgb) * chunk)


def send_frame(device, frame, clear_first=False, plan=None) -> bool:
    """Stage a resolved frame and publish it.

    The board's staging buffer persists between commits, so a control that
    drops out of the frame would otherwise keep its last colour forever. When
    the set of targets shrinks the caller asks for a CLEAR first, which also
    drops the per-pixel validity bits that overlay mode renders from.

    The three passes are emitted in a fixed order, and the order is load-bearing
    because they all write into one staging buffer where the last write to a
    pixel wins. Controls first, then the extra lights of a control, then raw
    ranges.

    The expansion has to follow the control pass rather than precede it. Naming
    a control colours every light of it on one of the two render pipelines, so a
    SET_BUTTONS entry emitted afterwards would re-colour the very pixels the
    expansion had just set, undoing it within the same frame and only on the
    pipeline that is harder to reproduce. Raw ranges go last so that addressing
    a pixel explicitly always beats reaching it through a control name.

    :param device: an opened HostLightingDevice
    :param frame: a resolved frame
    :param clear_first: whether to clear staging before staging this frame
    :param plan: the board's per-control staging plan, or None to stage every
        control by name and expand nothing
    :return: False if the COMMIT went unacknowledged (the frame still applied)
    """
    if clear_first:
        device.send(hlp.CMD_CLEAR)

    buttons, extra, ranges = [], [], []
    for target, rgb in frame.items():
        if target[0] == 'button':
            by_name, records = (plan or {}).get(target[1], (True, ()))
            if by_name:
                buttons.append((target[1], rgb))
            extra.extend((record['first_led'], record['led_count'], rgb) for record in records)
        elif target[0] == 'range':
            ranges.append((target[1], target[2], rgb))
        else:
            # staging is fire-and-forget, so a target kind no pass below
            # recognises would light nothing and report nothing either
            raise ValueError(f"frame carries an unknown target kind: {target[0]!r}")

    for offset in range(0, len(buttons), hlp.SET_BUTTONS_MAX):
        batch = buttons[offset:offset + hlp.SET_BUTTONS_MAX]
        payload = bytes([len(batch)]) + b''.join(bytes([bid, *rgb]) for bid, rgb in batch)
        # the count byte says how many fixed-width entries follow, so a colour of
        # the wrong width would shift the rest and stage garbage silently
        if len(payload) != 1 + 4 * len(batch):
            raise ValueError("SET_BUTTONS carries three-byte colours only")
        device.send(hlp.CMD_SET_BUTTONS, payload)

    for start, count, rgb in extra + ranges:
        stage_range(device, start, count, rgb)

    try:
        device.request_ok(hlp.CMD_COMMIT, timeout=0.25)
        return True
    except hlp.HostLightingTimeout:
        return False  # best-effort: the frame still applied, the reply was late


def validate_targets(device, profile, caps) -> tuple:
    """Report profile controls that this board, or this firmware, cannot light.

    Staging is fire-and-forget in the main loop, so a control the board cannot
    resolve is skipped silently. Checking each one once at startup turns that
    into a visible warning before the user wonders why a button never lights.

    Where the board publishes a light table this is a question the table already
    answers, so it is read rather than asked. Only a board with no table has to
    be probed by writing to it and seeing what comes back, which is also the
    only path that dirties the staging buffer and so the only one that has to
    clear it again.

    :param device: an opened HostLightingDevice
    :param profile: a loaded profile
    :param caps: the negotiated capabilities
    :return: (controls the board has no LED for, controls this firmware is too
        old to reach)
    """
    targets = sorted({target[1] for target, _ in profile.values() if target[0] == 'button'})
    if caps.light_table:
        owned = {record['button_id'] for record in caps.lights}
        return [hlp.control_name(button_id) for button_id in targets
                if button_id not in owned], []

    unmapped, unreachable = [], []
    for button_id in targets:
        if not caps.stages_by_name(button_id):
            # a control this protocol version can name but not stage, and with no
            # light table there is no raw index to reach it by either
            unreachable.append(hlp.control_name(button_id))
            continue
        try:
            reply = device.request_ok(hlp.CMD_SET_BUTTONS, bytes([1, button_id, 0, 0, 0]))
        except hlp.HostLightingTimeout:
            continue  # a probe that went unanswered is not evidence of anything
        if reply[4]:  # [3] applied, [4] skipped
            unmapped.append(hlp.control_name(button_id))
    try:
        device.request_ok(hlp.CMD_CLEAR)
    except hlp.HostLightingTimeout:
        pass  # tidying up after the probes, and the loop below shrugs these off
    return unmapped, unreachable


def resolve_stream_rate(fps, caps) -> tuple:
    """Decide how fast to stream, and why.

    A board that renders slower than the cap is matched exactly. Sending it
    frames it will never render wastes the USB bus and the poll loop feeding it
    for no visible gain.

    A board that renders faster is capped instead, because the headroom is worth
    more than the rate: streaming flat out leaves nothing spare for a slow poll
    or a retry, and nobody can see the difference on cabinet lighting anyway.

    :param fps: the rate asked for, or None if nobody asked
    :param caps: the negotiated capabilities
    :return: (the rate to stream at, why it is that rate)
    """
    if fps is not None:
        return fps, 'asked for with --fps'
    if not caps.connected:
        return DEFAULT_FPS, 'no board to ask'
    if not caps.render_hz:
        return DEFAULT_FPS, 'the board did not state a render rate'
    if caps.render_hz < MAX_STREAM_FPS:
        return float(caps.render_hz), "matching the board's render rate"
    return MAX_STREAM_FPS, f"capped below the board's {caps.render_hz} Hz"


def frame_stats(frames: int, misses: int, started: float) -> str:
    """Summarise a run: frames published, rate, and unacknowledged commits."""
    elapsed = max(time.monotonic() - started, 1e-6)
    return (f"{frames} frames in {elapsed:.1f}s ({frames / elapsed:.1f}/s published), "
            f"{misses} commits unacknowledged")


def run(connection, profile, device=None, fps=None, timeout_ms=2000,
        overlay=False, full_brightness=False, force=False, report=print):
    """Poll spice2x and stream frames until interrupted or disconnected.

    :param connection: an open SpiceConnection
    :param profile: a loaded profile
    :param device: an opened HostLightingDevice, or None for a dry run
    :param fps: poll rate, or None to resolve one after the handshake
    :param timeout_ms: takeover keepalive timeout sent in SET_MODE
    :param overlay: overlay onto the board's animations instead of taking the whole frame
    :param full_brightness: ignore the board's brightness setting
    :param force: run against a protocol major version this bridge does not support
    :param report: callable used for progress output
    """
    fingerprint = None
    frames = misses = 0
    started = time.monotonic()
    caps = hlp.Capabilities.absent()
    plan = {}

    try:
        if device is not None:
            caps = hlp.negotiate(device, force=force)
            _, label, firmware = hlp.read_identity(device)
            report(f"board: {label} ({firmware})")
            if caps.forced:
                report(f"WARNING: this bridge does not support Host Lighting "
                       f"v{caps.reported[0]}.{caps.reported[1]}, and --force was given. "
                       f"Commands may not mean what this bridge thinks they mean, so "
                       f"nothing below this line is reliable.")
            report(caps.summary())
            fingerprint = caps.fingerprint
            # SET_MODE: takeover mode, keepalive timeout ms (LE), apply board brightness
            device.request_ok(hlp.CMD_SET_MODE,
                              bytes([1 if overlay else 0, timeout_ms & 0xFF,
                                     (timeout_ms >> 8) & 0xFF, 0 if full_brightness else 1]))
            report(f"takeover: {'overlay' if overlay else 'whole frame'}, "
                   f"{timeout_ms} ms keepalive, "
                   f"{'ignoring' if full_brightness else 'applying'} board brightness")
            unmapped, unreachable = validate_targets(device, profile, caps)
            if unmapped:
                report(f"warning: {len(unmapped)} mapped control(s) have no LED on this "
                       f"board: {', '.join(unmapped)}")
            if unreachable:
                report(f"warning: {len(unreachable)} mapped control(s) need Host Lighting "
                       f"v1.2 or newer to reach: {', '.join(unreachable)}")
            plan = caps.staging_plan()
            several = sorted(hlp.control_name(button_id)
                             for button_id, (_, records) in plan.items() if len(records) > 1)
            if several:
                report(f"controls the board gives more than one light, all of which will be "
                       f"lit: {', '.join(several)}")

        # resolved here rather than at argparse time, so that the board gets a
        # say in it: nothing is known about the board until the handshake above
        fps, why = resolve_stream_rate(fps, caps)
        report(f"streaming at {fps:g} fps ({why})")
        period = 1.0 / fps
        keepalive_due = time.monotonic() + timeout_ms / 2000.0
        last_frame = None
        reported_unmapped = False
        started = next_tick = fingerprint_due = time.monotonic()

        while True:
            states = connection.lights_read()
            if not reported_unmapped:
                missing = sorted(set(states) - set(profile))
                if missing:
                    report(f"{len(missing)} lights not in the profile, ignoring: "
                           f"{', '.join(missing[:6])}{' ...' if len(missing) > 6 else ''}")
                reported_unmapped = True

            frame = profile_module.resolve_frame(states, profile)
            if frame != last_frame:
                # a target that has left the frame must be cleared, or its last
                # colour stays staged on the board and republishes every commit
                departed = bool(set(last_frame) - set(frame)) if last_frame else False
                if device is None:
                    report(f"frame:{' [clear]' if departed else ''} "
                           f"{profile_module.format_frame(frame)}")
                elif not send_frame(device, frame, clear_first=departed, plan=plan):
                    misses += 1
                last_frame = frame
                keepalive_due = time.monotonic() + timeout_ms / 2000.0
                frames += 1
            elif device is not None and time.monotonic() >= keepalive_due:
                try:
                    device.request_ok(hlp.CMD_PING, timeout=0.25)
                except hlp.HostLightingTimeout:
                    pass
                keepalive_due = time.monotonic() + timeout_ms / 2000.0

            # checked on wall-clock, not frame count, so a static game screen
            # (which produces no new frames) still notices a reconfiguration
            if device is not None and time.monotonic() >= fingerprint_due:
                fingerprint_due = time.monotonic() + 5.0
                current = hlp.read_state(device)['fingerprint']
                if current != fingerprint:
                    fingerprint = current
                    # Re-read rather than only saying so. The light table is what
                    # the expansion addresses, and a table that moved underneath
                    # it points at the wrong lights while still looking valid: a
                    # stale ordinal is a perfectly good ordinal for another light.
                    # The walk costs a round trip per four records, so this drops
                    # a frame on a large board; the tick below catches back up.
                    caps.refresh(device)
                    plan = caps.staging_plan()
                    ranges = any(target[0] == 'range' for target, _ in profile.values())
                    report("board LED map changed, controls re-resolved"
                           + (" - check the profile's raw ranges" if ranges else ""))

            next_tick += period
            delay = next_tick - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                next_tick = time.monotonic()  # fell behind; do not spiral

    except KeyboardInterrupt:
        report("")
        report(frame_stats(frames, misses, started))
    except hlp.HostLightingDisconnected as error:
        # blaming spice2x for the board being unplugged sends the user looking
        # in entirely the wrong place
        report("")
        report(f"board disconnected ({error})")
        report(frame_stats(frames, misses, started))
    except (ConnectionError, OSError) as error:
        # the game exiting closes the API socket; that is a normal ending, and
        # the board still needs releasing, which the caller's finally handles
        report("")
        report(f"spice2x connection lost ({error})")
        report(frame_stats(frames, misses, started))
