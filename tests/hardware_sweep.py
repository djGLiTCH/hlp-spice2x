"""Walk a board through every way this bridge can light it, one phase at a time.

A functional check to run by eye after changing anything that touches staging.
Each phase announces what should be visible before it starts, holds it long
enough to look at, and moves on. Roughly a hundred seconds in total.

Everything goes through the bridge's own staging, so what is exercised is what
a real run does rather than a hand-rolled approximation of it. Every control it
uses is taken from what the board reports, so it adapts to whatever is attached:
phases with nothing to demonstrate on a given board say so and are skipped.

The eleven phases, and what each is actually testing:

    1   every control in turn        that each control name resolves on its own
    2   every light in turn          that every ordinal addresses the light it
                                     claims to - the strongest check here, since
                                     a control owning two lights shows them one
                                     at a time rather than together
    3   every control at once        that a full frame stages without collisions
    4   multi-light controls         that a bare control name reaches all of its
                                     lights, not just the first
    5   one light of a control       that an index picks out the light it names
    6   a whole strip, two ways      by control name, then by raw LED range
    7   precedence                   that explicit targeting beats a control name
    8   brightness                   that a light level scales the colour smoothly
    9   a control leaving the frame  that staging is cleared so it goes dark
    10  overlay mode                 that the board's own animation shows through
    11  keepalive and release        that takeover survives without new frames,
                                     and that the board is handed back cleanly

Takes the board's lighting over for the duration and releases it at the end. It
also reports anything the board itself objected to - an unacknowledged commit,
or an ordinal it says it skipped - so a failure shows up even if nobody is
watching the LEDs.

Run it from the repository root:

    python -m tests.hardware_sweep

SPDX-FileCopyrightText: (C) 2026 Jacob Simpson
SPDX-License-Identifier: GPL-3.0-or-later
"""
import sys
import time

from hlp_spice2x import bridge, hlp
from hlp_spice2x import profile as profile_module

RED = (255, 0, 0)
GREEN = (0, 255, 0)
BLUE = (0, 0, 255)
WHITE = (255, 255, 255)
AMBER = (255, 160, 0)
MAGENTA = (255, 0, 255)


