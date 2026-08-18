"""Profiles map a game's light names onto a board's controls.

Light names differ per game, so the mapping lives in a JSON file rather than
in code:

    {"lights": {"P1 Button 1": {"button": "B1", "colour": "FF0000"},
                "Neon Left":   {"range": [16, 30], "colour": "FF00FF"}}}

A `button` entry names a control and lets the firmware resolve it to whatever
LEDs that board actually has wired, so the profile works on any layout. Where
the board says a control owns several lights, all of them are lit. Adding an
`index` picks out one of them instead, counting as the board's own light table
lists them, which stays portable in the control it names but not in the light it
picks. A `range` entry addresses raw LED indexes instead, which is tied to the
board it was written for.

Targets are resolved as far as they can be without a board. Which ordinal an
`index` names is a question only the board can answer, so it is left until the
handshake and refused there if the firmware or the board cannot honour it.

SPDX-FileCopyrightText: (C) 2026 Jacob Simpson
SPDX-License-Identifier: GPL-3.0-or-later
"""
import json
import os

from . import hlp


HEX_DIGITS = frozenset('0123456789abcdefABCDEF')


class ProfileError(ValueError):
    """A profile could not be understood."""


def parse_colour(name: str, colour) -> tuple:
    """Read a profile colour as RGB, or as RGBW where a white component is given.

    The length check is the point of this. int(colour, 16) accepts a '0x'
    prefix, embedded underscores, surrounding whitespace and any number of
    digits, so 'FF0000FF' already parsed before white existed and silently gave
    blue. Telling six digits from eight is only safe once the length is known to
    be one of the two.

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
    """Load a profile and resolve its control names to protocol target IDs.

    :param path: path to the profile JSON
    :return: (profile, warnings), where profile maps a light name to
        (target, rgb) with target being ('button', id), ('light', id, index)
        or ('range', start, count), and warnings is a list of non-fatal
        complaints about entries that were skipped
    """
    with open(path, encoding='utf-8') as handle:
        profile = json.load(handle)

    resolved = {}
    warnings = []
    for name, entry in (profile.get('lights') or {}).items():
        if not isinstance(entry, dict):
            raise ProfileError(f"light '{name}': expected an object, got {type(entry).__name__}")
        # index 0 is a real light and a falsy one, so presence is the test, and an
        # index with nothing to index into is a mistake rather than a blank line
        if 'index' in entry and not entry.get('button'):
            raise ProfileError(f"light '{name}': 'index' needs a 'button' to index into")
        # a range key with nothing usable in it is a mistake, not a blank line:
        # [] and 0 and null are all falsy, so the skeleton skip below would
        # swallow them while a merely malformed [16] is refused outright
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
            # the firmware rejects a range running past its LED buffer, and staging
            # is fire-and-forget, so catch it here where it is visible
            if start < 0 or count < 1 or start + count > hlp.MAX_LEDS:
                raise ProfileError(f"light '{name}': range [{start}, {count}] is outside "
                                   f"the 0-{hlp.MAX_LEDS - 1} LED buffer")
            resolved[name] = (('range', start, count), rgb)
        else:
            control = str(entry['button']).upper()
            target = hlp.CONTROL_TARGETS.get(control)
            if target is None:
                raise ProfileError(f"light '{name}': unknown control '{entry['button']}'")
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
        raise ProfileError(f"{path} has no mapped lights - fill in the 'button' fields first")
    return resolved, warnings


def write_skeleton(path: str, names) -> None:
    """Write a profile listing every light, ready for control names to be filled in.

    :param path: file to create; refuses to overwrite
    :param names: iterable of light names as reported by the game
    """
    if os.path.exists(path):
        raise ProfileError(f"{path} already exists - remove it or choose another name")
    skeleton = {'lights': {name: {'button': '', 'colour': 'FFFFFF'} for name in sorted(names)}}
    with open(path, 'w', encoding='utf-8') as handle:
        json.dump(skeleton, handle, indent=2)
        handle.write('\n')


def resolve_frame(states: dict, profile: dict) -> dict:
    """Scale each mapped light's colour by its state, combining duplicates.

    Two game lights may land on one control; their contributions add and clamp,
    so a button lit by both reads as the brighter mix rather than whichever
    happened to be applied last.

    A mapped light the game reports as off stages black rather than being
    omitted: the game is authoritative for the controls it drives, so in
    overlay mode those controls go dark rather than showing the animation
    underneath. Only controls absent from the profile animate.

    :param states: mapping of light name to state, from the game
    :param profile: a loaded profile
    :return: mapping of target to (r, g, b)
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
            # two entries on one target may differ in width if only one of them
            # asked for white, so the narrower is padded rather than truncating
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
            # no board, so no ordinal: the entry is shown as the profile wrote it
            label = f"{hlp.control_name(target[1])}[{target[2]}]"
        else:
            label = f"range@{target[1]}+{target[2]}"
        parts.append(f"{label}=#{''.join(f'{component:02X}' for component in rgb)}")
    return ' '.join(parts)
