"""Talk to a GP2040-CE board's Host Lighting interface over HID.

The Host Lighting add-on exposes a vendor HID interface (usage page 0xFF47,
usage 0x0002) carrying the Host Lighting Protocol (HLP) 2.0. Its fixed
64-byte reports let host software drive the board's RGB LEDs live and read
the board's lights and controls from its own configuration.

A request is `[0] command, [1] sequence, [2] flags, [3..] payload`. Its reply
is `[0] command|0x80, [1] sequence, [2] status, [3..] payload`. An event
arrives with `[0] = 0x80`.

HELLO names the board and lists what it implements, and GET_PAGE reads its
pages. This module reads the five the bridge needs:

* page 0 (identity) gives the board label and firmware version, for display.
* page 1 (state) gives whether a host frame is shown, the holder and the
  claim generation, and the CONFIGURE settings in force. A STATE_CHANGED
  event carries the same record.
* page 8 (summary) gives the LED extent and addressable limit, the render
  rate, the colour format, and a change counter for each configuration page.
* page 10 (controls) lists every control with an action, lit or not. It is
  the only page that can say a control does not exist.
* page 11 (lights) lists every light, with its LEDs, kind and owning action.

Controls are named by GP2040-CE's own GpioAction values and lights by their
LightType, so STAGE by action or by kind reaches every light carrying it
without a table read. Pages 2, 9 and 12 (input modes, profiles, animations)
are part of the protocol and unread here.

The framing, the reply matching and the page decoders follow the reference
host in gp2040ce-binary-tools (gp2040ce_bintools/hostlighting.py). The
capability layer and the timeout and disconnect handling are this project's
own, so the module is not a drop-in for it.

See docs/host-lighting.md in the GP2040-CE repository for the protocol
reference.

SPDX-FileCopyrightText: (C) 2026 Jacob Simpson
SPDX-License-Identifier: GPL-3.0-or-later
"""
import secrets
import struct
import time

# Discovery matches the interface by these and never by VID:PID, which varies by input mode.
# The usage is the protocol major version, so only boards of this major are found.
USAGE_PAGE = 0xFF47
USAGE = 0x0002
REPORT_SIZE = 64
MAGIC = b'GPHL'
PROTOCOL_MAJOR = 2
# The message for a run that finds no interface. The link names the firmware builds this release was built against.
NO_INTERFACE = ("no Host Lighting interface found. Connect a board with the add-on enabled. "
                "The add-on is not in an official GP2040-CE release yet. Test builds are at "
                "https://github.com/djGLiTCH/GP2040-CE/releases/tag/HLP_v2.0-rc7")
# the usage pre-release protocol 1.x firmware presents on the same usage page
USAGE_PROTOCOL_1 = 0x4C

# command IDs, grouped by function range
CMD_HELLO = 0x01                # session and discovery, 0x01-0x0F
CMD_GET_PAGE = 0x02
CMD_CLAIM = 0x03
CMD_CONFIGURE = 0x04
CMD_SUBSCRIBE = 0x05
CMD_STAGE = 0x10                # frame staging, 0x10-0x1F
CMD_CLEAR = 0x13
CMD_COMMIT = 0x20               # frame lifecycle, 0x20-0x2F
CMD_RELEASE = 0x21

# request flags, the same on every command
FLAG_COMMIT_AFTER = 0x01        # publish the staged frame after this command succeeds
FLAG_NO_REPLY = 0x02            # send no reply
FLAG_KEEPALIVE = 0x04           # refresh a live takeover's keepalive
# bits 4-6 carry the claim generation the sender acts under, 1-7, or 0 while it holds no claim
FLAG_GENERATION_SHIFT = 4
FLAG_GENERATION_MASK = 0x70

REPLY_FLAG = 0x80
EVENT_MARKER = 0x80             # [0] of an event, which no reply carries
STATUS_OK = 0
STATUS_UNSUPPORTED = 1
STATUS_INVALID = 2
STATUS_STALE = 3
STATUS_CLAIMED = 4
STATUS_NAMES = {STATUS_OK: 'OK', STATUS_UNSUPPORTED: 'UNSUPPORTED', STATUS_INVALID: 'INVALID_ARG',
                STATUS_STALE: 'STALE_TOKEN', STATUS_CLAIMED: 'CLAIMED'}
EVENT_STATE_CHANGED = 0

# pages
PAGE_IDENTITY = 0
PAGE_STATE = 1
PAGE_SUMMARY = 8
PAGE_CONTROLS = 10
PAGE_LIGHTS = 11

# A page reply's header ends at [11]. The records sit at [12..59] and the state token at [60..63].
RECORDS_OFFSET = 12
RECORDS_AREA = 48
TOKEN_OFFSET = 60

# each page's field groups, in mask bit order, and their widths in bytes. A group's width never
# changes once it ships, and a later 2.x board adds groups after these.
PAGE_GROUPS = {
    PAGE_STATE: (9, 22, 4),
    PAGE_SUMMARY: (28, 8),
    PAGE_CONTROLS: (4, 4, 6, 4),
    PAGE_LIGHTS: (4, 2, 3, 4, 2, 1),
}
# The groups read here, as GET_PAGE field masks. A mask always names its groups, since mask 0
# also returns the groups a later board adds.
STATE_GROUPS = 0x07             # State, Controller, Settings
SUMMARY_GROUPS = 0x03           # Summary, Changes
CONTROL_GROUPS = 0x01           # key, action, flags
LIGHT_GROUPS = 0x07             # LEDs, kind and flags, owner pin and action
IDENTITY_LABEL = 0              # page 0 group bits
IDENTITY_FIRMWARE = 3
IDENTITY_STRING_MAX = 48

# the none values, which never equal a key
NONE_8 = 0xFF
ACTION_NONE = -32768            # 0x8000 as a signed 16-bit action

# staging: [3] bits 0-3 addressing, bits 4-7 pixel format
ADDRESSING_LED_RUN = 0
ADDRESSING_LIGHT = 4
ADDRESSING_ACTION = 6
ADDRESSING_KIND = 7
ADDRESS_SIZES = {ADDRESSING_LIGHT: 2, ADDRESSING_ACTION: 2, ADDRESSING_KIND: 1}
PIXEL_RGB = 0
PIXEL_RGBW = 1
PIXEL_SIZES = {PIXEL_RGB: 3, PIXEL_RGBW: 4}
OUTCOME_BITS = 32               # the STAGE outcome mask, which bounds a report's entries

