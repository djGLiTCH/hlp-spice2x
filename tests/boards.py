"""Boards for the tests, on gp2040ce-hostlighting's ModelBoard.

ModelBoard answers as the firmware does and follows the protocol's timing,
claim, events and change counters on a clock the test controls. Board wires
one from a description in the bridge's own terms. Each light is named by the
control or kind of light it belongs to, with its first LED, and each control
by its key and action. The pages, the Summary and the state token follow from
that.

The HELLO attributes, `drop`, `reject` and `hooks` are ModelBoard's.
on_clock() runs the bridge and the library on the board's clock, so that a
wait takes no real time.

SPDX-FileCopyrightText: (C) 2026 Jacob Simpson
SPDX-License-Identifier: GPL-3.0-or-later
"""
from types import SimpleNamespace

from gp2040ce_hostlighting import claim, protocol, session
from gp2040ce_hostlighting.protocol import ACTION_NONE, ADDRESSING_ACTION, ADDRESSING_KIND, KIND_BUTTON, PIN_NONE
from gp2040ce_hostlighting.testing import ModelBoard, control_record, fake_device, light_record, summary_record

from hlp_spice2x import bridge, hlp

# The reference board as it reports itself on HLP 2.0, a Haute42 COSMOX M Ultra.
# It has 46 lights. Up owns ordinals 3 and 12, L3 owns 13 and 15, and the case
# strip has 30 lights.
M_ULTRA_LIGHTS = [('Left', 0), ('Down', 1), ('Right', 2), ('Up', 3), ('B3', 4), ('B4', 5),
                  ('R1', 6), ('L1', 7), ('B1', 8), ('B2', 9), ('R2', 10), ('L2', 11),
                  ('Up', 12), ('L3', 13), ('R3', 14), ('L3', 15)] + [('CASE', led)
                                                                     for led in range(16, 46)]

# The same board's Controls page at its default profile, as (key, action): 21
# pins, Up on GP2 and GP27, L3 on GP18 and GP26, turbo (action 32) on GP14, and
# S1, S2, A1 and A2 wired but unlit.
M_ULTRA_CONTROLS = [(2, 1), (3, 2), (4, 4), (5, 3), (6, 5), (7, 6), (8, 12), (9, 11), (10, 7),
                    (11, 8), (12, 10), (13, 9), (14, 32), (16, 13), (17, 14), (18, 17), (19, 18),
                    (20, 15), (21, 16), (26, 17), (27, 1)]

# A board with one light per control and no duplicates, for tests that do not
# care about the shape of the hardware.
PLAIN_LIGHTS = [(name, led) for led, name in enumerate(hlp.ACTION_NAMES[:16])]

# another application's claim token and name, as ModelBoard.take_over() uses them by default
OTHER_TOKEN = 0xB2
OTHER_NAME = 'Other'

# the HELLO attributes a Board takes, as ModelBoard names them
HELLO_ATTRIBUTES = ('magic', 'version', 'request_flags', 'commands', 'pages', 'addressing_modes',
                    'pixel_formats', 'events')


def on_clock(monkeypatch, board):
    """Run the bridge and the library on a board's clock for one test.

    A wait for a reply, a sleep between ticks and every timeout then move the
    board's clock, and take no real time.

    :param monkeypatch: the test's pytest monkeypatch fixture
    :param board: the ModelBoard whose clock to run on
    :return: the board
    """
    clock = SimpleNamespace(monotonic=board.time, perf_counter=board.time, sleep=board.sleep)
    for module in (bridge, claim, protocol, session):
        monkeypatch.setattr(module, 'time', clock)
    return board


class ScriptedConnection:
    """A spice2x connection that answers a scripted run of polls, then ends the run.

    Ending it the way Ctrl-C does exercises the loop's own shutdown path rather
    than a special case built for tests.
    """

    def __init__(self, *polls):
        """Hold what each poll reports.

        :param polls: one entry per poll: the light states, or a callable
            returning them, which may first do something to the board
        """
        self.script = list(polls) or [{}]
        self.polls = 0

    def lights_read(self):
        """Report the next poll's states, or interrupt the loop once they run out."""
        if self.polls >= len(self.script):
            raise KeyboardInterrupt
        step = self.script[self.polls]
        self.polls += 1
        return dict(step() if callable(step) else step)


class OneShotConnection(ScriptedConnection):
    """A spice2x connection that answers one poll and then ends the run."""

    def __init__(self, states=None):
        """Hold the light states this connection will report once."""
        super().__init__(states or {})


