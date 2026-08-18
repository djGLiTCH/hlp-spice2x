"""Talk to a GP2040-CE board's Host Lighting interface over HID.

The Host Lighting add-on exposes a vendor HID interface (usage page 0xFF47,
usage 0x4C) carrying the Host Lighting Protocol: fixed 64-byte reports that
let host software drive the board's RGB LEDs live and read the board's LED
layout from its own configuration.

Every request is `[0]=command, [1]=sequence, [2..]=payload`; the reply echoes
the command with bit 7 set: `[0]=command|0x80, [1]=sequence, [2]=status,
[3..]=payload`.

The board describes itself through GET_CAPS pages. The protocol defines six;
this module decodes the four the bridge needs, and the rest are listed so the
surface is clear:

* page 0 (identity): factory-unique board ID, board label, firmware version.
  Read here, and the right thing to bind a board to, since device paths change
  with USB ports.
* page 1 (runtime state): input mode, profile number, brightness step,
  host-assigned player, LED-map fingerprint, current animation index, and from
  protocol v1.1 the feature bitmask, LED framework, animation namespace and
  render rate. Note the brightness field is a step index into the board's
  configured steps, not a 0-255 level, and the step count is not reported.
* page 2 (LED map): LEDs per button, colour format, layout, LED extent,
  brightness maximum, and the per-button, player, turbo and case LED ranges.
  Read here for the colour format, which says whether the chain has a white
  channel to drive, and for the extent that bounds a raw range.
* page 3 (animations): current on-board animation index, and how many the
  board offers. Not read here.
* page 4 (positions): per-light grid positions, where the render pipeline
  provides them. Not read here: page 4 is a projection of page 5, so page 5
  answers everything the bridge would ask of it.
* page 5 (light table, protocol v1.1): every light the board has, naming the
  control that owns each one. This is the page that can say a control owns
  more than one light, which page 2's one-range-per-control table cannot.

Pages 2 and 5 answer different questions. Page 2 is "where do I write this
control"; page 5 is the board's inventory of lights.

This is a trimmed copy of the Host Lighting helpers from gp2040ce-binary-tools,
vendored so the bridge needs nothing but hidapi. What is trimmed is the tooling
around the protocol - the ping, caps, fill and reboot entry points, their
printers and the CLI - none of which a bridge has any use for. What is kept is
the protocol surface itself, so teaching the bridge a new protocol version is a
change here rather than a second client. If those tools land upstream, this
module can be replaced by an import of gp2040ce_bintools.hostlighting.

See docs/host-lighting.md in the GP2040-CE repository for the protocol
reference.

SPDX-FileCopyrightText: (C) 2026 Jacob Simpson
SPDX-License-Identifier: GPL-3.0-or-later
"""
import time

# discovery: match the interface by these, never by VID:PID (which varies by input mode)
USAGE_PAGE = 0xFF47
USAGE = 0x4C
REPORT_SIZE = 64

# The protocol freezes command IDs and existing payload layouts within a major
# version: a minor version may only add, and only a major version may change
# what is already there. So a board reporting a minor above the highest known
# here still keeps every promise this client relies on, and is driven as the
# highest known rather than refused; a different major may have changed any of
# them underneath, and is refused.
SUPPORTED_MAJOR = 1
MAX_SUPPORTED_MINOR = 3
PING_MAGIC = b'GPHL'

# command IDs, grouped by function range
CMD_PING = 0x01                 # session and discovery, 0x01-0x0F
CMD_GET_CAPS = 0x02
CMD_SET_MODE = 0x03
CMD_SET_BUTTONS = 0x10          # frame staging, 0x10-0x2F
CMD_SET_RANGE = 0x11
CMD_SET_RANGE_RGBW = 0x12
CMD_CLEAR = 0x14
CMD_SET_LIGHT = 0x15            # protocol v1.2
CMD_SET_LIGHT_RGBW = 0x16       # protocol v1.3
CMD_COMMIT = 0x30               # frame lifecycle, 0x30-0x3F
CMD_RELEASE = 0x31

RESPONSE_FLAG = 0x80
STATUS_NAMES = {0: 'OK', 1: 'UNSUPPORTED', 2: 'INVALID_ARG'}

# GET_CAPS pages, as the request's payload byte [2]. Pages 3 and 4 are part of
# the protocol but unread here; see the module docstring for what they hold.
CAPS_PAGE_IDENTITY = 0
CAPS_PAGE_STATE = 1
CAPS_PAGE_LED_MAP = 2
CAPS_PAGE_ANIMATIONS = 3
CAPS_PAGE_POSITIONS = 4
CAPS_PAGE_LIGHTS = 5            # protocol v1.1

# pages answered a slice at a time, taking a start entry in payload byte [3]
PAGED_CAPS_PAGES = (CAPS_PAGE_POSITIONS, CAPS_PAGE_LIGHTS)

# page 1 fields appended by protocol v1.1. Replies are zero-filled before the
# board builds them, so firmware predating these reports them as zero, which
# reads correctly as "nothing supported, nothing stated" rather than as a value.
FEATURE_POSITIONS = 1 << 0
FEATURE_LIGHT_TABLE = 1 << 1

# page 5 record flags. Both are positive assertions: a set bit is the board
# vouching for something, so a record left at zero claims nothing.
LIGHT_FLAG_POSITION = 0x01      # the grid coordinates in this record are real
LIGHT_FLAG_PER_LIGHT = 0x02     # read from a per-light table, describes one light