# CLAIM actions and the application name's size
CLAIM_TAKE = 0                  # take the claim when unclaimed, keep it when held
CLAIM_TAKE_OVER = 1
CLAIM_GIVE_UP = 2
CLAIM_NAME_SIZE = 16
CLAIM_EXCLUSIVE = 0x01
CONTROLLER_SIZE = 22            # page 1's Controller group, which the CLAIM and status 4 replies carry

TAKEOVER_OVERLAY = 1

# Summary colour formats. Only the W formats have a white emitter, so a white component reaches
# no LED on the others.
LED_FORMAT_NAMES = {0: 'GRB', 1: 'RGB', 2: 'GRBW', 3: 'RGBW'}
WHITE_LED_FORMATS = frozenset({2, 3})

# The control names a profile may use, as GP2040-CE's GpioAction values (proto/enums.proto).
# STAGE by action reaches every light carrying it, so a control wired to two buttons lights both.
ACTION_NAMES = ['Up', 'Down', 'Left', 'Right', 'B1', 'B2', 'B3', 'B4', 'L1', 'R1', 'L2', 'R2',
                'S1', 'S2', 'A1', 'A2', 'L3', 'R3']
ACTIONS = {name: action for action, name in enumerate(ACTION_NAMES, start=1)}
ACTIONS.update({'A3': 41, 'A4': 42})
ACTIONS.update({f'E{n + 1}': 43 + n for n in range(12)})
# The lights with no control behind them, as LightType values. STAGE by kind reaches every light
# of the kind.
KINDS = {'CASE': 1, 'TURBO': 2, 'PLED1': 3, 'PLED2': 4, 'PLED3': 5, 'PLED4': 6}
KIND_BUTTON = 0

# every spelling a profile may use, as the (addressing, address) STAGE reaches it by, and the
# reverse for reporting
CONTROL_TARGETS = {name.upper(): (ADDRESSING_ACTION, action) for name, action in ACTIONS.items()}
CONTROL_TARGETS.update({name: (ADDRESSING_KIND, kind) for name, kind in KINDS.items()})
CONTROL_NAMES = {(ADDRESSING_ACTION, action): name for name, action in ACTIONS.items()}
CONTROL_NAMES.update({(ADDRESSING_KIND, kind): name for name, kind in KINDS.items()})

# What the bridge cannot run without, checked against HELLO's bitmaps. The addressing modes are
# added per profile.
REQUIRED = {
    'commands': (CMD_HELLO, CMD_GET_PAGE, CMD_CLAIM, CMD_CONFIGURE, CMD_STAGE, CMD_CLEAR),
    'flags': (0, 1, 2, 4, 5, 6),
    'pages': (PAGE_STATE, PAGE_SUMMARY, PAGE_LIGHTS),
    'pixel_formats': (PIXEL_RGB,),
}
CAPABILITY_LABELS = {'commands': 'command 0x{:02X}', 'flags': 'request flag bit {}', 'pages': 'page {}',
                     'addressing': 'addressing mode {}', 'pixel_formats': 'pixel format {}'}


def _u16(data: bytes, offset: int) -> int:
    return struct.unpack_from('<H', data, offset)[0]


def _s16(data: bytes, offset: int) -> int:
    return struct.unpack_from('<h', data, offset)[0]


def _u32(data: bytes, offset: int) -> int:
    return struct.unpack_from('<I', data, offset)[0]


def _string(data: bytes) -> str:
    """Decode a NUL-padded string, replacing a partial character at a cut end."""
    return data.split(b'\x00')[0].decode('utf-8', 'replace')


def _bits(value: int) -> set:
    """List the set bit numbers of a bitmap."""
    return {bit for bit in range(value.bit_length()) if value & (1 << bit)}


def build_request(command: int, sequence: int, payload: bytes = b'', flags: int = 0) -> bytes:
    """Frame a request as a 64-byte report.

    :param command: command byte (0x01-0x7F)
    :param sequence: sequence byte echoed by the board in its reply
    :param payload: the command's bytes from [3], at most 61
    :param flags: request flags (FLAG_*)
    :return: the request framed to REPORT_SIZE bytes
    """
    if len(payload) > REPORT_SIZE - 3:
        raise ValueError(f"payload too long ({len(payload)} > {REPORT_SIZE - 3})")
    return bytes([command, sequence, flags]) + payload + bytes(REPORT_SIZE - 3 - len(payload))


def match_reply(reply: bytes, command: int, sequence: int) -> bool:
    """Check whether a report answers the given request.

    :param reply: a report as read from the interface
    :param command: the command byte of the original request
    :param sequence: the sequence byte of the original request
    :return: True if the reply's command echo and sequence match
    """
    return len(reply) >= 3 and reply[0] == (command | REPLY_FLAG) and reply[1] == sequence


def is_event(report: bytes) -> bool:
    """Say whether a received report is an event."""
    return len(report) >= 3 and report[0] == EVENT_MARKER


def control_name(control) -> str:
    """Render a control as the name a profile uses for it.

    :param control: an (addressing, address) pair from CONTROL_TARGETS
    :return: the control name, or the raw address if it has none
    """
    if control in CONTROL_NAMES:
        return CONTROL_NAMES[control]
    return f"{'kind' if control[0] == ADDRESSING_KIND else 'action'} {control[1]}"


class HostLightingError(RuntimeError):
    """Errors talking to a Host Lighting interface."""


class HostLightingRejected(HostLightingError):
    """The board answered a command with a status other than 0."""

    def __init__(self, command: int, status: int, offset: int = 0, holder=None):
        """Record the rejected command, its status, and the byte at fault or the claim's holder.

        :param command: the command the board refused
        :param status: the reply's status byte
        :param offset: for status 2, the offset of the invalid request byte
        :param holder: for status 4, the decoded Controller group the reply shows
        """
        self.command = command
        self.status = status
        self.offset = offset
        self.holder = holder
        detail = f" at request byte {offset}" if status == STATUS_INVALID else ''
        if status == STATUS_CLAIMED and holder:
            detail = (f" by '{holder['holder_name']}'" if holder['holder_token']
                      else ": the claim has ended")
        super().__init__(f"command 0x{command:02X} rejected: "
                         f"{STATUS_NAMES.get(status, hex(status))}{detail}")


class HostLightingTimeout(HostLightingError):
    """No reply arrived in time.

    The board answers nothing for about 200 ms while it writes its settings
    flash, so a caller can carry on after one of these.
    """


