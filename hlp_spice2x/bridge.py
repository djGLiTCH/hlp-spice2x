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


def stage_range_rgbw(device, start, count, rgbw) -> None:
    """Stage one RGBW colour across a run of LEDs, in as many reports as it needs.

    :param device: an opened HostLightingDevice
    :param start: first LED index
    :param count: how many LEDs to colour
    :param rgbw: the colour to repeat across them
    """
    for offset in range(0, count, hlp.SET_RANGE_RGBW_MAX):
        chunk = min(hlp.SET_RANGE_RGBW_MAX, count - offset)
        device.send(hlp.CMD_SET_RANGE_RGBW,
                    bytes([start + offset, chunk]) + bytes(rgbw) * chunk)


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


class FrameResult:
    """What became of one published frame.

    Truthy when the COMMIT was acknowledged, so a caller that only cares whether
    the frame landed can still read it as a plain yes or no.
    """

    def __init__(self, acknowledged: bool, skipped=None):
        """Record how the board answered.

        :param acknowledged: whether the COMMIT came back in time
        :param skipped: ordinals the board reported it did not colour, where it
            was asked and is new enough to say
        """
        self.acknowledged = acknowledged
        self.skipped = list(skipped or ())

    def __bool__(self) -> bool:
        """Report whether the COMMIT was acknowledged."""
        return self.acknowledged


def stage_group(device, operations, verify=False) -> list:
    """Emit one group of staging operations, batching the per-light entries.

    Per-light staging is normally fire-and-forget like everything else in a
    frame, because a round trip per report costs more than the diagnosis is
    worth at streaming rates. When the caller asks to verify, the round-trip
    form is used instead and the board is asked which entries it actually
    coloured; only firmware from v1.3 can answer that, so the caller is the one
    that has to know whether asking is worth anything.

    :param device: an opened HostLightingDevice
    :param operations: (operation, rgb) pairs, as a staging map produces them
    :param verify: wait for each per-light report and read back what was skipped
    :return: the ordinals the board reported skipping, empty unless verifying
    """
    skipped = []
    batches = (
        (False, [(operation[1], *colour[:3])
                 for operation, colour in operations if operation[0] == 'light']),
        (True, [(operation[1], *hlp.subtractive_white(colour))
                for operation, colour in operations if operation[0] == 'light_rgbw']),
    )
    for rgbw, entries in batches:
        if not entries:
            continue
        if verify:
            stage = device.set_lights_rgbw if rgbw else device.set_lights
            _, _, stale = stage(entries, outcomes=True)
            skipped.extend(stale or ())
        else:
            device.send_lights(entries, rgbw=rgbw)
    for operation, colour in operations:
        if operation[0] == 'range':
            stage_range(device, operation[1], operation[2], colour[:3])
        elif operation[0] == 'range_rgbw':
            stage_range_rgbw(device, operation[1], operation[2], hlp.subtractive_white(colour))
    return skipped


