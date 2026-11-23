# hlp-spice2x (spice2x to GP2040-CE Host Lighting bridge)
# Filename: profile.py
# Folder: hlp_spice2x
# https://github.com/djGLiTCH/hlp-spice2x
# SPDX-FileCopyrightText: (C) 2026 Jacob Simpson (DJ GLiTCH)
# SPDX-License-Identifier: GPL-3.0-or-later
# See LICENSES/GPL-3.0-or-later.txt at the repository root.

"""Profiles map a game's light names onto a board's controls.

Light names differ per game, so the mapping lives in a JSON file:

    {"lights": {"P1 Button 1": {"button": "B1", "colour": "FF0000"},
                "Neon Left":   {"range": [16, 30], "colour": "FF00FF"}}}

A `button` entry names a control, and the firmware resolves it to the LEDs
that board has for it, so the profile works on any layout. Where a control
has several lights, all of them are lit. An `index` picks one of them,
counting as the board's Lights page lists them, and is tied to that board's
configuration. A `range` entry addresses raw LED indexes and is tied to the
board it was written for.

A control is a short name the bridge knows, such as B1, or any name the
board's page 3 gives an action or kind of light, such as BUTTON_PRESS_TURBO.
Loading keeps a short name at the value proto/enums.proto gives it, and any
other name as it is. resolve() looks each one up on the board's page 3, so
the value staged is the one the board's own firmware gives it.

Which light an `index` names is known only from the board, so it is resolved
at the handshake and refused there if the board cannot honour it.

A `colour` tints a light the game drives as an intensity. Some games drive
one fixture as three lights, its red, green and blue. A `channel` entry says
which of these a light is, and a fixture entry maps all of them at once:

    {"lights": {"Woofer R": {"button": "E1", "channel": "R"},
                "Top Panel": {"button": "S2", "channels": {"R": "Top Panel R",
                                                           "G": "Top Panel G",
                                                           "B": "Top Panel B"}}}}

Loading turns each channel into its primary colour, and a fixture into one
entry per channel keyed by the game's light name. The channels of a target
add, so the fixture shows the colour the game asks for.
"""
import json
import os

from . import hlp


HEX_DIGITS = frozenset('0123456789abcdefABCDEF')
# the highest LED index a range can name on the wire
MAX_LED_INDEX = 0xFFFF
# the longest name page 3 gives an action or kind of light
NAME_BYTES = 40
# Each channel's colour. A pure grey reaches a white chain's white emitter
# through the firmware, and an RGB chain shows it as white, so W is FFFFFF.
CHANNEL_COLOURS = {'R': (0xFF, 0, 0), 'G': (0, 0xFF, 0), 'B': (0, 0, 0xFF), 'W': (0xFF, 0xFF, 0xFF)}
# the characters that may part a channel letter from the rest of a light's name
CHANNEL_SEPARATORS = ' _-'


class ProfileError(ValueError):
    """A profile could not be understood."""