class HostLightingIncompatible(HostLightingError):
    """The board does not speak this protocol major, or lacks something the bridge needs.

    The board and the link are sound. The firmware does not offer what is
    asked of it.
    """


class HostLightingDisconnected(HostLightingError):
    """The underlying HID device failed.

    The board was unplugged or rebooted, so a caller cannot carry on.
    """


class HostLightingUnanswered(HostLightingDisconnected):
    """The board is on the bus and answers nothing.

    A caller treats it as a departed board and reports it as one that stopped
    answering.
    """


def _require_length(reply: bytes, length: int, what: str) -> None:
    """Refuse a reply too short for what is about to be read out of it.

    Reports are a fixed 64 bytes and every field sits at a fixed offset inside
    one, so a short reply is a truncated or malformed read. It is reported
    here before a decoder indexes past its end.

    :param reply: the reply report to check
    :param length: how many bytes the caller is about to read
    :param what: what is being decoded, for the error message
    """
    if len(reply) < length:
        raise HostLightingError(f"{what} reply is {len(reply)} bytes, need at least {length}")


def decode_hello(reply: bytes) -> dict:
    """Decode a HELLO reply.

    HELLO reply layout: [3..6] the ASCII magic "GPHL", [7] major, [8] minor,
    [9..16] board ID, [17] reply queue limit, [18] request flags, [19..34]
    commands, [35..38] pages, [39..40] addressing modes, [41..42] pixel formats,
    [43..44] events, [45..46] events enabled now. In the bitmaps at [18..44] a
    set bit n means the board implements member n.

    :param reply: a HELLO reply report
    :return: the version, board ID as hex, queue limit and the bitmaps as sets
    :raises HostLightingError: if the reply is not a Host Lighting one
    """
    _require_length(reply, 47, 'HELLO')
    if reply[3:7] != MAGIC:
        raise HostLightingError("handshake failed: this interface did not answer as "
                                "a Host Lighting one")
    return {
        'major': reply[7],
        'minor': reply[8],
        'board_id': bytes(reply[9:17]).hex().upper(),
        'queue_limit': reply[17],
        'flags': _bits(reply[18]),
        'commands': _bits(int.from_bytes(reply[19:35], 'little')),
        'pages': _bits(_u32(reply, 35)),
        'addressing': _bits(_u16(reply, 39)),
        'pixel_formats': _bits(_u16(reply, 41)),
        'events': _bits(_u16(reply, 43)),
        'subscribed': _bits(_u16(reply, 45)),
    }


def decode_page_header(reply: bytes) -> dict:
    """Decode the header every GET_PAGE reply carries.

    Layout: [3] page, [4..5] start record, [6..7] total records, [8] records in
    this reply, [9] record stride, [10..11] field groups returned, [60..63] the
    state token. The page, start and groups echo the request.

    :param reply: a GET_PAGE reply report, or a STATE_CHANGED event
    :return: the decoded header
    """
    _require_length(reply, REPORT_SIZE, 'page')
    return {
        'status': reply[2],
        'page': reply[3],
        'start': _u16(reply, 4),
        'total': _u16(reply, 6),
        'count': reply[8],
        'stride': reply[9],
        'groups': _u16(reply, 10),
        'token': _u32(reply, TOKEN_OFFSET),
    }


def decode_controller(data: bytes) -> dict:
    """Decode page 1's Controller group, the application holding CLAIM.

    The CLAIM reply carries the same 22 bytes at [8..29] and a status 4 reply
    at [3..24].

    :param data: the group's bytes
    :return: the holder's token (0 unclaimed) and name, whether its claim is
        exclusive, and the claim generation
    """
    return {'holder_token': _u32(data, 0), 'holder_name': _string(data[4:20]),
            'claim_exclusive': bool(data[20] & CLAIM_EXCLUSIVE), 'claim_generation': data[21]}


def _state_group(group: int, data: bytes) -> dict:
    if group == 0:
        return {'takeover_live': bool(data[0] & 0x01), 'input_mode': data[1], 'profile': data[2],
                'player': data[3], 'brightness_step': data[6], 'takeover_number': data[7]}
    if group == 1:
        return decode_controller(data)
    return {'overlay': data[0] == TAKEOVER_OVERLAY, 'keepalive_ms': _u16(data, 1),
            'brightness_applied': bool(data[3])}


def _summary_group(group: int, data: bytes) -> dict:
    if group == 1:
        # one change counter for each configuration page, 8 to 15
        return {'page_changes': {PAGE_SUMMARY + index: count for index, count in enumerate(data[:8])}}
    return {'led_extent': _u16(data, 0), 'led_limit': _u16(data, 2), 'render_hz': _u16(data, 4),
            'colour_format': data[6], 'controls': _u16(data, 16), 'lit_controls': _u16(data, 18),
            'unlit_controls': _u16(data, 20), 'lights': _u16(data, 22)}


def _control_group(group: int, data: bytes) -> dict:
    if group == 0:
        return {'key': data[0], 'action': _s16(data, 1),
                'duplicated': bool(data[3] & 0x01), 'lit': bool(data[3] & 0x02)}
    return {}


def _light_group(group: int, data: bytes) -> dict:
    if group == 0:
        return {'first_led': _u16(data, 0), 'led_count': _u16(data, 2)}
    if group == 1:
        return {'kind': data[0], 'duplicated': bool(data[1] & 0x01)}
    if group == 2:
        action = _s16(data, 1)
        return {'owner_pin': None if data[0] == NONE_8 else data[0],
                'owner_action': None if action == ACTION_NONE else action}
    return {}


GROUP_DECODERS = {
    PAGE_STATE: _state_group,
    PAGE_SUMMARY: _summary_group,
    PAGE_CONTROLS: _control_group,
    PAGE_LIGHTS: _light_group,
}