class Board(ModelBoard):
    """A ModelBoard wired from lights and controls named as a profile names them."""

    def __init__(self, lights=PLAIN_LIGHTS, controls=None, colour_format=0, white_channel=None,
                 render_hz=40, led_extent=None, led_limit=100, label='Fake Board',
                 firmware='v0.0.0-fake', board_id='0011223344556677', **hello):
        """Describe the board and what its firmware offers.

        :param lights: the board's lights in ordinal order, as (name, first LED)
            or (name, first LED, LED count), the name a profile control name:
            a control for a button light, or CASE, TURBO or PLED1-4
        :param controls: the board's inputs as (key, action), defaulting to one
            GPIO pin per control that owns a light
        :param colour_format: the Summary colour format
        :param white_channel: the Summary's white channel flag, by default set
            for GRBW and RGBW chains as the firmware sets it
        :param render_hz: the board's render rate, or 0 for "not stated"
        :param led_extent: the board's LED extent, defaulting to the end of its
            last light below the limit
        :param led_limit: the addressable LED limit
        :param label: the board label reported on page 0
        :param firmware: the firmware version reported on page 0
        :param board_id: the factory board ID as hex, reported by HELLO
        :param hello: ModelBoard's HELLO attributes, `drop` and `reject`, by name
        """
        super().__init__()
        for name, value in hello.items():
            if name not in HELLO_ATTRIBUTES + ('drop', 'reject'):
                raise TypeError(f"Board has no attribute {name!r} to set")
            setattr(self, name, dict(value) if name == 'reject' else
                    value if name in ('magic', 'version') else set(value))
        self.identity = [label, 'FakeBoard', 'rp2040', firmware, '0.0.0', 'fake', 'Release',
                         'GP2040-CE_0.0.0_FakeBoard']
        self.board_id = bytes.fromhex(board_id)
        self.colour_format = colour_format
        self.white_channel = colour_format in (2, 3) if white_channel is None else white_channel
        self.render_hz = render_hz
        self.limit = led_limit
        self.extent = led_extent
        self._wire(lights, controls)
        self.summary = [self._summary_group(), bytes(8)]
        # a board fresh on the bus: the pages hashed as they now are, the change counters at 0
        self.replug()

    @property
    def closed(self) -> bool:
        """Whether no handle has the board open."""
        return not self.opened

    def open(self):
        """Open the board as a HostLightingDevice through the library's real transport."""
        return fake_device(self)

    def _wire(self, lights, controls) -> None:
        """Turn the light and control descriptions into the pages' records."""
        wired = [tuple(light) + (1,) * (3 - len(light)) for light in lights]
        if controls is None:
            # one GPIO pin per control that owns a light, from GP2 up
            actions = []
            for name, _, _ in wired:
                target = hlp.CONTROL_TARGETS[name.upper()]
                if target[0] == ADDRESSING_ACTION and target[1] not in actions:
                    actions.append(target[1])
            controls = [(2 + n, action) for n, action in enumerate(actions)]
        self.wiring = [tuple(control) for control in controls]
        self.light_wiring = []
        used = {}
        for name, first, count in wired:
            addressing, value = hlp.CONTROL_TARGETS[name.upper()]
            light = {'first': first, 'count': count, 'kind': KIND_BUTTON, 'action': None, 'pin': None}
            if addressing == ADDRESSING_KIND:
                light['kind'] = value
            else:
                # The nth light of an action is tied to the nth GPIO pin or Hall-effect
                # trigger carrying it. An expander pin owns no light.
                pins = [key for key, action in self.wiring if action == value and key < 0xC0]
                nth = used.get(value, 0)
                used[value] = nth + 1
                light['action'] = value
                light['pin'] = pins[min(nth, len(pins) - 1)] if pins else None
            self.light_wiring.append(light)
        self._records()

    def _records(self) -> None:
        """Build the Lights and Controls pages' records from the wiring."""
        self.lights = [light_record(light['first'], light['count'], kind=light['kind'],
                                    pin=PIN_NONE if light['pin'] is None else light['pin'],
                                    action=ACTION_NONE if light['action'] is None else light['action'])
                       for light in self.light_wiring]
        self.controls = [control_record(key, action, duplicated=int(self._actions().count(action) > 1),
                                        lit=int(self._lit(key)), light_count=int(self._lit(key)))
                         for key, action in self.wiring]

    def _actions(self) -> list:
        """List each control's action, in page order."""
        return [action for _, action in self.wiring]

    def _lit(self, key) -> bool:
        """Say whether a control's key owns a light."""
        return any(light['pin'] == key for light in self.light_wiring)

    def _summary_group(self) -> bytes:
        """Build the Summary group these pages give."""
        extent = self.extent
        if extent is None:
            extent = max((min(light['first'] + light['count'], self.limit) for light in self.light_wiring
                          if light['first'] < self.limit), default=0)
        lit = sum(1 for key, _ in self.wiring if self._lit(key))
        return summary_record(led_extent=extent, led_limit=self.limit, render_hz=self.render_hz,
                              colour_format=self.colour_format, white_channel=int(self.white_channel),
                              button_layout=27, button_layout_right=31, grid_width=17, grid_height=9,
                              profiles=len(self.profiles), controls=len(self.wiring), lit_controls=lit,
                              unlit_controls=len(self.wiring) - lit, lights=len(self.light_wiring),
                              animations=len(self.animations))[0]

    def rewire(self, lights=None, controls=None) -> None:
        """Change what the board has, as a profile switch or an LED setup change does.

        The state token changes, the change counters of the pages that differ
        step, and an event is raised, as ModelBoard.reconfigure() does.

        :param lights: the new lights, as Board takes them, or None to keep them
        :param controls: the new controls, as (key, action), or None to keep them
        """
        if lights is not None:
            self._wire(lights, controls if controls is not None else self.wiring)
        else:
            # a light is tied to its pin, so it takes whatever action the pin now carries
            self.wiring = [tuple(control) for control in controls]
            actions = dict(self.wiring)
            for light in self.light_wiring:
                if light['pin'] is not None:
                    light['action'] = actions.get(light['pin'])
            self._records()
        self.summary = [self._summary_group(), self.summary[1]]
        self.reconfigure(lights=self.lights, controls=self.controls)
