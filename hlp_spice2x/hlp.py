# hlp-spice2x (spice2x to GP2040-CE Host Lighting bridge)
# Filename: hlp.py
# Folder: hlp_spice2x
# https://github.com/djGLiTCH/hlp-spice2x
# SPDX-FileCopyrightText: (C) 2026 Jacob Simpson (DJ GLiTCH)
# SPDX-License-Identifier: GPL-3.0-or-later
# See LICENSES/GPL-3.0-or-later.txt at the repository root.

"""What the bridge needs of a GP2040-CE board's Host Lighting interface.

The Host Lighting add-on exposes a vendor HID interface (usage page 0xFF47,
usage 0x0002) carrying the Host Lighting Protocol (HLP) 2.0. Its fixed
64-byte reports let host software drive the board's RGB LEDs live and read
the board's lights and controls from its own configuration.

The protocol itself is gp2040ce-hostlighting's. This module holds the
bridge's own part: the names a profile uses for controls and kinds of light,
what the bridge requires of a board, the capability view the bridge asks its
questions of, and the bridge's messages for finding a board.

The bridge reads six pages:

* page 0 (identity) gives the board label and firmware version, for display.
* page 1 (state) gives whether a host frame is shown, the holder and the
  claim generation, and the CONFIGURE settings in force. A STATE_CHANGED
  event carries the same record.
* page 3 (names) gives the firmware's name for each action and light kind.
  The bridge resolves each control a profile names through it, so a control
  is staged by the value the board's own firmware gives it.
* page 8 (summary) gives the LED extent and addressable limit, the render
  rate, the colour format, the white channel, and a change counter for each
  configuration page.
* page 10 (controls) lists every control with an action, lit or not. It is
  the only page that can say a control does not exist.
* page 12 (lights) lists every light, with its LEDs, kind and owning action.
"""
from gp2040ce_hostlighting import protocol as library
from gp2040ce_hostlighting.cache import ConfigurationCache
from gp2040ce_hostlighting.protocol import (ADDRESSING_ACTION, ADDRESSING_KIND, CMD_CLAIM, CMD_CLEAR, CMD_CONFIGURE,
                                            CMD_GET_PAGE, CMD_HELLO, CMD_STAGE, CMD_SUBSCRIBE, EVENT_STATE_CHANGED,
                                            PAGE_CONTROLS, PAGE_LIGHTS, PAGE_NAMES, PAGE_STATE, PAGE_SUMMARY, PIXEL_RGB,
                                            PIXEL_RGBW, USAGE_PROTOCOL_1, Hello, HostLightingBoardSelectionError,
                                            HostLightingDevice, HostLightingError, HostLightingIncompatibleError,
                                            HostLightingMagicError, Requirements, find_other_majors, list_boards,
                                            missing_capabilities, require_capabilities)

# The message for a run that finds no interface. The link names the firmware builds this release was built against.
NO_INTERFACE = ("no Host Lighting interface found. Connect a board with the add-on enabled. "
                "The add-on is not in an official GP2040-CE release yet. Test builds are at "
                "https://github.com/djGLiTCH/GP2040-CE/releases/tag/HLP_v2.0-rc12")
# the message for an interface whose HELLO reply does not carry the protocol's magic
NOT_HOST_LIGHTING = "handshake failed: this interface did not answer as a Host Lighting one"

# The groups read here, as GET_PAGE field masks. A mask always names its groups, since mask 0
# also returns the groups a later board adds.
STATE_GROUPS = 0x07             # State, Holder, Settings
SUMMARY_GROUPS = 0x03           # Summary, Changes
CONTROL_GROUPS = 0x01           # key, action, flags
LIGHT_GROUPS = 0x07             # LEDs, kind and flags, owner pin and action
IDENTITY_LABEL = 0              # page 0 group bits
IDENTITY_FIRMWARE = 3

# Summary colour formats, named for the startup line. Whether a chain has a white emitter is the
# Summary's white channel flag.
LED_FORMAT_NAMES = {0: 'GRB', 1: 'RGB', 2: 'GRBW', 3: 'RGBW'}

# The short control names a profile may use, as GP2040-CE's GpioAction values (proto/enums.proto).
# STAGE by action reaches every light carrying it, so a control wired to two buttons lights both.
# A board's page 3 gives the value each one has on its firmware, and these are used without a board.
ACTION_NAMES = ['Up', 'Down', 'Left', 'Right', 'B1', 'B2', 'B3', 'B4', 'L1', 'R1', 'L2', 'R2',
                'S1', 'S2', 'A1', 'A2', 'L3', 'R3']