def decode_records(reply: bytes) -> list:
    """Decode the records of a GET_PAGE reply, field group by field group.

    The reply takes the requested groups in bit order, echoes those it returned
    at [10..11] and sets the stride to their total width. Records are stepped by
    that stride, so a group a later board adds after them does not misalign the
    read.

    :param reply: the reply report of page 1, 8, 10 or 11, or a STATE_CHANGED event
    :return: the records, one dict each, with `ordinal` the record's index in the page
    :raises HostLightingError: if the header does not describe records that fit
    """
    header = decode_page_header(reply)
    if header['page'] not in PAGE_GROUPS:
        raise HostLightingError(f"page {header['page']} is not one this client decodes")
    widths = PAGE_GROUPS[header['page']]
    decode = GROUP_DECODERS[header['page']]
    known = sum(width for group, width in enumerate(widths) if header['groups'] & (1 << group))
    if header['count'] and header['stride'] < known:
        raise HostLightingError(f"page {header['page']} record stride is {header['stride']}, "
                                f"need at least {known}")
    if header['count'] * header['stride'] > RECORDS_AREA:
        raise HostLightingError(f"page {header['page']} claims {header['count']} records of "
                                f"{header['stride']} bytes, more than a reply holds")
    records = []
    for index in range(header['count']):
        offset = RECORDS_OFFSET + index * header['stride']
        record = {'ordinal': header['start'] + index}
        for group, width in enumerate(widths):
            if header['groups'] & (1 << group):
                record.update(decode(group, bytes(reply[offset:offset + width])))
                offset += width
        records.append(record)
    return records


def decode_state_changed(event: bytes) -> tuple:
    """Decode a STATE_CHANGED event, whose [3..63] are those of a page 1 reply.

    :param event: the event report ([1] is the event number)
    :return: (the page 1 record, the state token)
    :raises HostLightingError: for any other event, or one that is not page 1
    """
    _require_length(event, REPORT_SIZE, 'event')
    if event[1] != EVENT_STATE_CHANGED or event[3] != PAGE_STATE:
        raise HostLightingError(f"event {event[1]} is not STATE_CHANGED")
    records = decode_records(event)
    if not records:
        raise HostLightingError("STATE_CHANGED carries no record")
    return records[0], _u32(event, TOKEN_OFFSET)


def decode_identity(reply: bytes) -> dict:
    """Decode a page 0 reply's strings.

    Each returned group is a string of up to 48 bytes, NUL-terminated when
    shorter, starting after the previous one's NUL or 48th byte. Only the
    groups the reply echoes are present.

    :param reply: a page 0 reply report
    :return: {group bit: string} for the groups the reply echoes
    """
    header = decode_page_header(reply)
    area = bytes(reply[RECORDS_OFFSET:RECORDS_OFFSET + RECORDS_AREA])
    strings = {}
    used = 0
    for group in sorted(_bits(header['groups'])):
        raw = area[used:used + IDENTITY_STRING_MAX].split(b'\x00')[0]
        strings[group] = raw.decode('utf-8', 'replace')
        used += len(raw) + 1 if len(raw) < IDENTITY_STRING_MAX else IDENTITY_STRING_MAX
    return strings


def subtractive_white(colour) -> tuple:
    """Convert a profile colour into the RGBW a white chain is sent.

    W takes min(R, G, B) and the three colour channels are reduced by it. An
    RGBW pixel renders as sent, so the white part of a colour is shown by the
    white emitter alone.

    A white component the profile gave is added to W and clamped at 255.

    :param colour: (r, g, b), or (r, g, b, w) as the profile gave it
    :return: (r, g, b, w) to put on the wire
    """
    red, green, blue = colour[:3]
    achromatic = min(red, green, blue)
    asked = colour[3] if len(colour) > 3 else 0
    return (red - achromatic, green - achromatic, blue - achromatic,
            min(255, achromatic + asked))


def has_white_channel(colour_format: int) -> bool:
    """Say whether a Summary colour format has a white emitter to drive.

    :param colour_format: the Summary colour format byte
    :return: True if the chain carries a white channel a W component can reach
    """
    return colour_format in WHITE_LED_FORMATS


def stage_capacity(addressing: int, pixel_format: int) -> int:
    """How many run members or entries one STAGE report carries.

    :param addressing: ADDRESSING_* mode
    :param pixel_format: PIXEL_RGB or PIXEL_RGBW
    :return: the per-report capacity
    """
    pixel = PIXEL_SIZES[pixel_format]
    if addressing == ADDRESSING_LED_RUN:
        capacity = (REPORT_SIZE - 7) // pixel
    else:
        capacity = (REPORT_SIZE - 5) // (ADDRESS_SIZES[addressing] + pixel)
    return min(capacity, OUTCOME_BITS)


def _pixel(colour, pixel_format: int) -> bytes:
    """Encode one colour, refusing one of the wrong width for its format.

    The count byte tells the firmware how many fixed-width entries follow. A
    pixel of the wrong width shifts every entry after it, and the board does
    not reject the report.
    """
    if len(colour) != PIXEL_SIZES[pixel_format]:
        raise ValueError(f"pixel format {pixel_format} takes {PIXEL_SIZES[pixel_format]}-byte "
                         f"colours, got {len(colour)}")
    return bytes(colour)


def entry_payloads(addressing: int, entries: list, pixel_format: int = PIXEL_RGB) -> list:
    """Build the STAGE payloads that colour entries addressed one by one.

    Entries layout: [3] bits 0-3 addressing, bits 4-7 pixel format, [4] n,
    [5..] n x (address, pixel). An action is signed 16-bit, a light ordinal
    16-bit and a kind 8-bit.

    :param addressing: ADDRESSING_LIGHT, ADDRESSING_ACTION or ADDRESSING_KIND
    :param entries: (address, colour) pairs, the colour as wide as the format
    :param pixel_format: PIXEL_RGB or PIXEL_RGBW
    :return: one payload per report needed, in order
    """
    pack = {ADDRESSING_ACTION: '<h', ADDRESSING_LIGHT: '<H', ADDRESSING_KIND: '<B'}[addressing]
    capacity = stage_capacity(addressing, pixel_format)
    payloads = []
    for start in range(0, len(entries), capacity):
        chunk = entries[start:start + capacity]
        payloads.append(bytes([addressing | (pixel_format << 4), len(chunk)]) + b''.join(
            struct.pack(pack, address) + _pixel(colour, pixel_format) for address, colour in chunk))
    return payloads


def run_payloads(first: int, count: int, colour, pixel_format: int = PIXEL_RGB) -> list:
    """Build the STAGE payloads that colour a run of LEDs one colour.

    Run layout: [3] bits 0-3 addressing 0, bits 4-7 pixel format, [4..5] first
    LED, [6] count, [7..] count x pixel.

    :param first: first LED index
    :param count: how many LEDs to colour
    :param colour: the colour to repeat across them, as wide as the format
    :param pixel_format: PIXEL_RGB or PIXEL_RGBW
    :return: one payload per report needed, in order
    """
    capacity = stage_capacity(ADDRESSING_LED_RUN, pixel_format)
    pixel = _pixel(colour, pixel_format)
    payloads = []
    for offset in range(0, count, capacity):
        chunk = min(capacity, count - offset)
        payloads.append(bytes([ADDRESSING_LED_RUN | (pixel_format << 4)])
                        + struct.pack('<HB', first + offset, chunk) + pixel * chunk)
    return payloads