class Sweep:
    """Runs the phases against one board, tracking what the board objected to."""

    def __init__(self, device, caps):
        """Take over the board and work out what it can demonstrate.

        :param device: an opened HostLightingDevice
        :param caps: the negotiated capabilities
        """
        self.device = device
        self.caps = caps
        self.started = time.monotonic()
        self.failures = []
        self.last_frame = None

        self.controls = sorted({record['button_id'] for record in caps.lights})
        # controls the board gives more than one light, which is what makes
        # expansion and per-light targeting worth demonstrating at all
        self.multi = [button_id for button_id in self.controls
                      if button_id in hlp.ONE_LAMP_CONTROLS
                      and len(caps.lights_owned_by(button_id)) > 1]
        self.singles = [button_id for button_id in self.controls
                        if button_id in hlp.ONE_LAMP_CONTROLS]
        self.case = caps.led_map.get('case')

    def stamp(self) -> str:
        """Seconds since the sweep began, for lining up with what was seen."""
        return f"t+{time.monotonic() - self.started:6.1f}s"

    def phase(self, number, title, expected) -> None:
        """Announce a phase and what should be visible during it."""
        print(f"\n[{self.stamp()}] PHASE {number}: {title}")
        print(f"             expect: {expected}")

    def note(self, message) -> None:
        """Print a sub-step inside a phase."""
        print(f"[{self.stamp()}]   {message}")

    def skip(self, number, title, why) -> None:
        """Record a phase this board cannot demonstrate."""
        print(f"\n[{self.stamp()}] PHASE {number}: {title}")
        print(f"             SKIPPED: {why}")

    def fail(self, message) -> None:
        """Record something the board itself reported as wrong."""
        self.failures.append(message)
        print(f"[{self.stamp()}]   FAIL {message}")

    def hold(self, frame, staging, seconds, verify=False) -> None:
        """Publish a frame and hold it, clearing whatever left the frame.

        Works out the clear the way the streaming loop does. Without it every
        earlier phase would still be lit underneath the current one, because the
        board's staging buffer persists between commits.
        """
        departed = bool(set(self.last_frame) - set(frame)) if self.last_frame else False
        self.last_frame = dict(frame)
        result = bridge.send_frame(self.device, frame, staging=staging,
                                   clear_first=departed, verify=verify)
        if not result.acknowledged:
            self.fail("COMMIT went unacknowledged")
        if result.skipped:
            self.fail(f"board reported skipping ordinal(s) {result.skipped}")
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            time.sleep(0.25)
            try:
                self.device.request_ok(hlp.CMD_PING, timeout=0.25)
            except hlp.HostLightingError:
                pass  # a keepalive is worth sending, not worth insisting on

    def build_profile(self) -> dict:
        """Describe every target the sweep uses, so staging_for can resolve them.

        A staging map is built once from a profile, not per frame, because what
        a target resolves to depends on the board rather than on the frame.
        """
        profile = {f"c{button_id}": (('button', button_id), WHITE)
                   for button_id in self.controls}
        for button_id in self.multi:
            for index in range(len(self.caps.lights_owned_by(button_id))):
                profile[f"l{button_id}.{index}"] = (('light', button_id, index), WHITE)
        if self.case:
            profile['case'] = (('range', self.case[0], self.case[1]), AMBER)
        return profile

    def run(self) -> None:
        """Run every phase this board can demonstrate."""
        profile = self.build_profile()
        staging = bridge.staging_for(profile, self.caps)
        for warning in bridge.white_warnings(profile, self.caps):
            print(f"[{self.stamp()}] warning: {warning}")

        # whole-frame takeover, 10 s keepalive, honour the board's brightness
        self.device.request_ok(hlp.CMD_SET_MODE, bytes([0, 0x10, 0x27, 1]))

        self.phase(1, "every control in turn",
                   "each control lit white on its own, in the board's own order")
        for button_id in self.controls:
            self.note(f"{hlp.control_name(button_id)} ({button_id})")
            self.hold({('button', button_id): WHITE}, staging, 0.6)

        self.phase(2, "every light in turn, by its own ordinal",
                   "each individual light alone, so a control owning two shows them "
                   "one at a time")
        ordered = sorted((record['ordinal'], (button_id, index))
                         for button_id in self.controls
                         for index, record in enumerate(self.caps.lights_owned_by(button_id)))
        walk = {f"o{ordinal}": (('light', button_id, index), WHITE)
                for ordinal, (button_id, index) in ordered}
        walk_staging = bridge.staging_for(walk, self.caps)
        self.note(f"{len(ordered)} lights, 0.4s each")
        for ordinal, (button_id, index) in ordered:
            self.note(f"ordinal {ordinal}: {hlp.control_name(button_id)}[{index}]")
            self.hold({('light', button_id, index): WHITE}, walk_staging, 0.4)

        self.phase(3, "every control at once",
                   "the whole board lit white together, nothing missing or flickering")
        self.hold({('button', button_id): WHITE for button_id in self.controls}, staging, 5)

        if self.multi:
            named = ', '.join(hlp.control_name(b) for b in self.multi)
            self.phase(4, "controls the board gives more than one light",
                       f"{named} red, BOTH lights of each, everything else dark")
            self.hold({('button', button_id): RED for button_id in self.multi}, staging, 6)

            self.phase(5, "one light of a control at a time",
                       f"{named}: their two lights in DIFFERENT colours")
            frame = {}
            for button_id in self.multi:
                frame[('light', button_id, 0)] = GREEN
                frame[('light', button_id, 1)] = MAGENTA
            self.hold(frame, staging, 8, verify=self.caps.outcome_mask)
        else:
            why = "no control on this board owns more than one light"
            self.skip(4, "controls the board gives more than one light", why)
            self.skip(5, "one light of a control at a time", why)

        if self.case:
            self.phase(6, "a whole strip, by control name then by raw range",
                       "the strip white for 4s, then the same strip amber for 4s")
            self.note("by control name, one entry")
            self.hold({('button', 29): WHITE}, staging, 4)
            self.note(f"by raw LED range {self.case}")
            self.hold({('range', self.case[0], self.case[1]): AMBER}, staging, 4)
        else:
            self.skip(6, "a whole strip, two ways", "this board reports no case strip")

        if self.multi:
            control = self.multi[0]
            name = hlp.control_name(control)
            self.phase(7, "precedence between the ways of reaching a light",
                       f"{name} both red, then both green, then one green one blue")
            self.note("control name only")
            self.hold({('button', control): RED}, staging, 3)
            self.note("control name in green")
            self.hold({('button', control): GREEN}, staging, 3)
            self.note(f"control name green, plus {name}[1] explicitly blue - explicit wins")
            self.hold({('button', control): GREEN, ('light', control, 1): BLUE}, staging, 3)
        else:
            self.skip(7, "precedence between the ways of reaching a light",
                      "needs a control owning more than one light")

        ramp_control = self.singles[0] if self.singles else self.controls[0]
        self.phase(8, "brightness following the game's light level",
                   f"{hlp.control_name(ramp_control)} fading from off to full red")
        ramp = {'x': (('button', ramp_control), RED)}
        ramp_staging = bridge.staging_for(ramp, self.caps)
        self.hold(profile_module.resolve_frame({'x': 0.0}, ramp), ramp_staging, 0)
        for step in range(31):
            bridge.send_frame(self.device, profile_module.resolve_frame({'x': step / 30.0}, ramp),
                              staging=ramp_staging)
            time.sleep(0.2)

        if len(self.controls) >= 2:
            first, second = self.controls[0], self.controls[1]
            self.phase(9, "a control leaving the frame",
                       f"{hlp.control_name(first)} and {hlp.control_name(second)} red, then "
                       f"{hlp.control_name(first)} must go DARK")
            self.hold({('button', first): RED, ('button', second): RED}, staging, 3)
            self.note("one control leaves the frame, so staging is cleared first")
            self.hold({('button', second): RED}, staging, 6)
        else:
            self.skip(9, "a control leaving the frame", "this board has only one control")

        overlay = self.controls[:2]
        self.phase(10, "overlay mode",
                   "the board's own animation underneath, with "
                   f"{', '.join(hlp.control_name(b) for b in overlay)} white on top")
        self.device.request_ok(hlp.CMD_RELEASE)
        time.sleep(1)   # let the board's animation resume, so it is visibly underneath
        self.device.request_ok(hlp.CMD_SET_MODE, bytes([1, 0x10, 0x27, 1]))
        self.last_frame = None
        self.hold({('button', button_id): WHITE for button_id in overlay}, staging, 8)

        self.phase(11, "keepalive, then release",
                   "one control amber, held steady past the keepalive timeout, "
                   "then animations return")
        self.device.request_ok(hlp.CMD_RELEASE)
        time.sleep(0.5)
        # a deliberately short keepalive, so holding for 8s proves it is being fed
        self.device.request_ok(hlp.CMD_SET_MODE, bytes([0, 0xD0, 0x07, 1]))
        steady = self.singles[0] if self.singles else self.controls[0]
        self.note(f"keepalive timeout 2000 ms; holding {hlp.control_name(steady)} amber for 8s")
        self.last_frame = None
        self.hold({('button', steady): AMBER}, staging, 8)


def main(argv=None) -> int:
    """Run the sweep against an attached board.

    :param argv: optional board-ID prefix as the only argument
    :return: process exit status, non-zero if the board objected to anything
    """
    prefix = (argv or sys.argv[1:] or [''])[0]
    device = hlp.open_device(prefix)
    sweep = None
    try:
        caps = hlp.negotiate(device)
        print(caps.summary())
        if not caps.light_table:
            print("this board reports no light table, so most of the sweep has nothing"
                  " to address; run tests.hardware_probe to see what it does report")
            return 1
        sweep = Sweep(device, caps)
        print(f"{len(sweep.controls)} controls, {len(caps.lights)} lights, "
              f"{len(sweep.multi)} control(s) owning more than one light")
        sweep.run()
    finally:
        try:
            device.request_ok(hlp.CMD_RELEASE)
            print("\nreleased, on-board animations restored")
        except hlp.HostLightingError as error:
            print(f"\nrelease failed: {error}")
        device.close()

    failures = sweep.failures if sweep else []
    print(f"\n{len(failures)} protocol-level failure(s)"
          + (":" if failures else " - everything the board reported was as expected"))
    for message in failures:
        print(f"   {message}")
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