# The narrowest page 5 record this decoder can read. The real stride comes off
# the wire on every reply, which is why it is there: a later, wider record must
# not silently misalign an older decoder. Anything narrower is missing fields
# that are read below, so it is refused rather than guessed at.
LIGHT_STRIDE_MIN = 12

# a page 1 or page 2 slot with nothing in it, and the page 5 spelling of the
# same byte - they answer different questions, so both names are kept
UNMAPPED = 0xFF
BUTTON_NONE = 0xFF              # no control owns this light

# the firmware's LED buffer size, which bounds raw ranges
MAX_LEDS = 100
# per-report payload limits
SET_BUTTONS_MAX = 15            # [buttonId,R,G,B] entries
SET_RANGE_MAX = 20              # RGB pixels
SET_RANGE_RGBW_MAX = 15         # RGBW pixels
SET_LIGHT_MAX = 15              # [ordinal,R,G,B] entries, protocol v1.2
SET_LIGHT_RGBW_MAX = 12         # [ordinal,R,G,B,W] entries, protocol v1.3

# page 2 colour formats. The W formats are the ones with a white emitter to
# drive; on the others a white component has nowhere to go.
LED_FORMAT_NAMES = {0: 'GRB', 1: 'RGB', 2: 'GRBW', 3: 'RGBW'}
WHITE_LED_FORMATS = frozenset({2, 3})

# button IDs 0-17 as indexed in the page 2 LED map. That table is exactly these
# eighteen and can never grow, so this stays a plain list of that length.
BUTTON_NAMES = ['Up', 'Down', 'Left', 'Right', 'B1', 'B2', 'B3', 'B4', 'L1', 'R1', 'L2', 'R2',
                'S1', 'S2', 'L3', 'R3', 'A1', 'A2']
SPECIAL_TARGETS = {'PLED1': 24, 'PLED2': 25, 'PLED3': 26, 'PLED4': 27, 'TURBO': 28, 'CASE': 29}

# The wider namespace the light table can report, added by protocol v1.1: A3 and
# A4 at 18-19, E1-E12 at 30-41. Page 2 has no slot for these, so they appear
# only on page 5. 20-23 are permanently unassigned - those gamepad bits are the
# dpad in a second encoding rather than four more controls - so they are absent
# here on purpose. From v1.1 the light table names them; only from v1.2 does
# SET_BUTTONS stage them.
EXTENDED_TARGETS = {'A3': 18, 'A4': 19}
EXTENDED_TARGETS.update({f'E{n + 1}': 30 + n for n in range(12)})
EXTENDED_CONTROLS = frozenset(EXTENDED_TARGETS.values())

# every spelling a profile may use, and the reverse for reporting
CONTROL_TARGETS = {name.upper(): index for index, name in enumerate(BUTTON_NAMES)}
CONTROL_TARGETS.update(SPECIAL_TARGETS)
CONTROL_TARGETS.update(EXTENDED_TARGETS)
CONTROL_NAMES = dict(enumerate(BUTTON_NAMES))
CONTROL_NAMES.update({value: name for name, value in SPECIAL_TARGETS.items()})
CONTROL_NAMES.update({value: name for name, value in EXTENDED_TARGETS.items()})

# Controls that are meant to be a single lamp, and so are worth expanding to all
# of their lights. The player LEDs, turbo and the case strip are left out on
# purpose: each already has a staging path that covers the whole of it in one
# entry, and a case strip is routinely dozens of lights, so expanding one would
# buy nothing and cost several extra reports in every frame.
ONE_LAMP_CONTROLS = frozenset(range(0, 20)) | frozenset(range(30, 42))


def build_request(command: int, sequence: int, payload: bytes = b'') -> bytes:
    """Frame a request as a 64-byte report.

    :param command: command byte (0x01-0x7F)
    :param sequence: sequence byte echoed by the board in its reply
    :param payload: command payload, at most 62 bytes
    :return: the request framed to exactly REPORT_SIZE bytes
    """
    if len(payload) > REPORT_SIZE - 2:
        raise ValueError(f"payload too long ({len(payload)} > {REPORT_SIZE - 2})")
    return bytes([command, sequence]) + payload + bytes(REPORT_SIZE - 2 - len(payload))


def match_reply(reply: bytes, command: int, sequence: int) -> bool:
    """Check whether a reply report answers the given request.

    :param reply: a reply report as read from the interface
    :param command: the command byte of the original request
    :param sequence: the sequence byte of the original request
    :return: True if the reply's command echo and sequence match
    """
    return len(reply) >= 3 and reply[0] == (command | RESPONSE_FLAG) and reply[1] == sequence


def control_name(button_id: int) -> str:
    """Render a protocol button ID as its control name.

    :param button_id: protocol button ID
    :return: the control name, or the raw ID if it is not a known control
    """
    return CONTROL_NAMES.get(button_id, str(button_id))


class HostLightingError(RuntimeError):
    """Errors talking to a Host Lighting interface."""


class HostLightingRejected(HostLightingError):
    """The board answered a command with a non-OK status."""


class HostLightingTimeout(HostLightingError):
    """No reply arrived in time.

    Replies are best-effort while streaming, so a caller may reasonably carry
    on after one of these.
    """