def lacking(hello: dict, addressing=()) -> list:
    """List what the bridge needs that a board's HELLO does not offer.

    A host gates on HELLO's capability bitmaps and never on the minor version.

    :param hello: a decoded HELLO reply
    :param addressing: the addressing modes the profile stages by
    :return: what is missing, named for a message, or an empty list
    """
    needs = dict(REQUIRED, addressing=tuple(addressing))
    return [CAPABILITY_LABELS[kind].format(member) for kind, members in needs.items()
            for member in members if member not in hello[kind]]


class HostLightingCapabilities:
    """What the board on the other end offers, read once at connect.

    Call sites ask this object what is available, so the pages are decoded in
    one place and a run with no board answers every question with no.
    """

    def __init__(self, hello=None):
        """Hold a HELLO reply and the pages read alongside it.

        :param hello: the decoded HELLO reply, or None for no board
        """
        self.hello = hello
        self.summary = {}
        self.token = None
        self.lights = []
        # None where the board has no Controls page, so nothing can be said to be absent
        self.controls = None

    @classmethod
    def absent(cls):
        """Describe a run with no board, where nothing was read.

        :return: a HostLightingCapabilities describing no board at all
        """
        return cls()

    @property
    def connected(self) -> bool:
        """Whether these capabilities came from a board."""
        return self.hello is not None

    @property
    def events(self) -> bool:
        """Whether the board offers the STATE_CHANGED event, so its state need not be polled."""
        return (self.connected and CMD_SUBSCRIBE in self.hello['commands']
                and EVENT_STATE_CHANGED in self.hello['events'])

    @property
    def white_channel(self) -> bool:
        """Whether the board's chain has a white emitter for a W component to reach."""
        return has_white_channel(self.summary.get('colour_format', 0))

    @property
    def host_white(self) -> bool:
        """Whether a host-supplied white component can be sent and will render.

        An RGBW pixel may be sent to any chain, but its W renders only on a
        chain with a white emitter, and the board has to list the format.
        """
        return self.white_channel and self.connected and PIXEL_RGBW in self.hello['pixel_formats']

    @property
    def render_hz(self):
        """The board's render rate in Hz, or None if it did not say."""
        return self.summary.get('render_hz') or None

    @property
    def led_extent(self) -> int:
        """The highest LED index in use plus one, the number a frame is sized from."""
        return self.summary.get('led_extent', 0)

    @property
    def led_limit(self) -> int:
        """The addressable LED limit. An LED run reaching past it stages nothing."""
        return self.summary.get('led_limit', 0)

    def lights_of(self, control) -> list:
        """List the lights STAGE reaches through a control, in ordinal order.

        A light whose LEDs all lie at or past the addressable limit is on the
        page but skipped by STAGE, so it is left out here too.

        :param control: an (addressing, address) pair from CONTROL_TARGETS
        :return: the page 11 records STAGE by that action or kind colours
        """
        field = 'kind' if control[0] == ADDRESSING_KIND else 'owner_action'
        return [record for record in self.lights
                if record[field] == control[1] and record['led_count']
                and record['first_led'] < self.led_limit]

    def lacks_control(self, control) -> bool:
        """Say whether the Controls page shows the board has no such control.

        False where page 10 cannot say, which is when the board has no such
        page or the target is a kind of light.

        :param control: an (addressing, address) pair from CONTROL_TARGETS
        :return: True only if the Controls page leaves the control's action out
        """
        if self.controls is None or control[0] != ADDRESSING_ACTION:
            return False
        return all(record['action'] != control[1] for record in self.controls)

    def refresh(self, device, attempts: int = 3) -> bool:
        """Read the Summary, and the pages whose change counter moved since the last read.

        The Summary's change counters name the pages that changed, so a page
        whose counter stands is not read again. Each walk names the Summary's
        state token as the one it expects. A walk the board answers with
        status 3 starts over from the Summary, so the pages kept never mix two
        states. Nothing is replaced until every read has landed.

        :param device: an opened HostLightingDevice
        :param attempts: how many times to start over before giving up
        :return: True if the Lights or the Controls page was read again, or a
            Summary field changed
        :raises HostLightingError: if the configuration keeps changing under the reads
        """
        for _ in range(attempts):
            summary, token = device.read_record(PAGE_SUMMARY, SUMMARY_GROUPS)
            before = self.summary.get('page_changes', {})
            now = summary['page_changes']
            lights, controls = self.lights, self.controls
            if now[PAGE_LIGHTS] != before.get(PAGE_LIGHTS):
                lights = device.read_table(PAGE_LIGHTS, LIGHT_GROUPS, token)
            if PAGE_CONTROLS in self.hello['pages'] and now[PAGE_CONTROLS] != before.get(PAGE_CONTROLS):
                controls = device.read_table(PAGE_CONTROLS, CONTROL_GROUPS, token)
            if lights is None or (controls is None and PAGE_CONTROLS in self.hello['pages']):
                continue    # the token moved under a walk
            changed = (lights is not self.lights or controls is not self.controls
                       or any(value != self.summary.get(field) for field, value in summary.items()
                              if field != 'page_changes'))
            self.summary, self.token, self.lights, self.controls = summary, token, lights, controls
            return changed
        raise HostLightingError("the board's configuration kept changing while it was being read")

    def summary_line(self) -> str:
        """Name the protocol version and what the board reported, for the startup line.

        :return: a one-line summary of the board
        """
        if not self.connected:
            return "no board: running without one, so nothing was read"
        chain = LED_FORMAT_NAMES.get(self.summary.get('colour_format'), 'unknown')
        offers = [f"{len(self.lights)} lights on {self.led_extent} LEDs",
                  f"renders at {self.render_hz} Hz" if self.render_hz else 'render rate unstated',
                  f"{chain} chain (white channel: {'yes' if self.white_channel else 'no'})",
                  'state changes by event' if self.events else 'state changes by poll']
        return (f"board speaks HLP v{self.hello['major']}.{self.hello['minor']}: "
                f"{', '.join(offers)}")


