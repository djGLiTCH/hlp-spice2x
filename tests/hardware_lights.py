# hlp-spice2x (spice2x to GP2040-CE Host Lighting bridge)
# Filename: hardware_lights.py
# Folder: tests
# https://github.com/djGLiTCH/hlp-spice2x
# SPDX-FileCopyrightText: (C) 2026 Jacob Simpson (DJ GLiTCH)
# SPDX-License-Identifier: GPL-3.0-or-later
# See LICENSES/GPL-3.0-or-later.txt at the repository root.

"""Walk a board through every way this bridge can light it, one phase at a time.

A functional check to run by eye after changing anything that touches staging.
Each phase announces what should be visible before it starts, holds it long
enough to look at, and moves on. It takes about a hundred seconds.

Everything goes through the bridge's staging and gp2040ce-hostlighting's
claim. Every control it uses is taken from what the board reports. A phase
with nothing to show on a given board says so and is skipped.

The eleven phases, and what each tests:

    1   every control in turn        that each control name resolves on its own
    2   every light in turn          that every ordinal addresses the light it
                                     claims to, with the lights of a control
                                     shown one at a time
    3   every control at once        that a full frame stages without collisions
    4   multi-light controls         that a bare control name reaches all of its
                                     lights
    5   one light of a control       that an index picks out the light it names
    6   a whole strip, two ways      by control name, then by raw LED range
    7   precedence                   that naming one light beats its control's
                                     name, even when it asks for black
    8   brightness                   that a light level scales the colour smoothly
    9   a control leaving the frame  that staging is cleared so it goes dark
    10  overlay mode                 that the board's own animation shows through
    11  keepalive and hand back      that the takeover survives without new
                                     frames, and that giving the claim up
                                     restores the animations

Claims the board's lights for the duration and gives them up at the end. It
stops at once if another application holds them. It also reports an
unacknowledged or refused frame, so a failure shows without watching the LEDs.

Run it from the repository root:

    python -m tests.hardware_lights
"""
import logging
import sys
import time

from gp2040ce_hostlighting import protocol as hl
from gp2040ce_hostlighting.claim import ClaimState, LightsClaim

from hlp_spice2x import bridge, hlp
from hlp_spice2x import profile as profile_module

RED = (255, 0, 0)
GREEN = (0, 255, 0)
BLUE = (0, 0, 255)
WHITE = (255, 255, 255)
AMBER = (255, 160, 0)
MAGENTA = (255, 0, 255)
OFF = (0, 0, 0)
CASE = hlp.CONTROL_TARGETS['CASE']


def named_controls(caps) -> list:
    """List the controls a profile can name that this board has a light for.

    :param caps: the board's capabilities
    :return: (addressing, address) pairs, in the order their first light appears
    """
    controls = []
    for record in caps.lights:
        if record.kind.value == hl.KIND_BUTTON:
            control = (hl.ADDRESSING_ACTION, record.owner.action)
        else:
            control = (hl.ADDRESSING_KIND, record.kind.value)
        if control in hlp.CONTROL_NAMES and control not in controls and caps.lights_of(control):
            controls.append(control)
    return controls