class HostLightingIncompatible(HostLightingError):
    """The board speaks a protocol major version this client does not.

    Distinct from a rejected command: nothing is wrong with the board or the
    link, the two ends simply do not agree on what the commands mean.
    """


class HostLightingDisconnected(HostLightingError):
    """The underlying HID device failed.

    Usually the board was unplugged or rebooted. Distinct from a timeout
    because carrying on is pointless.
    """


def _require_length(reply: bytes, length: int, what: str) -> None:
    """Refuse a reply too short for what is about to be read out of it.

    Reports are a fixed 64 bytes and every field below sits at a fixed offset
    inside one, so a short reply means a truncated or malformed read rather than
    an older board. Checking once here turns what would otherwise be an
    IndexError deep in a decoder into something a caller can report.

    :param reply: the reply report to check
    :param length: how many bytes the caller is about to read
    :param what: what is being decoded, for the error message
    """
    if len(reply) < length:
        raise HostLightingError(f"{what} reply is {len(reply)} bytes, need at least {length}")


def decode_state(reply: bytes) -> dict:
    """Decode GET_CAPS page 1, the board's runtime state.

    Page 1 reply layout: [3] input mode, [4] profile, [5] brightness step,
    [6] host-assigned player, [7..10] LED-map fingerprint (little endian),
    [11] current animation index, then appended by protocol v1.1: [12..15]
    feature bitmask, [16] LED framework, [17] animation namespace, [18] render
    rate in Hz. Byte [5] is a step index into the board's brightness steps, not
    a 0-255 level.

    Firmware predating the v1.1 fields zero-fills them, and zero reads correctly
    as "not reported" for every one of them, so reading them needs no version
    gate - only trusting a non-zero answer does.

    :param reply: a page 1 reply report
    :return: the decoded fields, with `render_hz` None when the board did not say
    """
    _require_length(reply, 19, 'page 1')
    return {
        'input_mode': reply[3],
        'profile': reply[4],
        'brightness_step': reply[5],
        'host_player': reply[6],
        'fingerprint': int.from_bytes(reply[7:11], 'little'),
        # 0xFF is 'none selected' - the lights-off state some pipelines persist
        'animation_index': None if reply[11] == UNMAPPED else reply[11],
        'features': int.from_bytes(reply[12:16], 'little'),
        # diagnostic only: the protocol says to branch on the feature bits and
        # the per-record flags and never on this, and 0 = not reported is a
        # valid answer from a board that supports everything
        'framework': reply[16],
        'animation_namespace': reply[17],
        'render_hz': reply[18] or None,
    }


def decode_led_map(reply: bytes) -> dict:
    """Decode GET_CAPS page 2, the per-control LED map.

    Page 2 answers "where do I write this control" for the eighteen canonical
    controls. It is not an inventory of the board's lights: a control may own
    several lights and only the first appears here, and a board may carry lights
    on controls this page cannot name. Page 5 is the inventory.

    `led_extent` is the highest LED index in use plus one - the number to size a
    frame buffer from, and the board's own answer to the question MAX_LEDS
    hardcodes. It is deliberately not the sum of the ranges below it, because
    that sum omits the lights page 2 cannot name.

    :param reply: a page 2 reply report
    :return: the decoded fields, `buttons` mapping button ID to (first LED,
        count) and `players` mapping player number to LED index
    """
    _require_length(reply, 55, 'page 2')
    buttons = {}
    for index in range(len(BUTTON_NAMES)):
        first, count = reply[8 + index * 2], reply[9 + index * 2]
        if first != UNMAPPED:
            buttons[index] = (first, count)
    # The four player slots are positional, so an unmapped one has to drop out
    # by key rather than by position: filtering them into a list would slide
    # player 2's index into player 1's place whenever player 1 is absent.
    players = {slot + 1: reply[44 + slot] for slot in range(4)
               if reply[44 + slot] != UNMAPPED}
    return {
        'leds_per_button': reply[3],
        'colour_format': reply[4],
        'layout': reply[5],
        'led_extent': reply[6],
        'brightness_maximum': reply[7],
        'buttons': buttons,
        'players': players,
        'turbo': None if reply[48] == UNMAPPED else reply[48],
        'case': None if reply[49] == UNMAPPED or not reply[50] else (reply[49], reply[50]),
        'fingerprint': int.from_bytes(reply[51:55], 'little'),
    }


def subtractive_white(colour) -> tuple:
    """Convert a profile colour into the RGBW a board expects to be sent.

    The achromatic part of a colour is exactly what a white emitter exists to
    carry, so it is moved there: W takes min(R, G, B) and the three colour
    channels are reduced by it. Sending white as equal parts red, green and blue
    on a chain that has a white emitter lights three LEDs to make a worse white
    than the one sitting next to them.

    A white component the profile gave explicitly is added on top and clamped,
    so a colour asking for red plus white gets both rather than whichever of the
    two the conversion happened to favour.

    :param colour: (r, g, b), or (r, g, b, w) as the profile gave it
    :return: (r, g, b, w) to put on the wire
    """
    red, green, blue = colour[:3]
    achromatic = min(red, green, blue)
    asked = colour[3] if len(colour) > 3 else 0
    return (red - achromatic, green - achromatic, blue - achromatic,
            min(255, achromatic + asked))