ACTIONS = {name: action for action, name in enumerate(ACTION_NAMES, start=1)}
ACTIONS.update({'A3': 41, 'A4': 42})
ACTIONS.update({f'E{n + 1}': 43 + n for n in range(12)})
# The lights with no control behind them, as LightType values. STAGE by kind reaches every light
# of the kind.
KINDS = {'CASE': 1, 'TURBO': 2, 'PLED1': 3, 'PLED2': 4, 'PLED3': 5, 'PLED4': 6}

# every spelling a profile may use, as the (addressing, address) STAGE reaches it by, and the
# reverse for reporting
CONTROL_TARGETS = {name.upper(): (ADDRESSING_ACTION, action) for name, action in ACTIONS.items()}
CONTROL_TARGETS.update({name: (ADDRESSING_KIND, kind) for name, kind in KINDS.items()})
CONTROL_NAMES = {(ADDRESSING_ACTION, action): name for name, action in ACTIONS.items()}
CONTROL_NAMES.update({(ADDRESSING_KIND, kind): name for name, kind in KINDS.items()})
# The name proto/enums.proto gives each short name's value. A board's page 3 names its values as
# its firmware defines them, so a short name is resolved on a board through this name.
FIRMWARE_NAMES = {(ADDRESSING_ACTION, action): f'BUTTON_PRESS_{name.upper()}' for name, action in ACTIONS.items()}
FIRMWARE_NAMES.update({(ADDRESSING_KIND, 1): 'Case', (ADDRESSING_KIND, 2): 'Turbo'})
FIRMWARE_NAMES.update({(ADDRESSING_KIND, 2 + n): f'Player{n}Light' for n in range(1, 5)})
SHORT_NAMES = {name.upper(): CONTROL_NAMES[control] for control, name in FIRMWARE_NAMES.items()}
# page 3's enums, by the addressing mode that stages their values, actions first where a name is in both
NAME_ENUMS = {'GpioAction': ADDRESSING_ACTION, 'LightType': ADDRESSING_KIND}
# The first element of a control a profile names by a name the bridge has no value for, as
# ('firmware', NAME). It is resolved on a board through page 3.
FIRMWARE = 'firmware'

# What the bridge cannot run without, checked against HELLO's bitmaps. The addressing modes are
# added per profile.
REQUIRED = Requirements(commands=frozenset({CMD_HELLO, CMD_GET_PAGE, CMD_CLAIM, CMD_CONFIGURE, CMD_STAGE,
                                            CMD_CLEAR}),
                        request_flags=frozenset({0, 1, 2, 4, 5, 6}),
                        pages=frozenset({PAGE_STATE, PAGE_NAMES, PAGE_SUMMARY, PAGE_LIGHTS}),
                        pixel_formats=frozenset({PIXEL_RGB}))
# the groups the bridge reads of each configuration page it keeps
CACHE_MASKS = {PAGE_SUMMARY: SUMMARY_GROUPS, PAGE_CONTROLS: CONTROL_GROUPS, PAGE_LIGHTS: LIGHT_GROUPS}
# the configuration pages the bridge keeps beside the Summary
CACHE_PAGES = frozenset({PAGE_CONTROLS, PAGE_LIGHTS})


def control_name(control, names=None) -> str:
    """Render a control as a profile names it.

    :param control: an (addressing, address) pair, or a (FIRMWARE, name) control not yet resolved
    :param names: page 3's names, as read_names() returns them, or None without a board
    :return: the short name, else the firmware's name, else the raw address
    """
    if control[0] == FIRMWARE:
        return control[1]
    named = FIRMWARE_NAMES.get(control) if names is None else page_name(control, names)
    if named is None:
        return f"{'kind' if control[0] == ADDRESSING_KIND else 'action'} {control[1]}"
    return SHORT_NAMES.get(named.upper(), named)


def page_name(control, names):
    """Give the name a board's page 3 gives a control's value, or None where it gives none.

    :param control: an (addressing, address) pair staged by action or by kind
    :param names: page 3's names, as read_names() returns them
    """
    enum = next((enum for enum, addressing in NAME_ENUMS.items() if addressing == control[0]), None)
    return names.get(enum, {}).get(control[1])


def name_index(names) -> dict:
    """Index page 3's names for a profile to be resolved against.

    :param names: page 3's names, as read_names() returns them
    :return: {(addressing, upper-case name): value}
    """
    return {(addressing, name.upper()): value for enum, addressing in NAME_ENUMS.items()
            for value, name in names.get(enum, {}).items()}


