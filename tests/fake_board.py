"""A fake Host Lighting interface, parameterised by protocol version.

Sits where hidapi sits rather than replacing HostLightingDevice, so requests
travel through the real transport: the same framing, sequence handling and
reply matching that runs against a real board.

Hardware and firmware are described separately, because they are separate. A
board wires the lights it wires whatever firmware it runs, so `lights` describes
the hardware and `version` decides how much of it the firmware will admit to:
page 5 does not exist before v1.1, the extended controls do not stage before
v1.2, SET_LIGHT_RGBW does not exist before v1.3, and the per-entry outcome mask
reads as all-zeroes on anything older than v1.3. That last one is the trap worth
being able to reproduce: zeroes mean "every entry was skipped" to a host that
reads them without checking the version first.

SPDX-FileCopyrightText: © 2026 Jacob Simpson
SPDX-License-Identifier: GPL-3.0-or-later
"""
from hlp_spice2x import hlp

# The reference board, as it actually reports itself: a Haute42 COSMOX M Ultra
# Gen 2 running the LED-refactor pipeline. 46 lights, and the two controls that
# make it the board for multi-light work - Up owns ordinals 3 and 12, L3 owns 13
# and 15 - plus a 30-light case strip, which is what makes expanding whole-strip
# controls a bad idea rather than merely a wasteful one.
M_ULTRA_LIGHTS = [(2, 0), (1, 1), (3, 2), (0, 3), (6, 4), (7, 5), (9, 6), (8, 7),
                  (4, 8), (5, 9), (11, 10), (10, 11), (0, 12), (14, 13), (15, 14),
                  (14, 15)] + [(29, led) for led in range(16, 46)]

# A board with one light per control and no duplicates, for tests that only care
# about the protocol version rather than about the shape of the hardware.
PLAIN_LIGHTS = [(button_id, button_id) for button_id in range(16)]


class OneShotConnection:
    """A spice2x connection that answers one poll and then ends the run.

    Ending it the way Ctrl-C does exercises the loop's own shutdown path rather
    than a special case built for tests.
    """

    def __init__(self, states=None):
        """Hold the light states this connection will report once."""
        self.states = states or {}
        self.polls = 0

    def lights_read(self):
        """Report the states once, then interrupt the loop."""
        self.polls += 1
        if self.polls > 1:
            raise KeyboardInterrupt
        return dict(self.states)