def has_white_channel(colour_format: int) -> bool:
    """Say whether a page 2 colour format has a white emitter to drive.

    :param colour_format: the page 2 colour format byte
    :return: True if the chain carries a white channel a W component can reach
    """
    return colour_format in WHITE_LED_FORMATS


def decode_lights(reply: bytes) -> dict:
    """Decode one page of GET_CAPS page 5, the light table.

    Page 5 reply layout: [3] records the board has in total, [4] the ordinal
    this page starts at, [5] records in this page, [6] record stride, [7..] the
    records themselves, [60..63] the light table's fingerprint.

    The stride is read from the reply rather than assumed: it is on the wire
    precisely so that a later, wider record does not silently misalign an older
    decoder. A record is identified by its ordinal, not by its first LED, which
    is not a unique key - boards exist with two lights starting at one index.

    :param reply: a page 5 reply report
    :return: the page header plus its decoded records
    """
    _require_length(reply, 64, 'page 5')
    total, start, count, stride = reply[3], reply[4], reply[5], reply[6]
    if count and stride < LIGHT_STRIDE_MIN:
        raise HostLightingError(f"page 5 record stride is {stride}, need at least {LIGHT_STRIDE_MIN}")
    _require_length(reply, 7 + count * stride, 'page 5')
    records = []
    for n in range(count):
        record = reply[7 + n * stride:7 + (n + 1) * stride]
        flags = record[11]
        records.append({
            'ordinal': start + n,
            'first_led': record[0],
            'led_count': record[1],
            'kind': record[2],
            'button_id': record[3],
            'gpio_pin': None if record[4] == UNMAPPED else record[4],
            'gpio_action': int.from_bytes(record[5:7], 'little', signed=True),
            'player_index': None if record[7] == UNMAPPED else record[7],
            'case_group': None if record[8] == UNMAPPED else record[8],
            'position': (record[9], record[10]) if flags & LIGHT_FLAG_POSITION else None,
            # Reported as the caveat rather than the assertion: a caller wants to
            # know when a record cannot show duplicates, and that is the absence
            # of the board's per-light claim. A synthesised table is rebuilt from
            # per-control configuration, so it holds one row per control however
            # many lights that control really drives.
            'synthesised': not (flags & LIGHT_FLAG_PER_LIGHT),
        })
    return {
        'total': total,
        'start': start,
        'stride': stride,
        'records': records,
        'fingerprint': int.from_bytes(reply[60:64], 'little'),
    }


