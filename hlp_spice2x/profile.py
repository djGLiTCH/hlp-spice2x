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
"""
import json
import os

from . import hlp


HEX_DIGITS = frozenset('0123456789abcdefABCDEF')
# the highest LED index a range can name on the wire
MAX_LED_INDEX = 0xFFFF
# the longest name page 3 gives an action or kind of light
NAME_BYTES = 40


class ProfileError(ValueError):
    """A profile could not be understood."""


def parse_colour(name: str, colour) -> tuple:
    """Read a profile colour as RGB, or as RGBW where a white component is given.

    The length is checked first. int(colour, 16) accepts a '0x' prefix,
    underscores, surrounding whitespace and any number of digits, so six
    digits are told from eight only once the length is known.

    :param name: the light's name, for the error message
    :param colour: the colour as the profile wrote it
    :return: (r, g, b), or (r, g, b, w) for an eight-digit colour
    """
    text = colour if isinstance(colour, str) else ''
    if len(text) not in (6, 8) or not set(text) <= HEX_DIGITS:
        raise ProfileError(f"light '{name}': colour '{colour}' must be six hex digits as "
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
        non-fatal complaints about entries that were skipped
    """
    with open(path, encoding='utf-8') as handle:
        profile = json.load(handle)

    resolved = {}
    warnings = []
    for name, entry in (profile.get('lights') or {}).items():
        if not isinstance(entry, dict):
            raise ProfileError(f"light '{name}': expected an object, got {type(entry).__name__}")
        # Index 0 is a real light and a falsy one, so presence is the test. An
        # index with no control to index into is refused.
        if 'index' in entry and not entry.get('button'):
            raise ProfileError(f"light '{name}': 'index' needs a 'button' to index into")
        # A range key with a falsy value ([], 0 or null) is refused here, since
        # the skeleton skip below would otherwise pass it over.
        if 'range' in entry and not entry.get('range'):
            raise ProfileError(f"light '{name}': 'range' must be [start, count]")
        if 'button' not in entry and 'range' not in entry:
            # neither key at all is more likely a typo than a deliberate skip
            warnings.append(f"light '{name}' has no 'button' or 'range' key, ignoring")
            continue
        if not entry.get('button') and not entry.get('range'):
            continue  # an unmapped skeleton line

        rgb = parse_colour(name, entry.get('colour', entry.get('color', 'FFFFFF')))

        if entry.get('range'):
            try:
                start, count = (int(part) for part in entry['range'])
            except (TypeError, ValueError):
                raise ProfileError(f"light '{name}': 'range' must be [start, count]") from None
            # An LED index travels as 16 bits. The range is held to the board's
            # addressable limit at the handshake.
            if start < 0 or count < 1 or start + count > MAX_LED_INDEX + 1:
                raise ProfileError(f"light '{name}': range [{start}, {count}] is outside "
                                   f"LED indexes 0-{MAX_LED_INDEX}")
            resolved[name] = (('range', start, count), rgb)
        else:
            control = str(entry['button']).strip().upper()
            if not control or len(control) > NAME_BYTES or not control.isascii() or not control.isprintable():
                raise ProfileError(f"light '{name}': control '{entry['button']}' is not a name a board can "
                                   f"give, which is printable ASCII of up to {NAME_BYTES} bytes")
            target = hlp.CONTROL_TARGETS.get(control, (hlp.FIRMWARE, control))
            if 'index' in entry:
                try:
                    index = int(entry['index'])
                except (TypeError, ValueError):
                    raise ProfileError(f"light '{name}': 'index' must be a whole number, "
                                       f"got {entry['index']!r}") from None
                if index < 0:
                    raise ProfileError(f"light '{name}': 'index' cannot be negative")
                resolved[name] = (('light', target, index), rgb)
            else:
                resolved[name] = (('button', target), rgb)

    if not resolved:
        raise ProfileError(f"{path} has no mapped lights. Fill in the 'button' fields first")
    return resolved, warnings


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


def write_skeleton(path: str, names) -> None:
    """Write a profile listing every light, ready for control names to be filled in.

    :param path: file to create, never overwritten
    :param names: iterable of light names as reported by the game
    """
    if os.path.exists(path):
        raise ProfileError(f"{path} already exists. Remove it or choose another name")
    skeleton = {'lights': {name: {'button': '', 'colour': 'FFFFFF'} for name in sorted(names)}}
    with open(path, 'w', encoding='utf-8') as handle:
        json.dump(skeleton, handle, indent=2)
        handle.write('\n')


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