class HostLightingDevice:
    """One GP2040-CE board's Host Lighting interface."""

    def __init__(self, path: bytes):
        """Open the HID device at the given hidapi path.

        :param path: platform-specific hidapi device path from enumeration
        """
        hid = _import_hid()
        device = hid.device()
        device.open_path(path)
        # a read with no timeout returns at once, so the reports already received can be drained
        device.set_nonblocking(True)
        self._bind(device)

    @classmethod
    def from_hid(cls, device):
        """Wrap an already-open hidapi device object.

        A fake board and a real one are both initialised through _bind, so
        per-session state added there reaches both.

        :param device: an object with the hidapi read/write/close interface
        :return: a HostLightingDevice driving it
        """
        wrapper = cls.__new__(cls)
        wrapper._bind(device)
        return wrapper

    def _bind(self, device) -> None:
        """Attach an open hidapi device and initialise per-session state."""
        self.device = device
        self.open = True
        # random, so two applications sharing the board seldom send the same sequence number
        self.sequence = secrets.randbelow(255)
        self.events = []
        # the requests sent whose replies are awaited, and those replies when read while waiting for another
        self.awaited = set()
        self.pending = []
        # the claim generation this application holds the claim under, or 0 while it holds none
        self.generation = 0

    def close(self) -> None:
        """Close the HID device, once."""
        if self.open:
            self.open = False
            self.device.close()

    def _read(self, timeout_ms: int) -> bytes:
        """Read one report, waiting up to a timeout, or return empty when none arrived.

        The only place a report is read. hidapi raises when the read fails.
        """
        try:
            return bytes(self.device.read(REPORT_SIZE, timeout_ms))
        except OSError as error:
            raise HostLightingDisconnected(f"board went away while reading ({error})") from None

    def send(self, command: int, payload: bytes = b'', flags: int = 0) -> int:
        """Send one request without waiting for its reply.

        Every request carries the claim generation this application holds the
        claim under, which a request other than CLAIM is refused without once
        the holder has changed.

        :param command: command byte
        :param payload: the command's bytes from [3]
        :param flags: request flags (FLAG_*)
        :return: the sequence number sent
        """
        self.sequence = (self.sequence % 255) + 1
        flags |= self.generation << FLAG_GENERATION_SHIFT
        if not flags & FLAG_NO_REPLY:
            # no report kept for an earlier request with these numbers answers this one
            self.pending = [report for report in self.pending
                            if not match_reply(report, command, self.sequence)]
            self.awaited.add((command, self.sequence))
        try:
            self._write(command, build_request(command, self.sequence, payload, flags))
        except HostLightingDisconnected:
            # a request that was not sent has no reply to wait for
            self.awaited.discard((command, self.sequence))
            raise
        return self.sequence

    def _write(self, command: int, request: bytes) -> None:
        """Write one report, the only place a report is written.

        hidapi reports a failed write by a negative result and does not raise.

        :param command: the request's command byte, for the error message
        :param request: the 64-byte request
        :raises HostLightingDisconnected: if the device does not take the report
        """
        # the interface uses unnumbered reports, and hidapi takes a leading 0x00 report ID on write
        try:
            written = self.device.write(b'\x00' + request)
        except OSError as error:
            raise HostLightingDisconnected(f"board went away while sending "
                                           f"0x{command:02X} ({error})") from None
        if isinstance(written, int) and written < 0:
            raise HostLightingDisconnected(f"board went away while sending 0x{command:02X} (write error)")

    def _keep(self, report: bytes) -> None:
        """Keep an event or the reply to a request still awaited, and drop another application's reply."""
        if is_event(report):
            self.events.append(report)
        elif len(report) >= 3 and (report[0] & ~REPLY_FLAG, report[1]) in self.awaited:
            self.pending.append(report)

    def drain(self) -> None:
        """Read the reports already received, keeping the events and the replies still awaited.

        Replies and events reach every application with the interface open and
        wait until read. None of those waiting answers a request not yet sent,
        so they are read first. An idle application would otherwise take
        another's earlier reply with the same command and sequence for its own.
        """
        while True:
            report = self._read(0)
            if not report:
                return
            self._keep(report)

    def take_events(self) -> list:
        """Hand over the events received so far, oldest first.

        :return: the event reports, which are then forgotten here
        """
        self.drain()
        events, self.events = self.events, []
        return events

    def wait_reply(self, command: int, sequence: int, timeout: float = 0.5, accept=None) -> bytes:
        """Wait for the reply to one request.

        Each reply is matched by its command echo and sequence number. An
        event or another application's reply can arrive between two replies.

        :param command: the request's command byte
        :param sequence: the request's sequence number
        :param timeout: seconds to wait
        :param accept: optional test a matching report must also pass, to tell
            this application's reply from another's with the same numbers
        :return: the reply report
        :raises HostLightingTimeout: if no reply arrives in time
        """
        for report in [report for report in self.pending if match_reply(report, command, sequence)]:
            self.pending.remove(report)
            if accept is None or accept(report):
                self.awaited.discard((command, sequence))
                return report
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            report = self._read(max(1, int(left * 1000)))
            if not report:
                continue
            if not match_reply(report, command, sequence):
                self._keep(report)
            elif accept is None or accept(report):
                self.awaited.discard((command, sequence))
                return report
        self.awaited.discard((command, sequence))
        raise HostLightingTimeout(f"no reply to command 0x{command:02X} within {timeout}s")

    def request(self, command: int, payload: bytes = b'', flags: int = 0, timeout: float = 0.5,
                accept=None) -> bytes:
        """Send one command and wait for its matching reply.

        Callers keep the timeout at 250 ms or more. The board answers nothing
        for about 200 ms while it writes its settings flash.

        :param command: command byte
        :param payload: the command's bytes from [3]
        :param flags: request flags (FLAG_*)
        :param timeout: seconds to wait for the matching reply
        :param accept: optional test a matching report must also pass
        :return: the reply report ([2] is the status byte, [3..] the payload)
        """
        if flags & FLAG_NO_REPLY:
            raise ValueError("a request sent with FLAG_NO_REPLY gets no reply, so use send()")
        self.drain()
        return self.wait_reply(command, self.send(command, payload, flags), timeout, accept)

    @staticmethod
    def rejected(command: int, reply: bytes) -> HostLightingRejected:
        """Build the error for a refused request.

        A status 2 reply carries the offset of the invalid request byte at [3],
        and a status 4 reply the claim's holder at [3..24].

        :param command: the command the board refused
        :param reply: its reply report
        :return: the error to raise
        """
        if reply[2] != STATUS_CLAIMED:
            return HostLightingRejected(command, reply[2], reply[3] if len(reply) > 3 else 0)
        _require_length(reply, 3 + CONTROLLER_SIZE, 'status 4')
        return HostLightingRejected(command, reply[2], 0,
                                    decode_controller(bytes(reply[3:3 + CONTROLLER_SIZE])))

    def request_ok(self, command: int, payload: bytes = b'', flags: int = 0, timeout: float = 0.5,
                   accept=None) -> bytes:
        """Send one command and require status 0 in the reply.

        :param command: command byte
        :param payload: the command's bytes from [3]
        :param flags: request flags (FLAG_*)
        :param timeout: seconds to wait for the matching reply
        :param accept: optional test a matching report must also pass
        :return: the reply report
        :raises HostLightingRejected: on any other status
        """
        reply = self.request(command, payload, flags, timeout, accept)
        if reply[2] != STATUS_OK:
            raise self.rejected(command, reply)
        return reply

    def hello(self) -> dict:
        """Send HELLO and decode the reply.

        :return: the decoded reply (see decode_hello)
        :raises HostLightingError: if the reply is not a Host Lighting one
        """
        return decode_hello(self.request_ok(CMD_HELLO))

    def get_page(self, page: int, start: int = 0, mask: int = 0, token: int = 0) -> bytes:
        """Read one GET_PAGE reply.

        Request layout: [3] page, [4..5] start record, [6..9] expected state
        token (0 = no check), [10..11] field mask. A reply to another
        application's request with the same numbers is told apart by its echoed
        page, start and field groups.

        :param page: page number
        :param start: first record
        :param mask: field groups to return
        :param token: expected state token, 0 for no check
        :return: the reply report, with status 0 or 3
        :raises HostLightingRejected: on any other status
        """
        def echoes(reply: bytes) -> bool:
            if reply[2] not in (STATUS_OK, STATUS_STALE) or len(reply) < REPORT_SIZE:
                return True
            header = decode_page_header(reply)
            if header['page'] != page or header['start'] != start:
                return False
            return reply[2] == STATUS_STALE or not mask or not header['groups'] & ~mask

        reply = self.request(CMD_GET_PAGE, struct.pack('<BHIH', page, start, token, mask), accept=echoes)
        if reply[2] not in (STATUS_OK, STATUS_STALE):
            raise self.rejected(CMD_GET_PAGE, reply)
        return reply

    def read_record(self, page: int, mask: int) -> tuple:
        """Read a one-record page (1 or 8).

        :param page: page number
        :param mask: field groups to return
        :return: (the decoded record, the state token)
        :raises HostLightingError: if the page returned no record
        """
        reply = self.get_page(page, 0, mask)
        records = decode_records(reply)
        if not records:
            raise HostLightingError(f"page {page} returned no record")
        return records[0], decode_page_header(reply)['token']

    def read_table(self, page: int, mask: int, token: int):
        """Read every record of a table page under one state token.

        Every read names the token as the one it expects. If the board's
        configuration changes mid-walk the board answers status 3 and the walk
        is abandoned, since its records would mix two states.

        :param page: page number
        :param mask: field groups to return
        :param token: the state token the records must be read under
        :return: the decoded records, or None if the token is no longer current
        :raises HostLightingError: if the board stops making progress
        """
        records = []
        while True:
            reply = self.get_page(page, len(records), mask, token)
            if reply[2] == STATUS_STALE:
                return None
            header = decode_page_header(reply)
            found = decode_records(reply)
            if not found:
                if len(records) < header['total']:
                    raise HostLightingError(f"page {page} stalled at {len(records)} of "
                                            f"{header['total']} records")
                return records
            records.extend(found)
            if len(records) >= header['total']:
                return records

    def read_identity(self) -> tuple:
        """Read the board label and firmware version from page 0.

        The board returns the requested strings that fit in one reply, in bit
        order, and echoes which. A string that did not fit is asked for again.

        :return: (board label, firmware version), '?' for one the board did not give
        """
        strings = {}
        missing = (1 << IDENTITY_LABEL) | (1 << IDENTITY_FIRMWARE)
        while missing:
            found = decode_identity(self.get_page(PAGE_IDENTITY, 0, missing))
            if not found:
                break
            strings.update(found)
            for group in found:
                missing &= ~(1 << group)
        return strings.get(IDENTITY_LABEL, '?'), strings.get(IDENTITY_FIRMWARE, '?')

    def claim(self, action: int, token: int, name: bytes = b'', flags: int = 0) -> dict:
        """Claim the board, take it over or give it up.

        Request layout: [3] action, [4..7] host token, [8..23] application name,
        [24] claim flags. The reply echoes the action and token at [3..7], which
        tells it from another application's CLAIM reply, then shows the holder
        now at [8..29], laid out as page 1's Controller group.

        The generation is taken only when the holder is this application, and
        is then sent on every request. An application that holds no claim
        sends 0.

        :param action: CLAIM_TAKE, CLAIM_TAKE_OVER or CLAIM_GIVE_UP
        :param token: this application's non-zero token
        :param name: this application's name, up to 16 bytes
        :param flags: request flags (FLAG_*)
        :return: the holder now, as decode_controller gives it
        """
        payload = (bytes([action]) + struct.pack('<I', token)
                   + name[:CLAIM_NAME_SIZE].ljust(CLAIM_NAME_SIZE, b'\x00') + bytes([0]))

        def echoes(reply: bytes) -> bool:
            # an invalid argument's reply carries no echo
            return reply[2] != STATUS_OK or reply[3:8] == payload[:5]

        reply = self.request_ok(CMD_CLAIM, payload, flags, accept=echoes)
        _require_length(reply, 8 + CONTROLLER_SIZE, 'CLAIM')
        holder = decode_controller(bytes(reply[8:8 + CONTROLLER_SIZE]))
        self.generation = holder['claim_generation'] if holder['holder_token'] == token else 0
        return holder

    def configure(self, overlay: bool, keepalive_ms: int, brightness: bool) -> dict:
        """Set the takeover mode, keepalive timeout and brightness scaling.

        Layout, request and reply: [3] takeover (0 whole-frame, 1 overlay),
        [4..5] keepalive timeout in ms, [6] apply board brightness. The reply
        gives the settings applied, the timeout clamped to 100-10000.

        :param overlay: True for overlay takeover, False for whole-frame
        :param keepalive_ms: keepalive timeout in ms
        :param brightness: scale host pixels by the board's brightness
        :return: the settings the board applied
        """
        reply = self.request_ok(CMD_CONFIGURE,
                                struct.pack('<BHB', int(overlay), keepalive_ms, int(brightness)))
        _require_length(reply, 7, 'CONFIGURE')
        return {'overlay': reply[3] == TAKEOVER_OVERLAY, 'keepalive_ms': _u16(reply, 4),
                'brightness_applied': bool(reply[6])}

    def subscribe(self, events: int) -> int:
        """Enable events. The subscription is shared by every application on the board, so this only adds.

        :param events: bit n enables event n (EVENT_STATE_CHANGED is 0)
        :return: the events now enabled
        """
        reply = self.request_ok(CMD_SUBSCRIBE, struct.pack('<H', events))
        _require_length(reply, 5, 'SUBSCRIBE')
        return _u16(reply, 3)