class Capabilities:
    """What the board on the other end can do, decided once at connect.

    Call sites ask this object what is available rather than comparing version
    numbers, so a new protocol version adds a field here instead of a version
    comparison at every branch that cares.

    Two of these deliberately do not follow from the version. The light table is
    gated on the page 1 feature bit as well as the version, because a board that
    enumerates before its render core has populated the light registry will
    legitimately report the bit clear and answer page 5 with nothing. And the
    LED framework byte is not consulted at all: the protocol says to branch on
    the feature bits and the per-record flags and never on it, and a board that
    does not report a framework is not thereby a lesser board.
    """

    def __init__(self, major=None, minor=None, reported=None, state=None, led_map=None,
                 forced=False):
        """Hold a negotiated version and the capability pages read alongside it.

        :param major: the negotiated major version, or None for no board
        :param minor: the negotiated minor version, clamped to what is supported
        :param reported: the (major, minor) the board actually stated
        :param state: the decoded GET_CAPS page 1
        :param led_map: the decoded GET_CAPS page 2
        :param forced: whether an unsupported major version was overridden
        """
        self.major = major
        self.minor = minor
        self.reported = reported
        self.forced = forced
        self.state = state or {}
        self.led_map = led_map or {}
        self.lights = []

    @property
    def lights(self) -> list:
        """The board's light records, in the order page 5 reported them."""
        return self._lights

    @lights.setter
    def lights(self, records) -> None:
        """Cache the records, and the per-control index derived from them.

        Indexing here rather than at each lookup keeps the streaming loop off a
        scan of the whole table: a board with a case strip has several dozen
        records, and a frame may ask about several controls.
        """
        self._lights = list(records)
        owners = {}
        for record in self._lights:
            if not record['synthesised']:
                owners.setdefault(record['button_id'], []).append(record)
        self.owners = owners

    @classmethod
    def absent(cls):
        """Capabilities for a run with no board, where nothing can be negotiated.

        Every capability answers no, so a dry run takes the same code paths as
        the oldest board rather than needing a check for the absence of a board
        at each one.

        :return: a Capabilities describing no board at all
        """
        return cls()

    @property
    def connected(self) -> bool:
        """Whether these capabilities came from a board."""
        return self.major is not None

    @property
    def light_table(self) -> bool:
        """Whether page 5 will return entries.

        Both halves matter: the page does not exist before v1.1, and a cleared
        feature bit is a promise it returns nothing rather than a hint that it
        might. The bit can go from clear to set on a fingerprint change, so this
        is re-derived from the current page 1 rather than frozen at connect.
        """
        return (self.minor or 0) >= 1 and bool(self.state.get('features', 0) & FEATURE_LIGHT_TABLE)

    @property
    def per_light(self) -> bool:
        """Whether a single light can be staged by its ordinal (SET_LIGHT, v1.2)."""
        return (self.minor or 0) >= 2

    @property
    def outcome_mask(self) -> bool:
        """Whether a staging reply names which entries were skipped (v1.3)."""
        return (self.minor or 0) >= 3

    @property
    def white_channel(self) -> bool:
        """Whether the board's chain has a white emitter for a W component to reach."""
        return has_white_channel(self.led_map.get('colour_format', 0))

    @property
    def host_white(self) -> bool:
        """Whether a host-supplied white component is honoured as sent.

        Before v1.3 an achromatic colour is mapped onto the white emitter by the
        firmware itself and a host-supplied W is ignored, so sending the
        textbook subtractive white renders dark. Needing both halves is the
        point: a white chain on old firmware is not a white channel a host can
        drive.
        """
        return self.white_channel and (self.minor or 0) >= 3

    def stages_by_name(self, button_id: int) -> bool:
        """Whether SET_BUTTONS will colour this control on this firmware.

        The extended controls are named by the light table from v1.1 but do not
        stage by name until v1.2. Before then a SET_BUTTONS entry naming one is
        simply counted as skipped, and the light table is the only route to it.

        :param button_id: the control to ask about
        :return: True if naming the control in a SET_BUTTONS entry will reach it
        """
        return not (button_id in EXTENDED_CONTROLS and (self.minor or 0) < 2)

    def lights_owned_by(self, button_id: int) -> list:
        """List the lights the board attributes to a control, ignoring synthesised rows.

        A synthesised table is rebuilt from per-control configuration, so it
        holds one row per control however many lights that control really
        drives. Those rows cannot answer the question this is asked for, so they
        are discarded rather than counted.

        :param button_id: the control to ask about
        :return: the board's records for that control, in ordinal order
        """
        return self.owners.get(button_id, [])

    def names_control(self, button_id: int) -> bool:
        """Say whether the light table mentions a control at all, synthesised or not.

        The difference between this and lights_owned_by being empty is the
        difference between a board that has no such light and a board whose
        table cannot describe the one it has.

        :param button_id: the control to ask about
        :return: True if any record names that control
        """
        return any(record['button_id'] == button_id for record in self.lights)

    def stage_op(self, record) -> tuple:
        """Say how to colour one light on this firmware.

        An ordinal names the whole record in one entry and is immune to page 2's
        best-effort LED bindings, so it is preferred wherever SET_LIGHT exists.
        Before that the only address available is the raw LED index the record
        carries, together with its own count, which may differ from the board's
        global LEDs-per-button.

        :param record: a page 5 light record
        :return: ('light', ordinal) or ('range', first LED, LED count)
        """
        if self.per_light:
            return ('light', record['ordinal'])
        return ('range', record['first_led'], record['led_count'])

    def staging_plan(self) -> dict:
        """Decide, per control, how a bare control name reaches all of its lights.

        Only controls needing something other than a plain SET_BUTTONS entry
        appear here, so on most boards this is empty and costs nothing.

        Two shapes go in. A control the light table names but this firmware
        cannot stage by name has to be coloured by raw index instead. And a
        one-lamp control the board attributes several lights to gets all of them
        staged explicitly - all of them, including the one SET_BUTTONS already
        covered, because there is no sanctioned way to tell which one that was.
        The redundant write is one entry of one report, and accepting it is the
        only rule that is correct on both render pipelines.

        :return: mapping of button ID to (stages by name, staging operations)
        """
        plan = {}
        for button_id in {record['button_id'] for record in self.lights}:
            records = self.lights_owned_by(button_id)
            if not records:
                continue
            if not self.stages_by_name(button_id):
                plan[button_id] = (False, [self.stage_op(record) for record in records])
            elif button_id in ONE_LAMP_CONTROLS and len(records) > 1:
                plan[button_id] = (True, [self.stage_op(record) for record in records])
        return plan

    def refresh(self, device) -> None:
        """Re-read the pages a fingerprint change invalidates.

        A fingerprint change means re-reading whichever of the capability pages
        a host caches. Page 1 is re-read too, because the light-table feature
        bit can go from clear to set once the board has finished LED setup, and
        a host that enumerated early would otherwise never notice.

        :param device: an opened HostLightingDevice
        """
        self.state = read_state(device)
        self.led_map = read_led_map(device)
        self.lights = read_light_table(device, self)

    @property
    def render_hz(self):
        """The board's render rate in Hz, or None if it did not say."""
        return self.state.get('render_hz')

    @property
    def led_extent(self) -> int:
        """The board's own LED extent, falling back to the firmware buffer ceiling."""
        return self.led_map.get('led_extent') or MAX_LEDS

    @property
    def fingerprint(self):
        """The LED-map fingerprint, which changes when the map does."""
        return self.state.get('fingerprint')

    def summary(self) -> str:
        """Name the negotiated version and what it buys, for the startup line.

        Multi-version support that cannot be seen at runtime cannot be supported
        in the field, so this is printed on every run rather than under a flag.

        :return: a one-line summary of the negotiated capabilities
        """
        if not self.connected:
            return "no board: running without one, so nothing was negotiated"
        spoken = f"v{self.reported[0]}.{self.reported[1]}"
        driven = '' if self.reported[1] == self.minor else f", driven as v{self.major}.{self.minor}"
        offers = ['light table' if self.light_table else 'no light table',
                  'per-light staging' if self.per_light else 'no per-light staging']
        if self.outcome_mask:
            offers.append('outcome mask')
        offers.append(f"renders at {self.render_hz} Hz" if self.render_hz
                      else 'render rate unstated')
        white = 'yes' if self.white_channel else 'no'
        return f"board speaks HLP {spoken}{driven} - {', '.join(offers)} (white channel: {white})"