def resolve_control(control, index):
    """Find the value a board's firmware gives a control a profile names.

    A short name is looked up among its own enum's names. Any other name is
    looked up among the actions, then the kinds of light.

    :param control: an (addressing, address) pair from CONTROL_TARGETS, or a (FIRMWARE, name) control
    :param index: name_index() of the board's page 3
    :return: the (addressing, value) the board stages it by, or None if its firmware has no such name
    """
    if control[0] == FIRMWARE:
        keys = [(addressing, control[1]) for addressing in NAME_ENUMS.values()]
    else:
        keys = [(control[0], FIRMWARE_NAMES[control].upper())]
    return next(((key[0], index[key]) for key in keys if key in index), None)


def firmware_name(control) -> str:
    """Give the name a control is looked up by on page 3."""
    return control[1] if control[0] == FIRMWARE else FIRMWARE_NAMES[control]


def requirements(addressing=()) -> Requirements:
    """Name what the bridge needs of a board, with the addressing modes a profile stages by.

    :param addressing: the addressing modes the profile stages by
    :return: the bridge's requirements
    """
    return REQUIRED.union(Requirements(addressing_modes=frozenset(addressing)))


def lacking(hello: Hello, addressing=()) -> list:
    """List what the bridge needs that a board's HELLO does not offer.

    A host gates on HELLO's capability bitmaps and never on the minor version.

    :param hello: the board's HELLO
    :param addressing: the addressing modes the profile stages by
    :return: what is missing, named for a message, or an empty list
    """
    return missing_capabilities(hello, requirements(addressing))


def label_and_firmware(device: HostLightingDevice) -> tuple:
    """Read the board label and the firmware version from page 0, '?' for one the board did not give."""
    identity = device.read_identity((1 << IDENTITY_LABEL) | (1 << IDENTITY_FIRMWARE))
    return identity.board_label or '?', identity.firmware_version or '?'


class HostLightingCapabilities:
    """What the board on the other end offers, as its HELLO and its configuration pages say.

    Call sites ask this object what is available, so a run with no board
    answers every question with no. The pages are kept by a
    ConfigurationCache, the bridge's own or a Session's.
    """

    def __init__(self, hello=None, cache=None, names=None):
        """Hold a HELLO reply and the cache of the pages read alongside it.

        :param hello: the board's HELLO, or None for no board
        :param cache: the ConfigurationCache to ask, by default one keeping the
            Summary, Controls and Lights groups the bridge reads
        :param names: page 3's names, as read_names() returns them, or None until read
        """
        self.hello = hello
        if cache is None and hello is not None:
            cache = ConfigurationCache(hello, CACHE_MASKS)
            cache.pages = CACHE_PAGES & hello.pages
        self.cache = cache
        self.names = names

    def control_name(self, control) -> str:
        """Render a control as a profile names it, by this board's page 3 once read."""
        return control_name(control, self.names)

    @classmethod
    def absent(cls):
        """Describe a run with no board, where nothing was read.

        :return: a HostLightingCapabilities describing no board at all
        """
        return cls()

    @property
    def summary(self):
        """The Summary group, or None until read."""
        return self.cache.summary if self.cache is not None else None

    @property
    def token(self):
        """The state token the pages were read under, or None until read."""
        return self.cache.token if self.cache is not None else None

    @property
    def lights(self) -> tuple:
        """The Lights page's records, empty until read."""
        return (self.cache.lights or ()) if self.cache is not None else ()

    @property
    def controls(self):
        """The Controls page's records, or None where the board has no Controls page."""
        return self.cache.controls if self.cache is not None else None

    @property
    def connected(self) -> bool:
        """Whether these capabilities came from a board."""
        return self.hello is not None

    @property
    def events(self) -> bool:
        """Whether the board offers the STATE_CHANGED event, so its state need not be polled."""
        return (self.connected and CMD_SUBSCRIBE in self.hello.commands
                and EVENT_STATE_CHANGED in self.hello.events)

    @property
    def white_channel(self) -> bool:
        """Whether the board's chain has a white emitter for a W component to reach."""
        return self.summary is not None and self.summary.white_channel

    @property
    def host_white(self) -> bool:
        """Whether a host-supplied white component can be sent and will render.

        An RGBW pixel may be sent to any chain, but its W renders only on a
        chain with a white emitter, and the board has to list the format.
        """
        return self.white_channel and self.connected and PIXEL_RGBW in self.hello.pixel_formats

    @property
    def render_hz(self):
        """The board's render rate in Hz, or None if it did not say."""
        return (self.summary.render_hz or None) if self.summary is not None else None

    @property
    def led_extent(self) -> int:
        """The highest LED index in use plus one, the number a frame is sized from."""
        return self.summary.led_extent if self.summary is not None else 0

    @property
    def led_limit(self) -> int:
        """The addressable LED limit. An LED run reaching past it stages nothing."""
        return self.summary.led_limit if self.summary is not None else 0

    def lights_of(self, control) -> list:
        """List the lights STAGE reaches through a control, in ordinal order.

        A light whose LEDs all lie at or past the addressable limit is on the
        page but skipped by STAGE, so it is left out here too.

        :param control: the (addressing, value) a control is staged by, as resolved on this board
        :return: the page 12 records STAGE by that action or kind colours
        """
        reached = self.cache.lights_reached(*control) if self.cache is not None else None
        if not reached:
            return []
        return [record for record in self.lights if record.ordinal in reached]

    def lacks_control(self, control) -> bool:
        """Say whether the Controls page shows the board has no such control.

        False where page 10 cannot say, which is when the board has no such
        page or the target is a kind of light.

        :param control: the (addressing, value) a control is staged by, as resolved on this board
        :return: True only if the Controls page leaves the control's action out
        """
        if self.cache is None or control[0] != ADDRESSING_ACTION:
            return False
        return self.cache.controls_with(action=control[1]) == ()

    def refresh(self, device, attempts: int = 3) -> bool:
        """Read the Summary, and the pages whose change counter moved since the last read.

        :param device: an opened HostLightingDevice
        :param attempts: how many times to start over before giving up
        :return: True if the Lights or the Controls page was read again, or a
            Summary field changed
        :raises HostLightingError: if the configuration keeps changing under the reads
        """
        return bool(self.cache.refresh(device, attempts) & {PAGE_SUMMARY, PAGE_CONTROLS, PAGE_LIGHTS})

    def summary_line(self) -> str:
        """Name the protocol version and what the board reported, for the startup line.

        :return: a one-line summary of the board
        """
        if not self.connected:
            return "no board: running without one, so nothing was read"
        chain = LED_FORMAT_NAMES.get(self.summary.colour_format if self.summary else None, 'unknown')
        offers = [f"{len(self.lights)} lights on {self.led_extent} LEDs",
                  f"renders at {self.render_hz} Hz" if self.render_hz else 'render rate unstated',
                  f"{chain} chain (white channel: {'yes' if self.white_channel else 'no'})",
                  'state changes by event' if self.events else 'state changes by poll']
        return f"board speaks HLP v{self.hello.major}.{self.hello.minor}: {', '.join(offers)}"


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