def _import_hid():
    """Import the hidapi module, with a helpful error if it is missing."""
    try:
        import hid
    except ImportError as error:
        raise HostLightingError("this tool requires the hidapi package: pip install hidapi") from error
    return hid


def find_devices() -> list:
    """Enumerate all Host Lighting interfaces of this protocol major on the system.

    :return: list of hidapi enumeration dicts for matching interfaces
    """
    hid = _import_hid()
    return [info for info in hid.enumerate()
            if info.get('usage_page') == USAGE_PAGE and info.get('usage') == USAGE]


def find_other_majors() -> list:
    """Find the Host Lighting interfaces that speak another protocol major.

    The usage is the major version, so the usage page alone finds them. They
    are not opened.

    :return: the usage each such interface presents
    """
    hid = _import_hid()
    return [info.get('usage') for info in hid.enumerate()
            if info.get('usage_page') == USAGE_PAGE and info.get('usage') != USAGE]


def other_majors_note(usages: list) -> str:
    """Say what was found on another protocol major, and what to do about it.

    :param usages: the usages find_other_majors returned
    :return: a one-line note, empty when there were none
    """
    if not usages:
        return ''
    note = (f"{len(usages)} Host Lighting interface(s) found speaking another protocol major, "
            f"which this version does not drive.")
    if USAGE_PROTOCOL_1 in usages:
        note += (" Pre-release protocol 1.x firmware is one of them: update its firmware to an "
                 "HLP 2.0 build, or use hlp-spice2x v0.3.0 with it.")
    return note