class HostLightingDevice:
    """One GP2040-CE board's Host Lighting interface."""

    def __init__(self, path: bytes):
        """Open the HID device at the given hidapi path.

        :param path: platform-specific hidapi device path from enumeration
        """
        hid = _import_hid()
        device = hid.device()
        device.open_path(path)
        device.set_nonblocking(True)
        self._bind(device)

    @classmethod
    def from_hid(cls, device):
        """Wrap an already-open hidapi device object.

        The one seam through which a device can be built around something other
        than a real HID path, so a fake board and a real one are initialised by
        the same code and cannot drift apart as per-session state is added.

        :param device: an object with the hidapi read/write/close interface
        :return: a HostLightingDevice driving it
        """
        wrapper = cls.__new__(cls)
        wrapper._bind(device)
        return wrapper

    def _bind(self, device) -> None:
        """Attach an open hidapi device and initialise per-session state."""
        self.device = device
        self.sequence = 0

    def close(self) -> None:
        """Close the HID device."""
        self.device.close()

    def send(self, command: int, payload: bytes = b'') -> None:
        """Send one command without waiting for its reply.

        Staging commands are fire-and-forget for throughput; only the COMMIT
        that publishes a frame is worth waiting on.

        :param command: command byte
        :param payload: command payload bytes
        """
        self.sequence = (self.sequence % 127) + 1
        # the interface uses unnumbered reports; hidapi wants a leading 0x00 report ID on write
        try:
            self.device.write(b'\x00' + build_request(command, self.sequence, payload))
        except OSError as error:
            raise HostLightingDisconnected(f"board went away while sending "
                                           f"0x{command:02X} ({error})") from None

    def request(self, command: int, payload: bytes = b'', timeout: float = 0.5) -> bytes:
        """Send one command and wait for its matching reply.

        Because commands can be pipelined, replies may arrive interleaved;
        each incoming report is matched against this request by its command
        echo and sequence number rather than assuming strict ordering.

        :param command: command byte
        :param payload: command payload bytes
        :param timeout: seconds to wait for the matching reply
        :return: the reply report ([2] is the status byte, [3..] the payload)
        """
        self.sequence = (self.sequence % 127) + 1
        try:
            self.device.write(b'\x00' + build_request(command, self.sequence, payload))
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                report = bytes(self.device.read(REPORT_SIZE))
                if match_reply(report, command, self.sequence):
                    return report
                time.sleep(0.001)
        except OSError as error:
            raise HostLightingDisconnected(f"board went away while sending "
                                           f"0x{command:02X} ({error})") from None
        raise HostLightingTimeout(f"no reply to command 0x{command:02X} within {timeout}s")

    def request_ok(self, command: int, payload: bytes = b'', timeout: float = 0.5) -> bytes:
        """Send one command and require an OK status in the reply.

        :param command: command byte
        :param payload: command payload bytes
        :param timeout: seconds to wait for the matching reply
        :return: the reply report
        """
        reply = self.request(command, payload, timeout)
        if reply[2] != 0:
            status = STATUS_NAMES.get(reply[2], hex(reply[2]))
            raise HostLightingRejected(f"command 0x{command:02X} rejected: {status}")
        return reply

    def get_caps_page(self, page: int, start_entry: int = 0) -> bytes:
        """Read one GET_CAPS page.

        :param page: which capability page to read (CAPS_PAGE_* constant)
        :param start_entry: first entry to return, for the paged pages
        :return: the reply report ([3..] is the page's payload)
        """
        if page in PAGED_CAPS_PAGES:
            return self.request_ok(CMD_GET_CAPS, bytes([page, start_entry]))
        return self.request_ok(CMD_GET_CAPS, bytes([page]))

    def read_lights(self, retries: int = 2) -> tuple:
        """Read every page 5 record, walking the pages.

        The table can move underneath a walk that takes several reads - a
        profile switch is enough - and a spliced result is worse than no result,
        because ordinals from either side of the change all look equally valid.
        Every page carries the table's fingerprint, so a walk that straddled a
        change is thrown away and retried rather than returned.

        :param retries: how many times to restart a walk the board changed under
        :return: (the board's light records, the fingerprint they were read at)
        :raises HostLightingRejected: on firmware without page 5 (pre-v1.1)
        :raises HostLightingError: if the board stops making progress, or keeps
            changing the table while it is being read
        """
        for _ in range(retries + 1):
            records = []
            fingerprint = None
            while True:
                page = decode_lights(self.get_caps_page(CAPS_PAGE_LIGHTS, len(records)))
                if fingerprint is None:
                    fingerprint = page['fingerprint']
                elif page['fingerprint'] != fingerprint:
                    break  # the table moved mid-walk, so this walk is not coherent
                if not page['records']:
                    if len(records) < page['total']:
                        raise HostLightingError(f"light table stalled at {len(records)} of "
                                                f"{page['total']} records")
                    return records, fingerprint
                records.extend(page['records'])
                if len(records) >= page['total']:
                    return records, fingerprint
        raise HostLightingError("light table kept changing while it was being read")

    def _light_reports(self, command: int, entries: list, capacity: int, width: int) -> list:
        """Split per-light entries into the reports needed to carry them.

        Entry width is checked before anything is built. The count byte tells
        the firmware how many fixed-width entries follow, so an entry of the
        wrong width is not rejected - it shifts every entry after it and stages
        garbage, with nothing on the wire to say so.

        :param command: the staging command, for the error message
        :param entries: per-light tuples, one report entry each
        :param capacity: entries per report for this command
        :param width: bytes per entry for this command
        :return: (chunk, payload) for each report to send
        """
        wrong = next((entry for entry in entries if len(entry) != width), None)
        if wrong is not None:
            raise ValueError(f"command 0x{command:02X} takes {width}-byte entries, got {len(wrong)}")
        return [(entries[start:start + capacity],
                 bytes([len(entries[start:start + capacity])])
                 + b''.join(bytes(entry) for entry in entries[start:start + capacity]))
                for start in range(0, len(entries), capacity)]

    def send_lights(self, entries: list, rgbw: bool = False) -> None:
        """Stage per-light colours by ordinal without waiting for the replies.

        The streaming counterpart to set_lights. Staging is fire-and-forget for
        throughput, the same as SET_BUTTONS and SET_RANGE, and only the COMMIT
        that publishes a frame is worth waiting on; a round trip per report in a
        frame loop would cost more than the diagnosis is worth at that rate. A
        caller that needs to know which entries the board skipped wants
        set_lights instead.

        :param entries: (ordinal, red, green, blue) tuples, or with a white
            component when rgbw is set
        :param rgbw: send the RGBW form of the command (protocol v1.3)
        """
        command = CMD_SET_LIGHT_RGBW if rgbw else CMD_SET_LIGHT
        capacity = SET_LIGHT_RGBW_MAX if rgbw else SET_LIGHT_MAX
        for _, payload in self._light_reports(command, entries, capacity, 5 if rgbw else 4):
            self.send(command, payload)

    def _stage_light_entries(self, command: int, entries: list, capacity: int, width: int,
                             outcomes: bool = False, timeout: float = 0.5) -> tuple:
        """Stage per-light entries and read back what the board made of them.

        :param command: CMD_SET_LIGHT or CMD_SET_LIGHT_RGBW
        :param entries: per-light tuples, one report entry each
        :param capacity: entries per report for this command
        :param width: bytes per entry for this command
        :param outcomes: whether to read the per-entry outcome mask, which only
            protocol v1.3 fills in - older boards zero-fill it, so reading it
            there reports every entry as skipped
        :param timeout: seconds to wait for each report's reply
        :return: (applied, skipped, the ordinals the board skipped), the ordinals
            being None when the outcome mask was not read
        """
        applied = skipped = 0
        stale = [] if outcomes else None
        for chunk, payload in self._light_reports(command, entries, capacity, width):
            reply = self.request_ok(command, payload, timeout)
            _require_length(reply, 7, f"command 0x{command:02X}")
            applied += reply[3]
            skipped += reply[4]
            if outcomes:
                # [5..6] little endian, bit n set = entry n of this report applied
                mask = reply[5] | (reply[6] << 8)
                stale.extend(entry[0] for bit, entry in enumerate(chunk) if not (mask >> bit) & 1)
        return applied, skipped, stale

    def set_lights(self, entries: list, outcomes: bool = False, timeout: float = 0.5) -> tuple:
        """Stage per-light colours by page 5 ordinal (protocol v1.2).

        An ordinal at or past the board's light total is counted as skipped by
        the board rather than treated as an error.

        :param entries: (ordinal, red, green, blue) tuples
        :param outcomes: whether to read the outcome mask; only pass True for a
            board negotiated at protocol v1.3 or later
        :param timeout: seconds to wait for each report's reply
        :return: (applied, skipped, the ordinals the board skipped or None)
        :raises HostLightingRejected: on firmware without SET_LIGHT (pre-v1.2)
        """
        return self._stage_light_entries(CMD_SET_LIGHT, entries, SET_LIGHT_MAX, 4, outcomes, timeout)

    def set_lights_rgbw(self, entries: list, outcomes: bool = False, timeout: float = 0.5) -> tuple:
        """Stage per-light colours with a white component (protocol v1.3).

        Boards whose chain has no white channel ignore W, the same rule as
        SET_RANGE_RGBW, so sending it is always safe on the wire. Whether the
        board renders a host-supplied W as the host intended is a separate
        question, and a version-dependent one.

        :param entries: (ordinal, red, green, blue, white) tuples
        :param outcomes: whether to read the outcome mask
        :param timeout: seconds to wait for each report's reply
        :return: (applied, skipped, the ordinals the board skipped or None)
        :raises HostLightingRejected: on firmware without SET_LIGHT_RGBW (pre-v1.3)
        """
        return self._stage_light_entries(CMD_SET_LIGHT_RGBW, entries, SET_LIGHT_RGBW_MAX, 5,
                                         outcomes, timeout)


