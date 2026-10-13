"""A fake Host Lighting 2.0 interface, parameterised by what the board offers.

Sits where hidapi sits, so requests travel through the real transport, with
the same framing, sequence handling and reply matching that runs against a
real board.

Hardware and firmware are described separately. `lights` and `controls`
describe what the board has wired. The capability sets (`commands`, `pages`,
`flags`, `addressing`, `pixel_formats`, `events`) are what its firmware lists
in HELLO, and the fake answers by them as firmware does. A command it does not
list is status 1, and a page it does not list is an invalid argument.

It keeps the state the protocol gives a board:

* the staged frame and the frame shown
* the takeover and its number
* the claim's holder and generation
* the CONFIGURE settings and the subscription
* a state token with a change counter for each configuration page

It keeps no clock. A lease lapses when a test says so.

Modelled on FakeBoard and ModelBoard in gp2040ce-binary-tools
(tests/hostlighting_fakes.py).

SPDX-FileCopyrightText: (C) 2026 Jacob Simpson
SPDX-License-Identifier: GPL-3.0-or-later
"""
import struct
import zlib

from hlp_spice2x import hlp

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

ALL_COMMANDS = frozenset({0x01, 0x02, 0x03, 0x04, 0x05, 0x10, 0x11, 0x12, 0x13, 0x20, 0x21,
                          0x30, 0x31, 0x32, 0x33, 0x70, 0x71})
ALL_PAGES = frozenset({0, 1, 2, 8, 9, 10, 11, 12})
ALL_FLAGS = frozenset(range(7))
ALL_ADDRESSING = frozenset({0, 1, 4, 5, 6, 7})
ALL_PIXEL_FORMATS = frozenset({0, 1, 2})
ALL_EVENTS = frozenset({0})

# another application's claim token and name, for the tests that share a board
OTHER_TOKEN = 0xB2
OTHER_NAME = 'Other'


def attach(monkeypatch, *boards, other_majors=()):
    """Make device discovery find these fake boards, as enumerating hidapi would.

    Patches the enumeration and the HostLightingDevice constructor for one
    test, so open_device and describe_boards run unchanged against the fakes.

    :param monkeypatch: the test's pytest monkeypatch fixture
    :param boards: the FakeBoards to present, in enumeration order
    :param other_majors: usages to report on the usage page beside them
    """
    real = hlp.HostLightingDevice
    paths = {f'fake-{n}'.encode(): board for n, board in enumerate(boards)}
    monkeypatch.setattr(hlp, 'find_devices', lambda: [{'path': path} for path in paths])
    monkeypatch.setattr(hlp, 'find_other_majors', lambda: list(other_majors))
    monkeypatch.setattr(hlp, 'HostLightingDevice', lambda path: real.from_hid(paths[path]))


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


