"""Show, one still picture at a time, which way of naming a light wins.

There are three ways a profile can reach a light, and where two of them touch
the same pixel the order they are staged in decides the colour. That order is
the single easiest thing to get wrong in a host implementation, and getting it
wrong produces a frame that looks right on one of the board's two render
pipelines and wrong on the other.

The rule this demonstrates: a control name is staged first, the extra lights
that name implies are staged next, and anything the profile asked for explicitly
is staged last. So naming one light beats reaching it through its control, and
that holds even when the explicit entry is asking for black.

Four steps, five seconds each, on one control that the board gives more than one
light. Nothing else on the board is lit and nothing moves inside a step, so each
step is a single picture that is either right or wrong:

    1  the control name alone            both of its lights, red
    2  plus its second light in cyan     first red, second CYAN
    3  plus its second light in black    first red, second DARK
    4  the control name alone again      both of its lights, red again

Step 3 is the one worth watching. An explicit entry asking for black must still
win, or a profile could never turn one light of a control off. Step 4 then
proves the staging buffer is cleared when that explicit entry leaves the frame,
without which the light would stay dark for the rest of the run.

Needs a board whose light table gives some control more than one light. Many
boards give none, and there is nothing to demonstrate on those.

Run it from the repository root:

    python -m tests.hardware_precedence

SPDX-FileCopyrightText: © 2026 Jacob Simpson
SPDX-License-Identifier: GPL-3.0-or-later
"""
import sys
import time

from hlp_spice2x import bridge, hlp

RED = (255, 0, 0)
CYAN = (0, 255, 255)
OFF = (0, 0, 0)
STEP_SECONDS = 5.0


def hold(device, frame, staging, previous, label, expect) -> dict:
    """Publish one frame and hold it, clearing whatever left the frame.

    The clear is what the streaming loop does when a target drops out: the
    board's staging buffer persists between commits, so a target that is no
    longer in the frame would otherwise keep its last colour forever.

    :param device: an opened HostLightingDevice
    :param frame: the resolved frame to publish
    :param staging: the staging map the frame's targets were resolved against
    :param previous: the frame published before this one, or None
    :param label: what this step is doing
    :param expect: what should be visible while it is held
    :return: this frame, to pass as `previous` next time
    """
    departed = bool(set(previous) - set(frame)) if previous else False
    print(f"\n  {label}")
    print(f"    EXPECT: {expect}")
    bridge.send_frame(device, frame, staging=staging, clear_first=departed)
    deadline = time.monotonic() + STEP_SECONDS
    while time.monotonic() < deadline:
        time.sleep(0.25)
        try:
            device.request_ok(hlp.CMD_PING, timeout=0.25)
        except hlp.HostLightingError:
            pass  # a keepalive is worth sending, not worth insisting on
    return dict(frame)


def pick_control(caps):
    """Find a control the board gives more than one light.

    Synthesised records are excluded by lights_owned_by, because a table
    rebuilt from per-control configuration holds one row per control however
    many lights it really drives, and so can never answer this question.

    :param caps: the negotiated capabilities
    :return: (button ID, its light records), or (None, []) if there is no such control
    """
    for button_id in sorted({record['button_id'] for record in caps.lights}):
        records = caps.lights_owned_by(button_id)
        if len(records) > 1 and button_id in hlp.ONE_LAMP_CONTROLS:
            return button_id, records
    return None, []


def main(argv=None) -> int:
    """Run the four steps against an attached board.

    :param argv: optional board-ID prefix as the only argument
    :return: process exit status
    """
    prefix = (argv or sys.argv[1:] or [''])[0]
    device = hlp.open_device(prefix)
    try:
        caps = hlp.negotiate(device)
        print(caps.summary())
        if not caps.per_light:
            print("\nthis board's firmware cannot colour one light of a control;"
                  " that needs Host Lighting v1.2 or newer")
            return 1

        button_id, records = pick_control(caps)
        if button_id is None:
            print("\nno control on this board owns more than one light, so there is"
                  " nothing here to demonstrate")
            return 1

        name = hlp.control_name(button_id)
        first, second = records[0]['first_led'], records[1]['first_led']
        print(f"\nusing {name}, which owns LED {first} ({name}[0]) "
              f"and LED {second} ({name}[1])")
        print(f"four steps, {STEP_SECONDS:.0f}s each, nothing else on the board lit")

        # Two profiles: one that mentions the second light and one that does not.
        # Which staging map is used is what decides whether that light is the
        # control's to colour or its own.
        control_only = {'control': ((('button', button_id)), RED)}
        with_light = dict(control_only, light=(('light', button_id, 1), CYAN))
        staging_control = bridge.staging_for(control_only, caps)
        staging_light = bridge.staging_for(with_light, caps)

        # whole-frame takeover, 10 s keepalive, honour the board's brightness
        device.request_ok(hlp.CMD_SET_MODE, bytes([0, 0x10, 0x27, 1]))

        frame = hold(device, {('button', button_id): RED}, staging_control, None,
                     "STEP 1 of 4 - the control name alone",
                     f"BOTH {name} lights red (LED {first} and LED {second})")
        frame = hold(device, {('button', button_id): RED, ('light', button_id, 1): CYAN},
                     staging_light, frame,
                     f"STEP 2 of 4 - the control name, plus {name}[1] asking for cyan",
                     f"LED {first} red, LED {second} CYAN - the explicit entry wins")
        frame = hold(device, {('button', button_id): RED, ('light', button_id, 1): OFF},
                     staging_light, frame,
                     f"STEP 3 of 4 - the control name, plus {name}[1] asking for black",
                     f"LED {first} red, LED {second} DARK - explicit wins even asking for off")
        hold(device, {('button', button_id): RED}, staging_control, frame,
             "STEP 4 of 4 - the control name alone again",
             f"BOTH {name} lights red again - the buffer was cleared when the entry left")
    finally:
        try:
            device.request_ok(hlp.CMD_RELEASE)
            print("\nreleased, on-board animations restored")
        except hlp.HostLightingError as error:
            print(f"\nrelease failed: {error}")
        device.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