def _import_hid():
    """Import the hidapi module, with a helpful error if it is missing."""
    try:
        import hid
    except ImportError as error:
        raise HostLightingError("this tool requires the hidapi package: pip install hidapi") from error
    return hid


def find_devices() -> list:
    """Enumerate all Host Lighting interfaces on the system.

    :return: list of hidapi enumeration dicts for matching interfaces
    """
    hid = _import_hid()
    return [info for info in hid.enumerate()
            if info.get('usage_page') == USAGE_PAGE and info.get('usage') == USAGE]


def read_identity(device: HostLightingDevice) -> tuple:
    """Read the board's identity from GET_CAPS page 0.

    Page 0 reply layout: [3] caps format, [4..11] factory-unique board ID,
    then two NUL-terminated strings (board label, firmware version).

    :param device: an opened HostLightingDevice
    :return: (factory board ID as hex, board label, firmware version)
    """
    reply = device.get_caps_page(CAPS_PAGE_IDENTITY)
    board_id = reply[4:12].hex().upper()
    strings = reply[12:].split(b'\x00')
    label = strings[0].decode('ascii', 'replace')
    firmware = strings[1].decode('ascii', 'replace') if len(strings) > 1 else ''
    return board_id, label, firmware


def read_state(device: HostLightingDevice) -> dict:
    """Read and decode GET_CAPS page 1, the board's runtime state.

    :param device: an opened HostLightingDevice
    :return: the decoded page, including the LED-map fingerprint
    """
    return decode_state(device.get_caps_page(CAPS_PAGE_STATE))