def page_reply(page: int, records: list, widths: tuple, start: int = 0, mask: int = 0,
               token: int = 1, status: int = hlp.STATUS_OK) -> bytearray:
    """Build a GET_PAGE reply as the firmware does, with groups in bit order while a record fits.

    :param page: the page number to echo
    :param records: every record of the page, each a list of its groups' bytes
    :param widths: the page's group widths, in bit order
    :param start: the start record to echo
    :param mask: the field mask asked for, or 0 for every group
    :param token: the state token
    :param status: 0, or 3 for a reply with no records and no groups
    :return: the reply, its command and sequence bytes left for the caller
    """
    wanted = (mask or (1 << len(widths)) - 1) if status == hlp.STATUS_OK else 0
    groups = stride = 0
    for group, width in enumerate(widths):
        if wanted & (1 << group) and stride + width <= hlp.RECORDS_AREA:
            groups |= 1 << group
            stride += width
    total = len(records) if status == hlp.STATUS_OK else 0
    count = max(0, min(hlp.RECORDS_AREA // stride, total - start)) if stride else 0
    body = b''.join(b''.join(record[group] for group in range(len(widths)) if groups & (1 << group))
                    for record in records[start:start + count])
    reply = bytearray(hlp.REPORT_SIZE)
    reply[2], reply[3] = status, page
    reply[4:12] = struct.pack('<HHBBH', start, total, count, stride, groups)
    reply[12:12 + len(body)] = body
    reply[60:64] = struct.pack('<I', token)
    return reply


class FakeBoard:
    """Answer Host Lighting 2.0 requests the way a board would."""

    def __init__(self, lights=PLAIN_LIGHTS, controls=None, colour_format=0, render_hz=40,
                 led_extent=None, led_limit=100, label='Fake Board', firmware='v0.0.0-fake',
                 board_id='0011223344556677', version=(2, 0), magic=b'GPHL',
                 commands=ALL_COMMANDS, pages=ALL_PAGES, flags=ALL_FLAGS,
                 addressing=ALL_ADDRESSING, pixel_formats=ALL_PIXEL_FORMATS, events=ALL_EVENTS,
                 drop=(), reject=()):
        """Describe the board and what its firmware offers.

        :param lights: the board's lights in ordinal order, as (name, first LED)
            or (name, first LED, LED count), the name a profile control name:
            a control for a button light, or CASE, TURBO or PLED1-4
        :param controls: the board's inputs as (key, action), defaulting to one
            GPIO pin per control that owns a light
        :param colour_format: the Summary colour format, where 2 and 3 have a white channel
        :param render_hz: the board's render rate, or 0 for "not stated"
        :param led_extent: the board's LED extent, defaulting to the end of its last light
        :param led_limit: the addressable LED limit
        :param label: the board label reported on page 0
        :param firmware: the firmware version reported on page 0
        :param board_id: the factory board ID as hex, reported by HELLO
        :param version: the (major, minor) HELLO reports
        :param magic: the HELLO magic, for standing in as something that is not
            a Host Lighting interface at all
        :param commands: the commands HELLO lists, where any other answers status 1
        :param pages: the pages HELLO lists, where any other is an invalid argument
        :param flags: the request flag bits HELLO lists
        :param addressing: the addressing modes HELLO lists
        :param pixel_formats: the pixel formats HELLO lists
        :param events: the events HELLO lists
        :param drop: commands to discard unanswered, as a full reply queue would
        :param reject: commands to answer with a status other than 0
        """
        self.version = tuple(version)
        self.magic = magic
        self.board_id = board_id
        self.label = label
        self.firmware = firmware
        self.colour_format = colour_format
        self.render_hz = render_hz
        self.led_limit = led_limit
        self.commands = set(commands)
        self.pages = set(pages)
        self.flags = set(flags)
        self.addressing = set(addressing)
        self.pixel_formats = set(pixel_formats)
        self.events = set(events)
        # mutable so a test can start dropping or rejecting mid-run
        self.drop = set(drop)
        self.reject = set(reject)
        # called with (board, request) before each request is answered, so a
        # test can change the board at an exact point in a conversation
        self.hooks = []
        self.requests = []
        self.outbox = []
        self.closed = False
        self.gone = False
        # the requests written while the board was off the bus
        self.refused = []

        self.changes = bytearray(8)
        self.lights = self.controls = []
        self._extent = led_extent
        self._wire(lights, controls)
        self.token = self._hash()

        self.staged = {}
        self.shown = {}
        # every frame published, oldest first, as {LED: pixel as it was sent}
        self.frames = []
        self.live = False
        self.number = 0
        self.holder, self.holder_name, self.holder_flags = 0, '', 0
        self.generation = 1
        self.settings = (0, 2000, 1)
        self.subscribed = 0
        self._seen = (self._state_bytes(), self.token)

    # -- describing the board ----------------------------------------------------

    def _wire(self, lights, controls) -> None:
        """Turn the light and control descriptions into what the pages report."""
        wired = [tuple(light) + (1,) * (3 - len(light)) for light in lights]
        if controls is None:
            # one GPIO pin per control that owns a light, from GP2 up
            actions = []
            for name, _, _ in wired:
                target = hlp.CONTROL_TARGETS[name.upper()]
                if target[0] == hlp.ADDRESSING_ACTION and target[1] not in actions:
                    actions.append(target[1])
            controls = [(2 + n, action) for n, action in enumerate(actions)]
        self.controls = [tuple(control) for control in controls]
        self.lights = []
        used = {}
        for name, first, count in wired:
            addressing, value = hlp.CONTROL_TARGETS[name.upper()]
            light = {'first': first, 'count': count, 'kind': hlp.KIND_BUTTON, 'action': None,
                     'pin': None}
            if addressing == hlp.ADDRESSING_KIND:
                light['kind'] = value
            else:
                # The nth light of an action is tied to the nth GPIO pin or Hall-effect
                # trigger carrying it. An expander pin owns no light.
                pins = [key for key, action in self.controls if action == value and key < 0xC0]
                nth = used.get(value, 0)
                used[value] = nth + 1
                light['action'] = value
                light['pin'] = pins[min(nth, len(pins) - 1)] if pins else None
            self.lights.append(light)

    @property
    def led_extent(self) -> int:
        """The highest LED index below the limit in use, plus one."""
        if self._extent is not None:
            return self._extent
        return max((min(light['first'] + light['count'], self.led_limit) for light in self.lights
                    if light['first'] < self.led_limit), default=0)

    def reconfigure(self, lights=None, controls=None) -> None:
        """Change what the board has, as a profile switch or an LED setup change does.

        The state token changes and the change counters of the pages that
        differ step, as page 8 reports them.

        :param lights: the new lights, or None to keep them
        :param controls: the new controls, or None to keep them
        """
        before = (self._page(hlp.PAGE_SUMMARY)[0][0], self._page(hlp.PAGE_CONTROLS),
                  self._page(hlp.PAGE_LIGHTS))
        if lights is not None:
            self._wire(lights, controls if controls is not None else self.controls)
        else:
            # a light is tied to its pin, so it takes whatever action the pin now carries
            self.controls = [tuple(control) for control in controls]
            actions = dict(self.controls)
            for light in self.lights:
                if light['pin'] is not None:
                    light['action'] = actions.get(light['pin'])
        after = (self._page(hlp.PAGE_SUMMARY)[0][0], self._page(hlp.PAGE_CONTROLS),
                 self._page(hlp.PAGE_LIGHTS))
        for page, was, now in zip((hlp.PAGE_SUMMARY, hlp.PAGE_CONTROLS, hlp.PAGE_LIGHTS),
                                  before, after):
            if was != now:
                index = page - hlp.PAGE_SUMMARY
                self.changes[index] = (self.changes[index] + 1) & 0xFF
        self.token = self._hash()
        self._note()

    def change_animation(self) -> None:
        """Change a page the bridge does not read, as an animation hotkey does.

        The state token changes and only page 12's change counter steps.
        """
        self.changes[4] = (self.changes[4] + 1) & 0xFF
        self.token = zlib.crc32(repr((self.token, self.changes[4])).encode()) or 1
        self._note()

    def _hash(self) -> int:
        """Compute the state token from what the configuration pages report."""
        pages = (self._page(hlp.PAGE_SUMMARY)[0][0], self._page(hlp.PAGE_CONTROLS),
                 self._page(hlp.PAGE_LIGHTS))
        return zlib.crc32(repr(pages).encode()) or 1

    # -- other applications ------------------------------------------------------

    def take_over(self, token=OTHER_TOKEN, name=OTHER_NAME, exclusive=False) -> None:
        """Have another application take the claim over."""
        self._set_holder(token, name, hlp.CLAIM_EXCLUSIVE if exclusive else 0)
        self._note()

    def let_go(self) -> None:
        """End the claim, as its holder giving it up or its lease lapsing does."""
        self._set_holder(0, '', 0)
        self._note()

    def end_takeover(self) -> None:
        """End the takeover with the claim still held, as its keepalive timeout does."""
        self.live = False
        self.staged, self.shown = {}, {}
        self._note()

    def unplug(self) -> None:
        """Take the board off the bus.

        A write then returns a negative count and a read raises, as hidapi's do.
        """
        self.gone = True

    def replug(self) -> None:
        """Put the board back on the bus in a new USB session, which holds no claim or frame."""
        self.gone = False
        self.outbox = []
        self.staged, self.shown = {}, {}
        self.live = False
        self.holder, self.holder_name, self.holder_flags = 0, '', 0
        self.generation = 1
        self.settings = (0, 2000, 1)
        self.subscribed = 0
        self._seen = (self._state_bytes(), self.token)

    # -- hidapi ------------------------------------------------------------------

    def open(self):
        """Wrap this board in a real HostLightingDevice.

        :return: a HostLightingDevice driving this board through the real transport
        """
        self.closed = False
        return hlp.HostLightingDevice.from_hid(self)

    def write(self, data):
        """Take one report as hidapi writes it and queue whatever reply it earns."""
        if self.gone:
            self.refused.append(bytes(data)[1:])
            return -1
        report = bytes(data)
        assert len(report) == hlp.REPORT_SIZE + 1 and report[0] == 0, 'report ID 0 then 64 bytes'
        request = report[1:]
        for hook in list(self.hooks):
            hook(self, request)
        self.requests.append(request)
        command, sequence, flags = request[0], request[1], request[2]
        if command in self.drop:
            return len(data)  # discarded, so neither executed nor answered
        reply = self._answer(command, flags, request)
        if reply is not None:
            if reply[2] == hlp.STATUS_OK and flags & hlp.FLAG_COMMIT_AFTER:
                self._publish()
            if not flags & hlp.FLAG_NO_REPLY:
                reply[0], reply[1] = command | hlp.REPLY_FLAG, sequence
                self.outbox.append(bytes(reply))
        self._note()
        return len(data)

    def read(self, size, timeout_ms=0):
        """Hand back the next queued report, or nothing at all."""
        if self.gone:
            raise OSError('read error')
        return list(self.outbox.pop(0)) if self.outbox else []

    def close(self):
        """Match the hidapi interface."""
        self.closed = True

    def sent(self, command) -> list:
        """Every request written for one command, for asserting on the path taken.

        :param command: the command byte to collect
        :return: the 64-byte requests written for that command, in order
        """
        return [request for request in self.requests if request[0] == command]

    # -- answering ---------------------------------------------------------------

    def _reply(self, status=hlp.STATUS_OK, payload=b'') -> bytearray:
        """Build a reply carrying the given status and payload from [3]."""
        reply = bytearray(hlp.REPORT_SIZE)
        reply[2] = status
        reply[3:3 + len(payload)] = payload
        return reply

    def _invalid(self, offset) -> bytearray:
        """Build an invalid-argument reply naming the request byte at fault."""
        return self._reply(hlp.STATUS_INVALID, bytes([offset]))

    def _controller(self) -> bytes:
        """Page 1's Controller group, which the CLAIM and status 4 replies carry too."""
        return (struct.pack('<I', self.holder) + self.holder_name.encode().ljust(16, b'\x00')
                + bytes([self.holder_flags, self.generation]))

    def _answer(self, command, flags, request):
        """Build the reply to one request, or None for a report the board ignores."""
        if command == 0 or command & hlp.REPLY_FLAG:
            return None
        if command in self.reject or command not in self.commands:
            return self._reply(hlp.STATUS_UNSUPPORTED)
        generation = (flags & hlp.FLAG_GENERATION_MASK) >> hlp.FLAG_GENERATION_SHIFT
        if command != hlp.CMD_CLAIM:
            # a generation that is neither 0 nor current is refused, reads included
            reads = command in (hlp.CMD_HELLO, hlp.CMD_GET_PAGE, hlp.CMD_SUBSCRIBE) and not (
                flags & (hlp.FLAG_COMMIT_AFTER | hlp.FLAG_KEEPALIVE))
            guarded = self.holder_flags & hlp.CLAIM_EXCLUSIVE and not (
                flags & 0x08 and generation == self.generation)
            if generation not in (0, self.generation) or (guarded and not reads):
                return self._reply(hlp.STATUS_CLAIMED, self._controller())
        handlers = {hlp.CMD_HELLO: self._hello, hlp.CMD_GET_PAGE: self._get_page,
                    hlp.CMD_CLAIM: self._claim, hlp.CMD_CONFIGURE: self._configure,
                    hlp.CMD_SUBSCRIBE: self._subscribe, hlp.CMD_STAGE: self._stage,
                    hlp.CMD_CLEAR: self._clear, hlp.CMD_COMMIT: self._commit,
                    hlp.CMD_RELEASE: self._release}
        if command in handlers:
            return handlers[command](request)
        return self._reply()

    def _hello(self, request):
        """Answer HELLO with this board's identity and capability bitmaps."""
        def bitmap(members, size):
            return sum(1 << member for member in members).to_bytes(size, 'little')

        body = (self.magic + bytes(self.version) + bytes.fromhex(self.board_id) + bytes([16])
                + bitmap(self.flags, 1) + bitmap(self.commands, 16) + bitmap(self.pages, 4)
                + bitmap(self.addressing, 2) + bitmap(self.pixel_formats, 2)
                + bitmap(self.events, 2) + struct.pack('<H', self.subscribed))
        return self._reply(payload=body)

    def _state_bytes(self) -> list:
        """Page 1's one record, as its three groups."""
        return [bytes([int(self.live), 0, 1, 1, 0, 0, 5, self.number, 5]), self._controller(),
                struct.pack('<BHB', *self.settings)]

    def _page(self, page) -> tuple:
        """Build one table page's records and group widths."""
        widths = hlp.PAGE_GROUPS[page]
        if page == hlp.PAGE_STATE:
            return [self._state_bytes()], widths
        if page == hlp.PAGE_SUMMARY:
            lit = sum(1 for key, _ in self.controls
                      if any(light['pin'] == key for light in self.lights))
            summary = struct.pack('<HHHBBBBHHHHHHHHBB', self.led_extent, self.led_limit,
                                  self.render_hz, self.colour_format, 255, 10, 10, 17, 9, 1,
                                  len(self.controls), lit, len(self.controls) - lit,
                                  len(self.lights), 0, 27, 31)
            return [[summary, bytes(self.changes)]], widths
        if page == hlp.PAGE_CONTROLS:
            records = []
            for key, action in self.controls:
                lit = any(light['pin'] == key for light in self.lights)
                duplicated = sum(1 for _, other in self.controls if other == action) > 1
                records.append([struct.pack('<BhB', key, action, int(duplicated) | lit << 1),
                                bytes(4), bytes(6), bytes(4)])
            return records, widths
        records = []
        for light in self.lights:
            action = hlp.ACTION_NONE if light['action'] is None else light['action']
            pin = hlp.NONE_8 if light['pin'] is None else light['pin']
            records.append([struct.pack('<HH', light['first'], light['count']),
                            bytes([light['kind'], 0]), struct.pack('<Bh', pin, action),
                            bytes(4), bytes(2), bytes([hlp.NONE_8])])
        return records, widths

    def _get_page(self, request):
        """Answer GET_PAGE, with the expected-token check and the field mask."""
        page, start, token, mask = struct.unpack_from('<BHIH', request, 3)
        if page not in self.pages:
            return self._invalid(3)
        if token and token != self.token:
            return page_reply(page, [], (), start, mask, self.token, hlp.STATUS_STALE)
        if page == hlp.PAGE_IDENTITY:
            return self._identity(mask)
        if page not in hlp.PAGE_GROUPS:
            return page_reply(page, [], (1,), start, mask, self.token)
        records, widths = self._page(page)
        return page_reply(page, records, widths, start, mask, self.token)

    def _identity(self, mask):
        """Answer page 0 with the strings asked for, in bit order, while they fit."""
        strings = [self.label, 'FakeBoard', 'rp2040', self.firmware, '0.0.0', 'fake', 'Release',
                   'GP2040-CE_0.0.0_FakeBoard']
        area = b''
        groups = 0
        for group, text in enumerate(strings):
            if not (mask or 0xFF) & (1 << group):
                continue
            raw = text.encode()[:hlp.IDENTITY_STRING_MAX]
            piece = raw + (b'\x00' if len(raw) < hlp.IDENTITY_STRING_MAX else b'')
            if len(area) + len(piece) > hlp.RECORDS_AREA:
                continue
            area += piece
            groups |= 1 << group
        reply = self._reply()
        reply[3:12] = struct.pack('<BHHBBH', hlp.PAGE_IDENTITY, 0, 1, 1, len(area), groups)
        reply[12:12 + len(area)] = area
        reply[60:64] = struct.pack('<I', self.token)
        return reply

    def _set_holder(self, token, name, flags) -> None:
        """Record the holder. A change of holder ends the takeover and resets the settings."""
        if token != self.holder:
            self.live = False
            self.staged, self.shown = {}, {}
            self.settings = (0, 2000, 1)
            self.generation = self.generation % 7 + 1
        self.holder, self.holder_name, self.holder_flags = token, name, flags

    def _claim(self, request):
        """Claim, take over or give up, and show the holder now."""
        action, token = request[3], struct.unpack_from('<I', request, 4)[0]
        if action > hlp.CLAIM_GIVE_UP:
            return self._invalid(3)
        if token == 0:
            return self._invalid(4)
        if action == hlp.CLAIM_GIVE_UP:
            if token == self.holder:
                self._set_holder(0, '', 0)
        elif action == hlp.CLAIM_TAKE_OVER or self.holder in (0, token):
            name = request[8:24].split(b'\x00')[0].decode()
            self._set_holder(token, name, request[24] & hlp.CLAIM_EXCLUSIVE)
        return self._reply(payload=request[3:8] + self._controller())

    def _configure(self, request):
        """Set the takeover mode, the timeout (0 = 2000, clamped to 100-10000) and brightness scaling."""
        overlay, timeout, brightness = request[3], struct.unpack_from('<H', request, 4)[0], request[6]
        if overlay > 1:
            return self._invalid(3)
        if brightness > 1:
            return self._invalid(6)
        self.settings = (overlay, 2000 if timeout == 0 else min(max(timeout, 100), 10000), brightness)
        return self._reply(payload=struct.pack('<BHB', *self.settings))

    def _subscribe(self, request):
        """Add the events asked for that the board has. A change before this raises no event."""
        first = not self.subscribed
        self.subscribed |= struct.unpack_from('<H', request, 3)[0] & sum(1 << e for e in self.events)
        if first and self.subscribed:
            self._seen = (self._state_bytes(), self.token)
        return self._reply(payload=struct.pack('<H', self.subscribed))

    def _leds(self, addressing, address) -> list:
        """Resolve one entry's address to the LEDs below the limit it stages."""
        if addressing == hlp.ADDRESSING_LIGHT:
            lights = self.lights[address:address + 1]
        elif addressing == hlp.ADDRESSING_ACTION:
            lights = [light for light in self.lights if light['action'] == address]
        elif addressing == hlp.ADDRESSING_KIND:
            lights = [light for light in self.lights if light['kind'] == address]
        else:  # by pin
            lights = [light for light in self.lights if light['pin'] == address]
        return [led for light in lights for led in range(light['first'], light['first'] + light['count'])
                if led < self.led_limit]

    def _stage(self, request):
        """Stage a run or entries, with the capacity, limit and addressing rules."""
        addressing, pixel_format = request[3] & 0x0F, request[3] >> 4
        sizes = {0: 3, 1: 4, 2: 2}
        if addressing not in self.addressing or pixel_format not in self.pixel_formats:
            return self._invalid(3)
        width = sizes[pixel_format]
        if addressing == hlp.ADDRESSING_LED_RUN:
            first, count = struct.unpack_from('<H', request, 4)[0], request[6]
            if first >= self.led_limit:
                return self._invalid(4)
            if not 0 < count <= min((hlp.REPORT_SIZE - 7) // width, 32) or first + count > self.led_limit:
                return self._invalid(6)
            for index in range(count):
                self.staged[first + index] = tuple(request[7 + index * width:7 + (index + 1) * width])
            return self._reply(payload=bytes([count, 0]) + struct.pack('<I', (1 << count) - 1))
        address_size = {hlp.ADDRESSING_LIGHT: 2, hlp.ADDRESSING_ACTION: 2}.get(addressing, 1)
        size = address_size + width
        count = request[4]
        if not 0 < count <= min((hlp.REPORT_SIZE - 5) // size, 32):
            return self._invalid(4)
        applied = mask = 0
        for index in range(count):
            entry = request[5 + index * size:5 + (index + 1) * size]
            pack = '<h' if addressing == hlp.ADDRESSING_ACTION else ('<H' if address_size == 2 else '<B')
            leds = self._leds(addressing, struct.unpack_from(pack, entry)[0])
            for led in leds:
                self.staged[led] = tuple(entry[address_size:])
            if leds:
                applied += 1
                mask |= 1 << index
        return self._reply(payload=bytes([applied, count - applied]) + struct.pack('<I', mask))

    def _clear(self, request):
        """Unstage every pixel."""
        self.staged = {}
        return self._reply()

    def _commit(self, request):
        """Publish the staged frame."""
        self._publish()
        return self._reply()

    def _release(self, request):
        """Return the LEDs to the on-board animations and clear the staged frame."""
        self.live = False
        self.staged, self.shown = {}, {}
        return self._reply()

    def _publish(self) -> None:
        """Show the staged frame, starting a takeover when none is live."""
        if not self.live:
            self.live = True
            self.number = (self.number + 1) & 0xFF
        self.shown = dict(self.staged)
        self.frames.append(self.shown)

    def _note(self) -> None:
        """Queue a STATE_CHANGED when page 1 or the token changed, while subscribed."""
        now = (self._state_bytes(), self.token)
        if now == self._seen:
            return
        self._seen = now
        if self.subscribed & 1:
            reply = page_reply(hlp.PAGE_STATE, [now[0]], hlp.PAGE_GROUPS[hlp.PAGE_STATE],
                               token=self.token)
            reply[0], reply[1], reply[2] = hlp.EVENT_MARKER, hlp.EVENT_STATE_CHANGED, 0
            self.outbox.append(bytes(reply))