def send_frame(device, frame, clear_first=False, staging=None, verify=False) -> FrameResult:
    """Stage a resolved frame and publish it.

    The board's staging buffer persists between commits, so a control that
    drops out of the frame would otherwise keep its last colour forever. When
    the set of targets shrinks the caller asks for a CLEAR first, which also
    drops the per-pixel validity bits that overlay mode renders from.

    The three passes are emitted in a fixed order, and the order is load-bearing
    because they all write into one staging buffer where the last write to a
    pixel wins. Control names first, then the extra lights those names imply,
    then everything the profile asked for by name of a single light or by raw
    index.

    The expansion has to follow the control pass rather than precede it. Naming
    a control colours all of its lights on one of the two render pipelines, so a
    SET_BUTTONS entry emitted afterwards would re-colour the very pixels the
    expansion had just set, undoing it within the same frame and only on the
    pipeline that is harder to reproduce. What the profile asked for explicitly
    goes last, so saying exactly which light is meant always beats reaching it
    through a control name.

    :param device: an opened HostLightingDevice
    :param frame: a resolved frame
    :param clear_first: whether to clear staging before staging this frame
    :param staging: how each target reaches the board's lights, from
        staging_for, or None to stage every control by name and expand nothing
    :param verify: ask the board which per-light entries it actually coloured,
        which only firmware from v1.3 can answer
    :return: what became of the frame; falsy if the COMMIT went unacknowledged,
        which does not stop the frame applying
    """
    if clear_first:
        device.send(hlp.CMD_CLEAR)

    buttons, implied, asked = [], [], []
    for target, colour in frame.items():
        if target[0] == 'button':
            default = (True, ())
        elif target[0] == 'range':
            default = (False, [('range', target[1], target[2])])
        elif target[0] == 'light':
            # its ordinal is only knowable against a board, so one that reached
            # here unresolved is a wiring mistake, and a silent one otherwise
            if target not in (staging or {}):
                raise ValueError(f"per-light target {target} was never resolved against a board")
            default = None
        else:
            # staging is fire-and-forget, so a target kind no pass below
            # recognises would light nothing and report nothing either
            raise ValueError(f"frame carries an unknown target kind: {target[0]!r}")
        by_name, operations = (staging or {}).get(target, default)
        if by_name:
            buttons.append((target[1], colour))
        group = implied if target[0] == 'button' else asked
        group.extend((operation, colour) for operation in operations)

    for offset in range(0, len(buttons), hlp.SET_BUTTONS_MAX):
        batch = buttons[offset:offset + hlp.SET_BUTTONS_MAX]
        # SET_BUTTONS is RGB-only: the protocol has no RGBW form of it, so a
        # white component never reaches the board down this path
        payload = bytes([len(batch)]) + b''.join(bytes([bid, *colour[:3]])
                                                 for bid, colour in batch)
        # the count byte says how many fixed-width entries follow, so a colour of
        # the wrong width would shift the rest and stage garbage silently
        if len(payload) != 1 + 4 * len(batch):
            raise ValueError("SET_BUTTONS carries three-byte colours only")
        device.send(hlp.CMD_SET_BUTTONS, payload)

    skipped = stage_group(device, implied, verify) + stage_group(device, asked, verify)

    try:
        device.request_ok(hlp.CMD_COMMIT, timeout=0.25)
        return FrameResult(True, skipped)
    except hlp.HostLightingTimeout:
        # best-effort: the frame still applied, the reply was late
        return FrameResult(False, skipped)


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
        if len(reply) < 5:
            continue  # nor is one too short to hold the count being read
        if reply[4]:  # [3] applied, [4] skipped
            unmapped.append(hlp.control_name(button_id))
    try:
        device.request_ok(hlp.CMD_CLEAR)
    except hlp.HostLightingTimeout:
        pass  # tidying up after the probes, and the loop below shrugs these off
    return unmapped, unreachable


def light_operation(caps, button_id, index) -> tuple:
    """Resolve one per-light profile target against the board's light table.

    Three different things can go wrong here and they want three different
    answers, because they send the user to three different places: firmware too
    old to stage a single light at all, a board whose table cannot describe
    which lights belong to a control, and an index past the end of a control
    that does have a table.

    :param caps: the negotiated capabilities
    :param button_id: the control being indexed into
    :param index: which of that control's lights, in the order the table lists them
    :return: the staging operation that colours that light
    :raises hlp.HostLightingIncompatible: if the firmware predates per-light staging
    :raises profile_module.ProfileError: if this board cannot offer that light
    """
    name = hlp.control_name(button_id)
    if not caps.per_light:
        raise hlp.HostLightingIncompatible(
            f"the profile targets {name}[{index}], and colouring one light of a control needs "
            f"Host Lighting v1.2 or newer; this board reports "
            f"v{caps.reported[0]}.{caps.reported[1]}")
    if not caps.light_table:
        raise profile_module.ProfileError(
            f"{name}[{index}] cannot be resolved: this board reported no light table, so which "
            f"of its lights belong to {name} is not knowable")
    records = caps.lights_owned_by(button_id)
    if not records and caps.names_control(button_id):
        raise profile_module.ProfileError(
            f"{name}[{index}] cannot be resolved: this board's light table is synthesised from "
            f"its per-control configuration, so it describes one light per control however many "
            f"that control really drives. This is a board limitation, not a firmware one")
    if index >= len(records):
        raise profile_module.ProfileError(
            f"{name}[{index}] is out of range: this board gives {name} {len(records)} "
            f"light{'' if len(records) == 1 else 's'}")
    return caps.stage_op(records[index])