def read_led_map(device: HostLightingDevice) -> dict:
    """Read and decode GET_CAPS page 2, the per-control LED map.

    :param device: an opened HostLightingDevice
    :return: the decoded page
    """
    return decode_led_map(device.get_caps_page(CAPS_PAGE_LED_MAP))


def read_protocol_version(device: HostLightingDevice) -> tuple:
    """Read the board's protocol version from its PING reply.

    PING reply layout: [3..6] the ASCII magic "GPHL", [7] major, [8] minor.

    :param device: an opened HostLightingDevice
    :return: the (major, minor) the board reports
    :raises HostLightingError: if the reply is not a Host Lighting one
    """
    reply = device.request_ok(CMD_PING)
    _require_length(reply, 9, 'PING')
    if reply[3:7] != PING_MAGIC:
        raise HostLightingError("handshake failed: this interface did not answer as "
                                "a Host Lighting one")
    return reply[7], reply[8]


def read_light_table(device: HostLightingDevice, caps: Capabilities) -> list:
    """Read the board's light table, where it has one to report.

    Gated on the page 1 feature bit as well as the version, because a cleared
    bit is a promise the page returns nothing rather than a hint that it might.
    Firmware too old for the page answers INVALID_ARG, which the protocol says
    to read as "not supported by this firmware" rather than as a failure, so the
    oldest boards take the quietest path rather than the loudest one.

    :param device: an opened HostLightingDevice
    :param caps: the negotiated capabilities
    :return: the board's light records, empty where it has none to give
    """
    if not caps.light_table:
        return []
    try:
        records, _ = device.read_lights()
    except HostLightingRejected:
        return []
    return records


def negotiate(device: HostLightingDevice, force: bool = False) -> Capabilities:
    """Agree what the board can do, once, before any streaming starts.

    A minor version above the highest supported is clamped down rather than
    refused: within a major version the protocol only ever adds, so a newer
    board still honours everything this client knows how to ask for. A different
    major version is refused, because it is allowed to have changed the meaning
    of commands this client sends without asking.

    :param device: an opened HostLightingDevice
    :param force: run against an unsupported major version anyway
    :return: the negotiated Capabilities
    :raises HostLightingIncompatible: on an unsupported major version, unless forced
    :raises HostLightingError: if the board does not answer as a Host Lighting one
    """
    major, minor = read_protocol_version(device)
    unsupported = major != SUPPORTED_MAJOR
    if unsupported and not force:
        raise HostLightingIncompatible(
            f"board speaks Host Lighting v{major}.{minor}, and this bridge supports "
            f"v{SUPPORTED_MAJOR}.0 to v{SUPPORTED_MAJOR}.{MAX_SUPPORTED_MINOR}. A different "
            f"major version may have changed commands this bridge relies on, so it is not "
            f"driven blind. Pass --force to run anyway.")
    caps = Capabilities(major=major, minor=min(minor, MAX_SUPPORTED_MINOR),
                        reported=(major, minor), state=read_state(device),
                        led_map=read_led_map(device), forced=unsupported)
    caps.lights = read_light_table(device, caps)
    return caps


def open_device(board_id_prefix: str = '') -> HostLightingDevice:
    """Open a Host Lighting device, disambiguating by board ID if needed.

    :param board_id_prefix: optional hex prefix of the page 0 factory board ID
    :return: an opened HostLightingDevice
    """
    infos = find_devices()
    if not infos:
        raise HostLightingError(
            "no Host Lighting interface found - is a board connected with the add-on enabled? "
            "The add-on is not in an official GP2040-CE release yet; test builds are at "
            "https://github.com/djGLiTCH/GP2040-CE/releases/tag/HLP_v1.3")
    candidates = []
    for info in infos:
        device = HostLightingDevice(info['path'])
        try:
            board_id, _, _ = read_identity(device)
        except HostLightingError:
            device.close()
            continue
        if board_id.startswith(board_id_prefix.upper()):
            candidates.append((board_id, device))
        else:
            device.close()
    if not candidates:
        raise HostLightingError(f"no board matches ID prefix '{board_id_prefix}'")
    if len(candidates) > 1:
        ids = ', '.join(board_id for board_id, _ in candidates)
        for _, device in candidates:
            device.close()
        raise HostLightingError(f"multiple boards found ({ids}) - select one with --board-id")
    return candidates[0][1]