def parse_colour(label: str, colour) -> tuple:
    """Read a profile colour as RGB, or as RGBW where a white component is given.

    The length is checked first. int(colour, 16) accepts a '0x' prefix,
    underscores, surrounding whitespace and any number of digits, so six
    digits are told from eight only once the length is known.

    :param label: the entry as the error message names it, such as light 'Neon'
    :param colour: the colour as the profile wrote it
    :return: (r, g, b), or (r, g, b, w) for an eight-digit colour
    """
    text = colour if isinstance(colour, str) else ''
    if len(text) not in (6, 8) or not set(text) <= HEX_DIGITS:
        raise ProfileError(f"{label}: colour '{colour}' must be six hex digits as "
                           f"RRGGBB, or eight as RRGGBBWW to ask for the white channel")
    value = int(text, 16)
    if len(text) == 6:
        return ((value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF)
    return ((value >> 24) & 0xFF, (value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF)


def load(path: str) -> tuple:
    """Load a profile and turn its control names into protocol targets.

    :param path: path to the profile JSON
    :return: (profile, warnings), where profile maps a light name to
        (target, colour) with target being ('button', control), ('light',
        control, index) or ('range', start, count), control the (addressing,
        address) a short name stands for or (hlp.FIRMWARE, name) for any other
        name, and colour (r, g, b) or (r, g, b, w), and warnings is a list of
        non-fatal complaints about entries that were skipped. A fixture entry
        appears as one light per channel.
    """
    # JSON keeps the last of two equal keys in one object and drops the first
    # without a word. Each such key is warned about, and the last is still used.
    repeated = []

    def keep_last(pairs):
        """Build an object as json does, noting each key it gives more than once."""
        built = {}
        for key, value in pairs:
            if key in built and key not in repeated:
                repeated.append(key)
            built[key] = value
        return built

    with open(path, encoding='utf-8') as handle:
        profile = json.load(handle, object_pairs_hook=keep_last)

    resolved = {}
    warnings = [f"'{key}' is given twice in one object of the profile, and only the last is used"
                for key in repeated]
    for name, entry in (profile.get('lights') or {}).items():
        if not isinstance(entry, dict):
            raise ProfileError(f"light '{name}': expected an object, got {type(entry).__name__}")
        if 'channels' in entry:
            label = f"fixture '{name}'"
            members = fixture_members(label, entry)
            target = parse_target(label, entry, warnings)
            mapped = [(light, CHANNEL_COLOURS[letter]) for letter, light in members.items()]
        else:
            label = f"light '{name}'"
            target = parse_target(label, entry, warnings)
            # A channel is read even on an unmapped light, as a fixture is. A
            # colour is read only when mapped, as 1.0 did.
            mapped = [(name, entry_colour(label, entry))] if target or 'channel' in entry else []
        if target is None:
            continue
        for light, rgb in mapped:
            # A fixture's light may also be given an entry of its own, or be named
            # by a second fixture, and only one of them can say how it is shown.
            if light in resolved:
                raise ProfileError(f"light '{light}' is mapped by two entries, the second being "
                                   f"{label}. Map each light once")
            resolved[light] = (target, rgb)

    if not resolved:
        raise ProfileError(f"{path} has no mapped lights. Fill in the 'button' fields first")
    return resolved, warnings


def parse_target(label: str, entry: dict, warnings: list):
    """Read where an entry is shown, from its button, index or range.

    :param label: the entry as error messages name it, such as light 'Neon'
    :param entry: the entry as the profile wrote it
    :param warnings: list to add a non-fatal complaint to
    :return: the target, or None for an entry that maps nothing
    """
    # Index 0 is a real light and a falsy one, so presence is the test. An
    # index with no control to index into is refused.
    if 'index' in entry and not entry.get('button'):
        raise ProfileError(f"{label}: 'index' needs a 'button' to index into")
    # A range key with a falsy value ([], 0 or null) is refused here, since
    # the skeleton skip below would otherwise pass it over.
    if 'range' in entry and not entry.get('range'):
        raise ProfileError(f"{label}: 'range' must be [start, count]")
    if 'button' not in entry and 'range' not in entry:
        # neither key at all is more likely a typo than a deliberate skip
        warnings.append(f"{label} has no 'button' or 'range' key, ignoring")
        return None
    if not entry.get('button') and not entry.get('range'):
        return None  # an unmapped skeleton line

    if entry.get('range'):
        try:
            start, count = (int(part) for part in entry['range'])
        except (TypeError, ValueError):
            raise ProfileError(f"{label}: 'range' must be [start, count]") from None
        # An LED index travels as 16 bits. The range is held to the board's
        # addressable limit at the handshake.
        if start < 0 or count < 1 or start + count > MAX_LED_INDEX + 1:
            raise ProfileError(f"{label}: range [{start}, {count}] is outside "
                               f"LED indexes 0-{MAX_LED_INDEX}")
        return ('range', start, count)

    control = str(entry['button']).strip().upper()
    if not control or len(control) > NAME_BYTES or not control.isascii() or not control.isprintable():
        raise ProfileError(f"{label}: control '{entry['button']}' is not a name a board can "
                           f"give, which is printable ASCII of up to {NAME_BYTES} bytes")
    target = hlp.CONTROL_TARGETS.get(control, (hlp.FIRMWARE, control))
    if 'index' not in entry:
        return ('button', target)
    try:
        index = int(entry['index'])
    except (TypeError, ValueError):
        raise ProfileError(f"{label}: 'index' must be a whole number, "
                           f"got {entry['index']!r}") from None
    if index < 0:
        raise ProfileError(f"{label}: 'index' cannot be negative")
    return ('light', target, index)


def channel_letter(label: str, channel) -> str:
    """Read a channel as one of R, G, B or W.

    :param label: the entry as error messages name it
    :param channel: the channel as the profile wrote it
    :return: the channel's letter, in upper case
    """
    letter = channel.strip().upper() if isinstance(channel, str) else ''
    if letter not in CHANNEL_COLOURS:
        raise ProfileError(f"{label}: channel '{channel}' must be R, G, B or W")
    return letter


def entry_colour(label: str, entry: dict) -> tuple:
    """Read a light entry's colour, from its channel or its colour.

    :param label: the entry as error messages name it
    :param entry: the entry as the profile wrote it
    :return: (r, g, b), or (r, g, b, w) for an eight-digit colour
    """
    if 'channel' in entry:
        if 'colour' in entry or 'color' in entry:
            raise ProfileError(f"{label}: give a 'channel' or a 'colour', not both")
        return CHANNEL_COLOURS[channel_letter(label, entry['channel'])]
    return parse_colour(label, entry.get('colour', entry.get('color', 'FFFFFF')))


def fixture_members(label: str, entry: dict) -> dict:
    """Read the lights a fixture entry names, one per channel.

    :param label: the fixture as error messages name it
    :param entry: the entry as the profile wrote it
    :return: mapping of channel letter to the game's light name
    """
    for key in ('colour', 'color', 'channel'):
        if key in entry:
            raise ProfileError(f"{label}: a fixture takes its colours from 'channels', "
                               f"so '{key}' is not used")
    channels = entry['channels']
    if not isinstance(channels, dict) or not channels:
        raise ProfileError(f"{label}: 'channels' must map R, G, B or W to a light's name")
    members = {}
    for channel, light in channels.items():
        letter = channel_letter(label, channel)
        if letter in members:
            raise ProfileError(f"{label}: channel {letter} is given twice")
        if not isinstance(light, str) or not light.strip():
            raise ProfileError(f"{label}: channel {letter} must name a light")
        if light in members.values():
            raise ProfileError(f"{label}: light '{light}' is named by two channels")
        members[letter] = light
    return members


def resolve(profile: dict, names: dict) -> tuple:
    """Resolve each control a profile names to the value a board's firmware gives it.

    :param profile: a loaded profile
    :param names: the board's page 3 names, as read_names() returns them
    :return: (the profile with each control at the board's value and the entries
        whose control the firmware has no name for left out, the controls left out)
    """
    index = hlp.name_index(names)
    resolved = {}
    missing = []
    for name, (target, colour) in profile.items():
        if target[0] == 'range':
            resolved[name] = (target, colour)
            continue
        control = hlp.resolve_control(target[1], index)
        if control is None:
            if target[1] not in missing:
                missing.append(target[1])
            continue
        resolved[name] = ((target[0], control) + target[2:], colour)
    return resolved, missing


def find_fixtures(names) -> dict:
    """Group the lights that are the red, green and blue of one fixture.

    A light is a channel when its name ends in R, G, B or W, after a space,
    an underscore, a hyphen or nothing. Many names end in R for right, such as
    FX-R, so a group needs its R, G and B all present. A W joins a group that
    has them. The fixture is named for what comes before the channel letter,
    or that with RGB added where a light already has that name.

    :param names: iterable of light names as reported by the game
    :return: mapping of fixture name to {channel letter: light name}, in R, G,
        B, W order
    """
    names = set(names)
    fixtures = {}
    for stem in sorted({name[:-1] for name in names if len(name) > 1 and name[-1] in CHANNEL_COLOURS}):
        members = {letter: stem + letter for letter in CHANNEL_COLOURS if stem + letter in names}
        if not {'R', 'G', 'B'} <= set(members):
            continue
        # The stem keeps its separator, so stripping it gives the name and
        # adding RGB to it gives the fallback, as "Woofer RGB" or "WooferRGB".
        name = stem.rstrip(CHANNEL_SEPARATORS)
        if not name or name in names or name in fixtures:
            name = stem + 'RGB'
        if name in names or name in fixtures:
            continue  # nothing free to call it, so its lights stay as they are
        fixtures[name] = members
    return fixtures


def write_skeleton(path: str, names) -> dict:
    """Write a profile listing every light, ready for control names to be filled in.

    The channels of a fixture are written as one fixture entry, so one control
    name maps the whole fixture. Every other light is written with a colour.

    :param path: file to create, never overwritten
    :param names: iterable of light names as reported by the game
    :return: the fixtures written, as find_fixtures() gives them
    """
    if os.path.exists(path):
        raise ProfileError(f"{path} already exists. Remove it or choose another name")
    names = list(names)
    fixtures = find_fixtures(names)
    grouped = {light for members in fixtures.values() for light in members.values()}
    lights = {name: {'button': '', 'colour': 'FFFFFF'} for name in names if name not in grouped}
    lights.update({name: {'button': '', 'channels': members} for name, members in fixtures.items()})
    skeleton = {'lights': dict(sorted(lights.items()))}
    with open(path, 'w', encoding='utf-8') as handle:
        json.dump(skeleton, handle, indent=2)
        handle.write('\n')
    return fixtures


def resolve_frame(states: dict, profile: dict) -> dict:
    """Scale each mapped light's colour by its state, combining duplicates.

    Two game lights may land on one control. Their contributions add and clamp.

    A mapped light the game reports as off stages black. In overlay takeover
    those controls go dark, and only controls absent from the profile show
    the on-board animation.

    :param states: mapping of light name to state, from the game
    :param profile: a loaded profile
    :return: mapping of target to (r, g, b), or (r, g, b, w) where an entry
        asked for the white channel
    """
    frame = {}
    for name, state in states.items():
        mapping = profile.get(name)
        if mapping is None:
            continue
        target, colour = mapping
        level = min(max(state, 0.0), 1.0)
        scaled = tuple(int(component * level) for component in colour)
        if target in frame:
            # two entries on one target differ in width if only one of them
            # asked for white, so the narrower is padded
            existing = frame[target]
            width = max(len(existing), len(scaled))
            existing += (0,) * (width - len(existing))
            scaled += (0,) * (width - len(scaled))
            frame[target] = tuple(min(255, a + c) for a, c in zip(existing, scaled))
        else:
            frame[target] = scaled
    return frame


def format_frame(frame: dict) -> str:
    """Render a resolved frame as text, for dry runs.

    :param frame: a resolved frame
    :return: a one-line human-readable summary
    """
    if not frame:
        return "(all lights off)"
    parts = []
    for target, rgb in sorted(frame.items(), key=lambda item: str(item[0])):
        if target[0] == 'button':
            label = hlp.control_name(target[1])
        elif target[0] == 'light':
            # no board, so no ordinal, and the entry is shown as the profile wrote it
            label = f"{hlp.control_name(target[1])}[{target[2]}]"
        else:
            label = f"range@{target[1]}+{target[2]}"
        parts.append(f"{label}=#{''.join(f'{component:02X}' for component in rgb)}")
    return ' '.join(parts)