def no_interface() -> str:
    """Build the message for a run that found no board to drive.

    :return: NO_INTERFACE, with a note on boards of another major where there are any
    """
    note = other_majors_note(find_other_majors())
    return f"{note} {NO_INTERFACE}" if note else NO_INTERFACE


def _probe(info: dict):
    """Open one interface and keep it if it answers HELLO as a board of this major.

    :param info: a hidapi enumeration dict
    :return: (decoded HELLO, the opened device), or None with the device closed
    """
    device = HostLightingDevice(info['path'])
    try:
        hello = device.hello()
    except HostLightingError:
        device.close()
        return None
    if hello['major'] != PROTOCOL_MAJOR:
        device.close()
        return None
    return hello, device


def negotiate(device: HostLightingDevice, addressing=()) -> HostLightingCapabilities:
    """Learn what the board offers, once, before any streaming starts.

    The major version is confirmed and the capability bitmaps are checked for
    what the bridge uses. The minor version gates nothing.

    :param device: an opened HostLightingDevice
    :param addressing: the addressing modes the profile stages by
    :return: the board's HostLightingCapabilities
    :raises HostLightingIncompatible: on another major version, or a board
        that does not list something the bridge needs
    :raises HostLightingError: if the board does not answer as a Host Lighting one
    """
    hello = device.hello()
    if hello['major'] != PROTOCOL_MAJOR:
        raise HostLightingIncompatible(
            f"board speaks Host Lighting v{hello['major']}.{hello['minor']}, and this bridge "
            f"speaks v{PROTOCOL_MAJOR}.x only")
    missing = lacking(hello, addressing)
    if missing:
        raise HostLightingIncompatible(
            f"this board's firmware (Host Lighting v{hello['major']}.{hello['minor']}) does not "
            f"offer what this bridge and profile need: {', '.join(missing)}")
    caps = HostLightingCapabilities(hello)
    caps.refresh(device)
    return caps


def open_device(board_id_prefix: str = '') -> HostLightingDevice:
    """Open a board's Host Lighting interface, disambiguating by board ID if needed.

    A board is bound by the factory-unique ID in its HELLO reply, never by
    device path, which changes with the USB port.

    :param board_id_prefix: optional hex prefix of the HELLO board ID
    :return: an opened HostLightingDevice
    """
    infos = find_devices()
    if not infos:
        raise HostLightingError(no_interface())
    candidates = []
    for info in infos:
        found = _probe(info)
        if found is None:
            continue
        hello, device = found
        if hello['board_id'].startswith(board_id_prefix.upper()):
            candidates.append((hello['board_id'], device))
        else:
            device.close()
    if not candidates:
        raise HostLightingError(f"no board matches ID prefix '{board_id_prefix}'")
    if len(candidates) > 1:
        boards = []
        for board_id, device in candidates:
            try:
                boards.append(f"{board_id} ({device.read_identity()[0]})")
            except HostLightingError:
                boards.append(board_id)
            device.close()
        raise HostLightingError(f"multiple boards found: {', '.join(boards)}. "
                                f"Select one with --board-id")
    return candidates[0][1]


def find_board(board_id: str):
    """Open the board with the given ID, if it is on the bus.

    :param board_id: the whole board ID from the board's HELLO reply
    :return: an opened HostLightingDevice, or None while the board is absent
    """
    for info in find_devices():
        try:
            found = _probe(info)
        except OSError:
            continue    # the interface left between the enumeration and the open
        if found is None:
            continue
        hello, device = found
        if hello['board_id'] == board_id:
            return device
        device.close()
    return None


def describe_boards() -> list:
    """Identify every attached board from HELLO and page 0, taking none of them over.

    An interface that does not answer as a Host Lighting one of this major is
    left out, as open_device leaves it out. A board whose page 0 cannot be read
    is listed with '?' for its label and firmware.

    :return: one dict per board, with board_id, label, firmware, and version as (major, minor)
    """
    boards = []
    for info in find_devices():
        found = _probe(info)
        if found is None:
            continue
        hello, device = found
        label, firmware = '?', '?'
        try:
            if PAGE_IDENTITY in hello['pages']:
                label, firmware = device.read_identity()
        except HostLightingError:
            pass
        finally:
            device.close()
        boards.append({'board_id': hello['board_id'], 'label': label, 'firmware': firmware,
                       'version': (hello['major'], hello['minor'])})
    return boards
