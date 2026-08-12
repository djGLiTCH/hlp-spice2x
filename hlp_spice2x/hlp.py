"""Talk to a GP2040-CE board's Host Lighting interface over HID.

The Host Lighting add-on exposes a vendor HID interface (usage page 0xFF47,
usage 0x4C) carrying the Host Lighting Protocol: fixed 64-byte reports that
let host software drive the board's RGB LEDs live and read the board's LED
layout from its own configuration.

Every request is `[0]=command, [1]=sequence, [2..]=payload`; the reply echoes
the command with bit 7 set: `[0]=command|0x80, [1]=sequence, [2]=status,
[3..]=payload`.

The board describes itself through GET_CAPS pages. The protocol defines five;
this module reads the two the bridge needs, and the rest are listed so the
surface is clear:

* page 0 (identity): factory-unique board ID, board label, firmware version.
  Read here, and the right thing to bind a board to, since device paths change
  with USB ports.
* page 1 (runtime state): input mode, profile number, brightness step,
  host-assigned player, LED-map fingerprint, current animation index. Read
  here for the fingerprint. Note the brightness field is a step index into the
  board's configured steps, not a 0-255 level, and the step count is not
  reported.
* page 2 (LED map): LEDs per button, colour format, layout, total LED count,
  brightness maximum, and the per-button, player, turbo and case LED ranges.
  Not read here: the bridge names controls and lets the firmware resolve them,
  which is what makes one profile work across board layouts.
* page 3 (animations): current on-board animation index, and how many the
  board offers. Not read here.
* page 4 (positions): per-light grid positions, where the render pipeline
  provides them. Not read here.

This is a trimmed copy of the Host Lighting helpers from gp2040ce-binary-tools,
vendored so the bridge needs nothing but hidapi. It keeps only what driving a
board requires; the full version also offers ping, caps, fill and reboot
commands, and decodes every page above. If those tools land upstream, this
module can be replaced by an import of gp2040ce_bintools.hostlighting.

See docs/host-lighting.md in the GP2040-CE repository for the protocol
reference.

SPDX-FileCopyrightText: © 2026 Jacob Simpson
SPDX-License-Identifier: GPL-3.0-or-later
"""
import time

# discovery: match the interface by these, never by VID:PID (which varies by input mode)
USAGE_PAGE = 0xFF47
USAGE = 0x4C
REPORT_SIZE = 64
REQUIRED_VERSION = (1, 0)

# command IDs, grouped by function range
CMD_PING = 0x01                 # session and discovery, 0x01-0x0F
CMD_GET_CAPS = 0x02
CMD_SET_MODE = 0x03
CMD_SET_BUTTONS = 0x10          # frame staging, 0x10-0x2F
CMD_SET_RANGE = 0x11
CMD_CLEAR = 0x14
CMD_COMMIT = 0x30               # frame lifecycle, 0x30-0x3F
CMD_RELEASE = 0x31

RESPONSE_FLAG = 0x80
STATUS_NAMES = {0: 'OK', 1: 'UNSUPPORTED', 2: 'INVALID_ARG'}

# GET_CAPS pages, as the request's payload byte [2]. Pages 2 to 4 are part of
# the protocol but unread here; see the module docstring for what they hold.
CAPS_PAGE_IDENTITY = 0
CAPS_PAGE_STATE = 1
CAPS_PAGE_LED_MAP = 2
CAPS_PAGE_ANIMATIONS = 3
CAPS_PAGE_POSITIONS = 4

# the firmware's LED buffer size, which bounds raw ranges
MAX_LEDS = 100
# per-report payload limits
SET_BUTTONS_MAX = 15            # [buttonId,R,G,B] entries
SET_RANGE_MAX = 20              # RGB pixels

# button IDs 0-17 as indexed in the page 2 LED map, plus the specials
BUTTON_NAMES = ['Up', 'Down', 'Left', 'Right', 'B1', 'B2', 'B3', 'B4', 'L1', 'R1', 'L2', 'R2',
                'S1', 'S2', 'L3', 'R3', 'A1', 'A2']
SPECIAL_TARGETS = {'PLED1': 24, 'PLED2': 25, 'PLED3': 26, 'PLED4': 27, 'TURBO': 28, 'CASE': 29}


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
    if button_id < len(BUTTON_NAMES):
        return BUTTON_NAMES[button_id]
    for name, value in SPECIAL_TARGETS.items():
        if value == button_id:
            return name
    return str(button_id)


class HostLightingError(RuntimeError):
    """Errors talking to a Host Lighting interface."""


class HostLightingRejected(HostLightingError):
    """The board answered a command with a non-OK status."""


class HostLightingTimeout(HostLightingError):
    """No reply arrived in time.

    Replies are best-effort while streaming, so a caller may reasonably carry
    on after one of these.
    """


class HostLightingDisconnected(HostLightingError):
    """The underlying HID device failed.

    Usually the board was unplugged or rebooted. Distinct from a timeout
    because carrying on is pointless.
    """


class HostLightingDevice:
    """One GP2040-CE board's Host Lighting interface."""

    def __init__(self, path: bytes):
        """Open the HID device at the given hidapi path.

        :param path: platform-specific hidapi device path from enumeration
        """
        hid = _import_hid()
        self.device = hid.device()
        self.device.open_path(path)
        self.device.set_nonblocking(True)
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

    def get_caps_page(self, page: int) -> bytes:
        """Read one GET_CAPS page.

        :param page: which capability page to read (CAPS_PAGE_* constant)
        :return: the reply report ([3..] is the page's payload)
        """
        return self.request_ok(CMD_GET_CAPS, bytes([page]))


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


def read_fingerprint(device: HostLightingDevice) -> int:
    """Read the LED-map fingerprint from GET_CAPS page 1, which changes if the map does.

    Page 1 reply layout: [3] input mode, [4] profile, [5] brightness step,
    [6] host-assigned player, [7..10] LED-map fingerprint (little endian),
    [11] current animation index. Byte [5] is a step index into the board's
    brightness steps, not a 0-255 level.

    :param device: an opened HostLightingDevice
    :return: the LED-map fingerprint
    """
    reply = device.get_caps_page(CAPS_PAGE_STATE)
    return int.from_bytes(reply[7:11], 'little')


def open_device(board_id_prefix: str = '') -> HostLightingDevice:
    """Open a Host Lighting device, disambiguating by board ID if needed.

    :param board_id_prefix: optional hex prefix of the page 0 factory board ID
    :return: an opened HostLightingDevice
    """
    infos = find_devices()
    if not infos:
        raise HostLightingError("no Host Lighting interface found - is a board connected "
                                "with the add-on enabled?")
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