class FakeBoard:
    """Answer Host Lighting requests the way a board at a given version would."""

    def __init__(self, version=(1, 3), lights=PLAIN_LIGHTS, synthesised=False,
                 colour_format=0, render_hz=40, light_table_feature=True, led_extent=None,
                 label='Fake Board', firmware='v0.0.0-fake',
                 board_id='0011223344556677', fingerprint=1, magic=b'GPHL',
                 drop=(), reject=()):
        """Describe the board and the firmware it is running.

        :param version: the (major, minor) the board reports from PING
        :param lights: the board's lights in ordinal order, as (button ID, first
            LED) or (button ID, first LED, LED count)
        :param synthesised: report the light table as rebuilt from per-control
            configuration, which cannot show a control owning several lights
        :param colour_format: page 2 colour format; 2 and 3 have a white channel
        :param render_hz: the board's render rate, or 0 for "not stated"
        :param light_table_feature: whether page 1 claims the light-table
            feature bit; a board that says no returns an empty page 5
        :param led_extent: the board's LED extent, defaulting to its light count
        :param label: the board label reported on page 0
        :param firmware: the firmware version reported on page 0
        :param board_id: the factory board ID as hex, reported on page 0
        :param fingerprint: the LED-map fingerprint reported on pages 1, 2 and 5
        :param magic: the PING magic, for standing in as something that is not
            a Host Lighting interface at all
        :param drop: commands to leave unanswered, as a flaky link would
        :param reject: commands to answer with a non-OK status
        """
        self.version = tuple(version)
        self.lights = [tuple(light) + (1,) * (3 - len(light)) for light in lights]
        self.synthesised = synthesised
        self.colour_format = colour_format
        self.render_hz = render_hz
        self.light_table_feature = light_table_feature
        self.led_extent = len(self.lights) if led_extent is None else led_extent
        self.label = label
        self.firmware = firmware
        self.board_id = board_id
        self.fingerprint = fingerprint
        self.magic = magic
        # mutable so a test can start dropping or rejecting mid-run
        self.drop = set(drop)
        self.reject = set(reject)
        self.requests = []
        self.pending = []
        self.closed = False

    @property
    def minor(self) -> int:
        """The minor version this board answers as, for gating its own replies."""
        return self.version[1]

    def open(self):
        """Wrap this board in a real HostLightingDevice.

        :return: a HostLightingDevice driving this board through the real transport
        """
        return hlp.HostLightingDevice.from_hid(self)

    def write(self, data):
        """Parse one written report and queue whatever reply it earns."""
        report = bytes(data)
        # data[0] is hidapi's leading report ID, so the command starts at [1]
        command, sequence, payload = report[1], report[2], report[3:]
        self.requests.append((command, payload))
        reply = self._answer(command, payload)
        if reply is not None:
            reply[0] = command | hlp.RESPONSE_FLAG
            reply[1] = sequence
            self.pending.append(bytes(reply))
        return len(data)

    def read(self, size):
        """Hand back the next queued reply, or nothing at all."""
        return list(self.pending.pop(0)) if self.pending else []

    def close(self):
        """Match the hidapi interface."""
        self.closed = True

    def staged(self, command) -> list:
        """Every payload written for one command, for asserting on the path taken.

        :param command: the command byte to collect
        :return: the payloads written for that command, in order
        """
        return [payload for written, payload in self.requests if written == command]

    def _reply(self, status=0) -> bytearray:
        """Build an empty reply carrying the given status."""
        reply = bytearray(hlp.REPORT_SIZE)
        reply[2] = status
        return reply

    def _has_light(self, button_id) -> bool:
        """Say whether any of this board's lights belong to a control."""
        return any(light[0] == button_id for light in self.lights)

    def _stageable(self, button_id) -> bool:
        """Say whether SET_BUTTONS can colour a control on this firmware.

        The extended controls are named by page 5 from v1.1 but do not stage by
        name until v1.2, so on v1.1 they are reported skipped even though the
        board plainly has the light.
        """
        if self.minor < 2 and (18 <= button_id <= 19 or 30 <= button_id <= 41):
            return False
        return self._has_light(button_id)

    def _answer(self, command, payload):
        """Build the reply to one request, or None to stay silent."""
        if command in self.drop:
            return None
        if command in self.reject:
            return self._reply(status=1)
        if command == hlp.CMD_PING:
            reply = self._reply()
            reply[3:7] = self.magic
            reply[7], reply[8] = self.version
            return reply
        if command == hlp.CMD_GET_CAPS:
            return self._caps_page(payload[0], payload[1] if len(payload) > 1 else 0)
        if command == hlp.CMD_SET_BUTTONS:
            return self._stage_buttons(payload)
        if command in (hlp.CMD_SET_LIGHT, hlp.CMD_SET_LIGHT_RGBW):
            return self._stage_lights(command, payload)
        if command in (hlp.CMD_SET_MODE, hlp.CMD_CLEAR, hlp.CMD_COMMIT, hlp.CMD_RELEASE,
                       hlp.CMD_SET_RANGE, hlp.CMD_SET_RANGE_RGBW):
            return self._reply()
        return self._reply(status=1)

    def _caps_page(self, page, start):
        """Build one GET_CAPS page."""
        if page == hlp.CAPS_PAGE_IDENTITY:
            reply = self._reply()
            reply[3] = 1
            reply[4:12] = bytes.fromhex(self.board_id)
            strings = self.label.encode() + b'\x00' + self.firmware.encode() + b'\x00'
            reply[12:12 + len(strings)] = strings
            return reply
        if page == hlp.CAPS_PAGE_STATE:
            reply = self._reply()
            reply[5], reply[6] = 5, 1
            reply[7:11] = self.fingerprint.to_bytes(4, 'little')
            # the v1.1 tail: firmware predating it leaves these zero-filled, which
            # is exactly how a host is meant to read "not reported"
            if self.minor >= 1:
                features = hlp.FEATURE_POSITIONS
                if self.light_table_feature:
                    features |= hlp.FEATURE_LIGHT_TABLE
                reply[12:16] = features.to_bytes(4, 'little')
                reply[16], reply[17], reply[18] = 2, 2, self.render_hz
            return reply
        if page == hlp.CAPS_PAGE_LED_MAP:
            reply = self._reply()
            reply[3], reply[4], reply[5] = 1, self.colour_format, 27
            reply[6], reply[7] = self.led_extent, 255
            for index in range(len(hlp.BUTTON_NAMES)):
                first = next((led for bid, led, _ in self.lights if bid == index), hlp.UNMAPPED)
                reply[8 + index * 2] = first
                reply[9 + index * 2] = 0 if first == hlp.UNMAPPED else 1
            for slot in range(4):
                reply[44 + slot] = hlp.UNMAPPED
            reply[48], reply[49], reply[50] = hlp.UNMAPPED, hlp.UNMAPPED, 0
            reply[51:55] = self.fingerprint.to_bytes(4, 'little')
            return reply
        if page == hlp.CAPS_PAGE_LIGHTS:
            return self._lights_page(start)
        # pages 3 and 4 are real but nothing here reads them
        return self._reply(status=2)

    def _lights_page(self, start):
        """Build one page of the light table, or refuse it the way old firmware does."""
        if self.minor < 1:
            # an unknown page is answered INVALID_ARG, which a host reads as
            # "not supported by this firmware" rather than as a failure
            return self._reply(status=2)
        reply = self._reply()
        stride, per_page = 12, 4
        # a cleared feature bit is a promise the page returns nothing, not a hint
        lights = self.lights if self.light_table_feature else []
        chunk = lights[start:start + per_page]
        reply[3], reply[4], reply[5], reply[6] = len(lights), start, len(chunk), stride
        flags = 0 if self.synthesised else hlp.LIGHT_FLAG_PER_LIGHT
        for n, (button_id, first_led, led_count) in enumerate(chunk):
            base = 7 + n * stride
            reply[base], reply[base + 1] = first_led, led_count
            reply[base + 3] = button_id
            reply[base + 4] = hlp.UNMAPPED
            reply[base + 7], reply[base + 8] = hlp.UNMAPPED, hlp.UNMAPPED
            reply[base + 11] = flags
        reply[60:64] = self.fingerprint.to_bytes(4, 'little')
        return reply

    def _stage_buttons(self, payload):
        """Answer a SET_BUTTONS with how many entries found a light."""
        reply = self._reply()
        applied = sum(1 for n in range(payload[0]) if self._stageable(payload[1 + n * 4]))
        reply[3], reply[4] = applied, payload[0] - applied
        return reply

    def _stage_lights(self, command, payload):
        """Answer a per-light staging command, masking outcomes only from v1.3."""
        if command == hlp.CMD_SET_LIGHT and self.minor < 2:
            return self._reply(status=1)
        if command == hlp.CMD_SET_LIGHT_RGBW and self.minor < 3:
            return self._reply(status=1)
        width = 5 if command == hlp.CMD_SET_LIGHT_RGBW else 4
        applied = skipped = mask = 0
        for n in range(payload[0]):
            if payload[1 + n * width] < len(self.lights):
                applied += 1
                mask |= 1 << n
            else:
                skipped += 1
        reply = self._reply()
        reply[3], reply[4] = applied, skipped
        if self.minor >= 3:
            reply[5], reply[6] = mask & 0xFF, (mask >> 8) & 0xFF
        return reply
