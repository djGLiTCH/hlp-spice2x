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


def send_frame(device, frame, clear_first=False) -> bool:
    """Stage a resolved frame and publish it.

    The board's staging buffer persists between commits, so a control that
    drops out of the frame would otherwise keep its last colour forever. When
    the set of targets shrinks the caller asks for a CLEAR first, which also
    drops the per-pixel validity bits that overlay mode renders from.

    :param device: an opened HostLightingDevice
    :param frame: a resolved frame
    :param clear_first: whether to clear staging before staging this frame
    :return: False if the COMMIT went unacknowledged (the frame still applied)
    """
    if clear_first:
        device.send(hlp.CMD_CLEAR)

    buttons = [(target[1], rgb) for target, rgb in frame.items() if target[0] == 'button']
    for offset in range(0, len(buttons), hlp.SET_BUTTONS_MAX):
        batch = buttons[offset:offset + hlp.SET_BUTTONS_MAX]
        payload = bytes([len(batch)]) + b''.join(bytes([bid, *rgb]) for bid, rgb in batch)
        device.send(hlp.CMD_SET_BUTTONS, payload)

    for target, rgb in frame.items():
        if target[0] != 'range':
            continue
        _, start, count = target
        for offset in range(0, count, hlp.SET_RANGE_MAX):
            chunk = min(hlp.SET_RANGE_MAX, count - offset)
            device.send(hlp.CMD_SET_RANGE, bytes([start + offset, chunk]) + bytes(rgb) * chunk)

    try:
        device.request_ok(hlp.CMD_COMMIT, timeout=0.25)
        return True
    except hlp.HostLightingTimeout:
        return False  # best-effort: the frame still applied, the reply was late


def validate_targets(device, profile) -> list:
    """Report profile controls that have no LED on this board.

    Staging is fire-and-forget in the main loop, so a control the board cannot
    resolve is skipped silently. Probing each one once at startup turns that
    into a visible warning before the user wonders why a button never lights.

    Leaves the staging buffer cleared, so the first real frame starts from a
    known state even if another host left pixels staged.

    :param device: an opened HostLightingDevice
    :param profile: a loaded profile
    :return: names of mapped controls the board has no LED for
    """
    targets = sorted({target[1] for target, _ in profile.values() if target[0] == 'button'})
    unmapped = []
    for button_id in targets:
        try:
            reply = device.request_ok(hlp.CMD_SET_BUTTONS, bytes([1, button_id, 0, 0, 0]))
        except hlp.HostLightingTimeout:
            continue  # a probe that went unanswered is not evidence of anything
        if reply[4]:  # [3] applied, [4] skipped
            unmapped.append(hlp.control_name(button_id))
    device.request_ok(hlp.CMD_CLEAR)
    return unmapped


def frame_stats(frames: int, misses: int, started: float) -> str:
    """Summarise a run: frames published, rate, and unacknowledged commits."""
    elapsed = max(time.monotonic() - started, 1e-6)
    return (f"{frames} frames in {elapsed:.1f}s ({frames / elapsed:.1f}/s published), "
            f"{misses} commits unacknowledged")


def run(connection, profile, device=None, fps=60.0, timeout_ms=2000,
        overlay=False, full_brightness=False, report=print):
    """Poll spice2x and stream frames until interrupted or disconnected.

    :param connection: an open SpiceConnection
    :param profile: a loaded profile
    :param device: an opened HostLightingDevice, or None for a dry run
    :param fps: poll rate
    :param timeout_ms: takeover keepalive timeout sent in SET_MODE
    :param overlay: overlay onto the board's animations instead of taking the whole frame
    :param full_brightness: ignore the board's brightness setting
    :param report: callable used for progress output
    """
    fingerprint = None
    frames = misses = 0
    started = time.monotonic()

    try:
        if device is not None:
            reply = device.request_ok(hlp.CMD_PING)
            if reply[3:7] != b'GPHL' or (reply[7], reply[8]) < hlp.REQUIRED_VERSION:
                raise hlp.HostLightingError("handshake failed: need protocol 1.0 or later")
            _, label, firmware = hlp.read_identity(device)
            report(f"board: {label} ({firmware})")
            fingerprint = hlp.read_fingerprint(device)
            # SET_MODE: takeover mode, keepalive timeout ms (LE), apply board brightness
            device.request_ok(hlp.CMD_SET_MODE,
                              bytes([1 if overlay else 0, timeout_ms & 0xFF,
                                     (timeout_ms >> 8) & 0xFF, 0 if full_brightness else 1]))
            report(f"takeover: {'overlay' if overlay else 'whole frame'}, "
                   f"{timeout_ms} ms keepalive, "
                   f"{'ignoring' if full_brightness else 'applying'} board brightness")
            unmapped = validate_targets(device, profile)
            if unmapped:
                report(f"warning: {len(unmapped)} mapped control(s) have no LED on this "
                       f"board: {', '.join(unmapped)}")

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
                elif not send_frame(device, frame, clear_first=departed):
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
                current = hlp.read_fingerprint(device)
                if current != fingerprint:
                    ranges = any(target[0] == 'range' for target, _ in profile.values())
                    report("board LED map changed" + (" - check the profile's raw ranges"
                                                      if ranges else " - controls re-resolved"))
                    fingerprint = current

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