def staging_for(profile, caps) -> dict:
    """Work out how each of the profile's targets reaches the board's lights.

    Built at connect and again whenever the board says its LED map changed,
    never per frame. The answers depend on the board, and the board only changes
    its mind when it says so.

    :param profile: a loaded profile
    :param caps: the negotiated capabilities
    :return: mapping of target to (stage by control name, staging operations)
    :raises hlp.HostLightingIncompatible: if the firmware cannot honour the profile
    :raises profile_module.ProfileError: if the board cannot honour the profile
    """
    staging = {('button', button_id): entry
               for button_id, entry in caps.staging_plan().items()}
    for target, colour in profile.values():
        # a white component only survives where the chain has an emitter for it
        # and the firmware honours what the host sent rather than deriving its own
        white = len(colour) > 3 and caps.host_white
        if target[0] == 'light':
            operation = light_operation(caps, target[1], target[2])
            staging[target] = (False, [('light_rgbw', operation[1]) if white else operation])
        elif target[0] == 'range' and white:
            staging[target] = (False, [('range_rgbw', target[1], target[2])])
        elif target[0] == 'button' and white:
            # SET_BUTTONS cannot carry white, so a control asking for it has to
            # be reached one light at a time instead of by name
            records = caps.lights_owned_by(target[1])
            if records:
                staging[target] = (False, [('light_rgbw', record['ordinal'])
                                           for record in records])
    return staging


def white_warnings(profile, caps) -> list:
    """Say, once at startup, where a profile's white component cannot be used.

    Per frame this would be noise, and silence would leave the user staring at a
    colour that is not the one they asked for with nothing to explain it. The
    reason matters as much as the fact: a chain with no white emitter and a
    firmware that ignores a host-supplied one look identical from the outside
    and are fixed in completely different ways.

    :param profile: a loaded profile
    :param caps: the negotiated capabilities
    :return: warnings to report, empty when the profile asks for no white
    """
    carrying = [target for target, colour in profile.values() if len(colour) > 3]
    if not carrying:
        return []
    asking = ('1 profile entry asks' if len(carrying) == 1
              else f"{len(carrying)} profile entries ask")
    if not caps.white_channel:
        chain = hlp.LED_FORMAT_NAMES.get(caps.led_map.get('colour_format'), 'unknown')
        return [f"{asking} for a white component, but this "
                f"board's LED chain is {chain} and has no white emitter to drive it, so the "
                f"white is dropped and the colour is sent as RGB"]
    if not caps.host_white:
        return [f"{asking} for a white component, but Host "
                f"Lighting v{caps.reported[0]}.{caps.reported[1]} maps achromatic colours onto "
                f"the white emitter in firmware and ignores a host-supplied white, so the white "
                f"is dropped and the colour is sent as RGB, which is what renders correctly "
                f"there. v1.3 honours it"]
    stranded = sorted({hlp.control_name(target[1]) for target in carrying
                       if target[0] == 'button' and not caps.lights_owned_by(target[1])})
    if stranded:
        return [f"white was asked for on {', '.join(stranded)}, but this board's light table "
                f"does not say which lights belong to {'them' if len(stranded) > 1 else 'it'}, "
                f"and white can only be sent to a light named individually, so the white is "
                f"dropped there and the colour is sent as RGB"]
    return []


def react_to_skips(device, caps, profile, skipped, report) -> bool:
    """Work out why the board skipped a light, and re-read the table if it moved.

    Being told is the whole point of the outcome mask. Before it existed a stale
    ordinal was undiagnosable: it is still a perfectly valid ordinal, so the
    board applies it, answers OK, and colours the wrong light.

    The fingerprint separates the two cases. If it moved, the table moved
    underneath the cached ordinals and re-reading fixes them. If it did not, the
    ordinals are current and the profile is simply asking for lights this board
    does not have, which no amount of re-reading will change.

    :param device: an opened HostLightingDevice
    :param caps: the negotiated capabilities, refreshed in place if the map moved
    :param profile: a loaded profile
    :param skipped: the ordinals the board reported skipping
    :param report: callable used for progress output
    :return: whether the next frame is worth verifying again
    """
    named = ', '.join(str(ordinal) for ordinal in skipped)
    before = caps.fingerprint
    try:
        caps.refresh(device)
    except hlp.HostLightingDisconnected:
        raise  # the board going away is not something to carry on through
    except hlp.HostLightingError as error:
        report(f"warning: board skipped light(s) {named}, and re-reading its light table "
               f"failed ({error})")
        return False
    if caps.fingerprint != before:
        report(f"board skipped light(s) {named}; its LED map had changed underneath, so the "
               f"light table was re-read")
        return caps.outcome_mask
    report(f"warning: board skipped light(s) {named} and its LED map has not changed, so the "
           f"profile is asking for lights this board does not have")
    return False