def negotiate(device: HostLightingDevice, addressing=()) -> HostLightingCapabilities:
    """Learn what the board offers, once, before any streaming starts.

    The major version is confirmed and the capability bitmaps are checked for
    what the bridge uses. The minor version gates nothing.

    :param device: an opened HostLightingDevice
    :param addressing: the addressing modes the profile stages by
    :return: the board's HostLightingCapabilities
    :raises HostLightingIncompatibleError: on another major version, a board that
        does not list something the bridge needs, or an interface that is not a
        Host Lighting one
    """
    try:
        hello = device.hello()
    except HostLightingMagicError:
        raise HostLightingIncompatibleError(NOT_HOST_LIGHTING) from None
    require_capabilities(hello, requirements(addressing))
    caps = HostLightingCapabilities(hello, names=device.read_names())
    caps.refresh(device)
    return caps


def open_device(board_id_prefix: str = '') -> HostLightingDevice:
    """Open a board's Host Lighting interface, in the bridge's own words when none can be chosen.

    :param board_id_prefix: optional hex prefix of the HELLO board ID
    :return: an opened HostLightingDevice
    :raises HostLightingError: when no board, or more than one, matches
    """
    try:
        return library.open_device(board_id_prefix)
    except HostLightingBoardSelectionError as error:
        if not error.boards:
            if error.interfaces:
                raise HostLightingError(str(error)) from None   # found, but none answered as a board
            note = other_majors_note(list(error.other_majors))
            raise HostLightingError(f"{note} {NO_INTERFACE}" if note else NO_INTERFACE) from None
        if not error.matched:
            raise HostLightingError(f"no board matches ID prefix '{board_id_prefix}'") from None
        named = ', '.join(f"{board.board_id} ({board.board_label or '?'})" for board in error.matched)
        raise HostLightingError(f"multiple boards found: {named}. Select one with --board-id") from None


def describe_boards() -> list:
    """Identify every attached board from HELLO and page 0, taking none of them over.

    :return: one dict per board, with board_id, label, firmware, and version as (major, minor)
    """
    return [{'board_id': board.board_id, 'label': board.board_label or '?',
             'firmware': board.firmware_version or '?', 'version': (board.major, board.minor)}
            for board in list_boards()]