class LightsCheck:
    """Runs the phases against one board, tracking what the board objected to."""

    def __init__(self, device, caps):
        """Work out what the board can demonstrate.

        :param device: an opened HostLightingDevice
        :param caps: the board's capabilities
        """
        self.device = device
        self.caps = caps
        self.started = time.monotonic()
        self.failures = []
        self.last_frame = None
        # whole-frame takeover, 10 s keepalive, honour the board's brightness
        self.claim = LightsClaim(device, bridge.CLAIM_NAME, hl.Settings(keepalive_ms=10000))

        self.controls = named_controls(caps)
        self.singles = [control for control in self.controls
                        if control[0] == hl.ADDRESSING_ACTION]
        # controls the board gives more than one light, which is what makes
        # per-light targeting worth demonstrating at all
        self.multi = [control for control in self.singles if len(caps.lights_of(control)) > 1]
        self.case = caps.lights_of(CASE)

    def stamp(self) -> str:
        """Seconds since the lights check began, for lining up with what was seen."""
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

    def configure(self, overlay, timeout_ms) -> None:
        """Send CONFIGURE again with a new takeover mode and timeout.

        The staged frame survives a CONFIGURE, so the next frame clears first.
        The claim is renewed first. A shorter timeout applies to the lease
        already running, which would lapse if its last renewal were older.
        """
        self.claim.configure(hl.Settings(
            takeover=hl.TAKEOVER_OVERLAY if overlay else hl.TAKEOVER_WHOLE_FRAME, keepalive_ms=timeout_ms))
        self.last_frame = {}

    def wait(self, seconds) -> None:
        """Let time pass, renewing the claim as the streaming loop does."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            time.sleep(0.1)
            try:
                self.claim.maintain()
            except hl.HostLightingTimeoutError:
                pass  # worth sending, not worth insisting on
            if self.claim.state is not ClaimState.HELD:
                raise hl.HostLightingError("another application took the lights over")

    def hold(self, frame, staging, seconds) -> None:
        """Publish a frame and hold it, clearing whatever left the frame.

        Works out the clear the way the streaming loop does. Without it every
        earlier phase would still be lit underneath the current one, because the
        staged frame is kept while the takeover lasts.
        """
        departed = self.last_frame is not None and bool(set(self.last_frame) - set(frame)
                                                        or not self.last_frame)
        self.last_frame = dict(frame)
        try:
            result = bridge.send_frame(self.device, frame, staging, clear_first=departed)
        except hl.HostLightingRejectedError as error:
            self.fail(f"frame refused: {error}")
        else:
            if not result.acknowledged:
                self.fail("frame went unacknowledged")
        self.wait(seconds)

    def build_profile(self) -> dict:
        """Describe every target the lights check uses, so staging_for can resolve them.

        A staging map is built once from a profile, since what a target
        resolves to depends on the board and not on the frame.
        """
        profile = {f"c{control}": (('button', control), WHITE) for control in self.controls}
        for control in self.controls:
            for index in range(len(self.caps.lights_of(control))):
                profile[f"l{control}.{index}"] = (('light', control, index), WHITE)
        if self.case:
            first = min(record.leds.first_led for record in self.case)
            last = max(record.leds.first_led + record.leds.led_count for record in self.case)
            profile['case'] = (('range', first, last - first), AMBER)
        return profile

    def run(self) -> None:
        """Run every phase this board can demonstrate."""
        profile = self.build_profile()
        staging = bridge.staging_for(profile, self.caps)
        self.claim.take()

        self.phase(1, "every control in turn",
                   "each control lit white on its own, in the board's own order")
        for control in self.controls:
            self.note(hlp.control_name(control))
            self.hold({('button', control): WHITE}, staging, 0.6)

        self.phase(2, "every light in turn, by its own ordinal",
                   "each individual light alone, so a control with two shows them "
                   "one at a time")
        ordered = sorted((record.ordinal, control, index) for control in self.controls
                         for index, record in enumerate(self.caps.lights_of(control)))
        self.note(f"{len(ordered)} lights, 0.4s each")
        for ordinal, control, index in ordered:
            self.note(f"ordinal {ordinal}: {hlp.control_name(control)}[{index}]")
            self.hold({('light', control, index): WHITE}, staging, 0.4)

        self.phase(3, "every control at once",
                   "the whole board lit white together, nothing missing or flickering")
        self.hold({('button', control): WHITE for control in self.controls}, staging, 5)

        if self.multi:
            named = ', '.join(hlp.control_name(control) for control in self.multi)
            self.phase(4, "controls the board gives more than one light",
                       f"{named} red, BOTH lights of each, everything else dark")
            self.hold({('button', control): RED for control in self.multi}, staging, 6)

            self.phase(5, "one light of a control at a time",
                       f"{named}: their two lights in DIFFERENT colours")
            frame = {}
            for control in self.multi:
                frame[('light', control, 0)] = GREEN
                frame[('light', control, 1)] = MAGENTA
            self.hold(frame, staging, 8)
        else:
            why = "no control on this board has more than one light"
            self.skip(4, "controls the board gives more than one light", why)
            self.skip(5, "one light of a control at a time", why)

        if self.case:
            target = profile['case'][0]
            self.phase(6, "a whole strip, by control name then by raw range",
                       "the strip white for 4s, then the same strip amber for 4s")
            self.note("by control name, one entry")
            self.hold({('button', CASE): WHITE}, staging, 4)
            self.note(f"by raw LED range [{target[1]}, {target[2]}]")
            self.hold({target: AMBER}, staging, 4)
        else:
            self.skip(6, "a whole strip, two ways", "this board reports no case lights")

        if self.multi:
            control = self.multi[0]
            name = hlp.control_name(control)
            one = ('light', control, 1)
            self.phase(7, "precedence between the ways of reaching a light",
                       f"{name} both red, then one red one cyan, then one red one dark, "
                       f"then both red again")
            self.note("control name only")
            self.hold({('button', control): RED}, staging, 3)
            self.note(f"control name red, plus {name}[1] cyan, where naming one light wins")
            self.hold({('button', control): RED, one: (0, 255, 255)}, staging, 3)
            self.note(f"control name red, plus {name}[1] black, which wins although it asks for off")
            self.hold({('button', control): RED, one: OFF}, staging, 3)
            self.note("control name only again, since the staged frame was cleared when the entry left")
            self.hold({('button', control): RED}, staging, 3)
        else:
            self.skip(7, "precedence between the ways of reaching a light",
                      "needs a control with more than one light")

        ramp_control = self.singles[0] if self.singles else self.controls[0]
        self.phase(8, "brightness following the game's light level",
                   f"{hlp.control_name(ramp_control)} fading from off to full red")
        ramp = {'x': (('button', ramp_control), RED)}
        ramp_staging = bridge.staging_for(ramp, self.caps)
        self.hold(profile_module.resolve_frame({'x': 0.0}, ramp), ramp_staging, 0)
        for step in range(31):
            if not bridge.send_frame(self.device, profile_module.resolve_frame({'x': step / 30.0}, ramp),
                                     ramp_staging).acknowledged:
                self.fail(f"ramp frame {step} was not acknowledged")
            time.sleep(0.2)

        if len(self.controls) >= 2:
            first, second = self.controls[0], self.controls[1]
            self.phase(9, "a control leaving the frame",
                       f"{hlp.control_name(first)} and {hlp.control_name(second)} red, then "
                       f"{hlp.control_name(first)} must go DARK")
            self.hold({('button', first): RED, ('button', second): RED}, staging, 3)
            self.note("one control leaves the frame, so the staged frame is cleared first")
            self.hold({('button', second): RED}, staging, 6)
        else:
            self.skip(9, "a control leaving the frame", "this board has only one control")

        overlay = self.controls[:2]
        self.phase(10, "overlay mode",
                   "the board's own animation underneath, with "
                   f"{', '.join(hlp.control_name(control) for control in overlay)} white on top")
        self.configure(True, 10000)
        self.hold({('button', control): WHITE for control in overlay}, staging, 8)

        self.phase(11, "keepalive, then hand back",
                   "one control amber, held steady past the keepalive timeout, "
                   "then animations return")
        # a deliberately short keepalive, so holding for 8s proves the claim is being renewed
        self.configure(False, 2000)
        steady = self.singles[0] if self.singles else self.controls[0]
        self.note(f"keepalive timeout 2000 ms, holding {hlp.control_name(steady)} amber for 8s")
        self.hold({('button', steady): AMBER}, staging, 8)
        state = self.device.read_record(hl.PAGE_STATE, hlp.STATE_GROUPS).records[0]
        if not state.state.takeover_live or not state.controller.held_by(self.claim.token):
            self.fail("the takeover or the claim did not survive the hold")


def main(argv=None) -> int:
    """Run the lights check against an attached board.

    :param argv: optional board-ID prefix as the only argument
    :return: process exit status, non-zero if the board objected to anything
    """
    prefix = (argv or sys.argv[1:] or [''])[0]
    # the library's claim lines, the give up among them, are printed with the check's own
    library = logging.getLogger('gp2040ce_hostlighting')
    lines = bridge._ReportLines(print)
    library.addHandler(lines)
    library.setLevel(logging.INFO)
    device = hlp.open_device(prefix)
    check = None
    try:
        caps = hlp.negotiate(device, (hl.ADDRESSING_LED_RUN, hl.ADDRESSING_LIGHT,
                                      hl.ADDRESSING_ACTION, hl.ADDRESSING_KIND))
        print(caps.summary_line())
        check = LightsCheck(device, caps)
        if not check.controls:
            print("this board reports no light a profile can name, so the lights check has nothing"
                  " to address. Run tests.hardware_probe to see what it reports.")
            return 1
        print(f"{len(check.controls)} controls, {len(caps.lights)} lights, "
              f"{len(check.multi)} control(s) with more than one light")
        check.run()
    finally:
        if check is not None:
            check.claim.give_up()
        device.close()
        library.removeHandler(lines)

    failures = check.failures if check else []
    print(f"\n{len(failures)} protocol-level failure(s)"
          + (":" if failures else ", and everything the board reported was as expected"))
    for message in failures:
        print(f"   {message}")
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