def ranges_past_the_board(profile, caps) -> list:
    """Find raw ranges that reach past the last LED this board has.

    The load-time check bounds a range by the firmware's buffer ceiling, which
    is all that can be known without a board: it catches a typo, not a profile
    written for a bigger board. The board reports its own extent, and a range
    past it is staged happily and lights nothing, because staging is
    fire-and-forget and out-of-range pixels are simply dropped.

    A warning rather than a refusal: unlike a control name, a range is the
    profile's own arithmetic, and half of one landing is still half a profile
    working.

    :param profile: a loaded profile
    :param caps: the negotiated capabilities
    :return: the (start, count) pairs that reach past the board, in order
    """
    return sorted({(target[1], target[2]) for target, _ in profile.values()
                   if target[0] == 'range' and target[1] + target[2] > caps.led_extent})


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
    :raises hlp.HostLightingIncompatible: if the board's firmware cannot honour the profile
    :raises profile_module.ProfileError: if the board itself cannot honour the profile
    """
    fingerprint = None
    frames = misses = 0
    started = time.monotonic()
    caps = hlp.Capabilities.absent()
    staging = {}
    verify_next = False

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
            staging = staging_for(profile, caps)
            # the ordinals are freshly resolved, so the next frame is the one
            # worth asking about; after that nothing has moved to invalidate them
            verify_next = caps.outcome_mask
            over = ranges_past_the_board(profile, caps)
            if over:
                report(f"warning: {len(over)} raw range(s) reach past this board's "
                       f"{caps.led_extent} LEDs and light nothing beyond it: "
                       + ', '.join(f"[{start}, {count}]" for start, count in over))
            for warning in white_warnings(profile, caps):
                report(f"warning: {warning}")
            several = sorted(hlp.control_name(target[1])
                             for target, (_, operations) in staging.items()
                             if target[0] == 'button' and len(operations) > 1)
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
                else:
                    result = send_frame(device, frame, clear_first=departed, staging=staging,
                                        verify=verify_next)
                    verify_next = False
                    if not result:
                        misses += 1
                    if result.skipped:
                        verify_next = react_to_skips(device, caps, profile, result.skipped,
                                                     report)
                        staging = staging_for(profile, caps)
                last_frame = frame
                keepalive_due = time.monotonic() + timeout_ms / 2000.0
                frames += 1
            elif device is not None and time.monotonic() >= keepalive_due:
                try:
                    device.request_ok(hlp.CMD_PING, timeout=0.25)
                except (hlp.HostLightingTimeout, hlp.HostLightingRejected):
                    pass  # a keepalive is worth sending, not worth insisting on
                keepalive_due = time.monotonic() + timeout_ms / 2000.0

            # checked on wall-clock, not frame count, so a static game screen
            # (which produces no new frames) still notices a reconfiguration
            if device is not None and time.monotonic() >= fingerprint_due:
                fingerprint_due = time.monotonic() + 5.0
                changed = False
                try:
                    current = hlp.read_state(device)['fingerprint']
                    changed = current != fingerprint
                    if changed:
                        # Re-read rather than only saying so. The light table is
                        # what the expansion addresses, and a table that moved
                        # underneath it points at the wrong lights while still
                        # looking valid: a stale ordinal is a perfectly good
                        # ordinal for another light. The walk costs a round trip
                        # per four records, so this drops a frame on a large
                        # board; the tick below catches back up.
                        caps.refresh(device)
                except hlp.HostLightingDisconnected:
                    raise  # the board going away is not a housekeeping failure
                except hlp.HostLightingError as error:
                    # This poll is housekeeping. Every other round trip the loop
                    # makes already shrugs off a bad reply, and the light table
                    # is at its most likely to be in flux during exactly the
                    # reconfiguration that moved the fingerprint. The next poll
                    # is five seconds away.
                    report(f"warning: could not re-read the board's LED map ({error}), "
                           f"retrying in 5s")
                    changed = False
                if changed:
                    # only once the re-read has landed: refresh replaces page 1
                    # before it walks the light table, so a failure part-way
                    # through would leave a new fingerprint standing against a
                    # stale table that nothing would ever re-read
                    fingerprint = current
                    staging = staging_for(profile, caps)
                    verify_next = caps.outcome_mask
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
