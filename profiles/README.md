<!--
hlp-spice2x (spice2x to GP2040-CE Host Lighting bridge)
Filename: README.md
Folder: profiles
https://github.com/djGLiTCH/hlp-spice2x
SPDX-FileCopyrightText: (C) 2026 Jacob Simpson (DJ GLiTCH)
SPDX-License-Identifier: GPL-3.0-or-later
See LICENSES/GPL-3.0-or-later.txt at the repository root.
-->

# Profiles

A profile maps one game's light names onto the controls of a board. Light
names come from the game, so a profile is per game.

How portable a profile is depends on the form of its entries, set out under
[Entries](#entries).

`example.json` is an illustration. Its names are invented and no real game
reports them.

## Capturing a profile

Start the game under spice2x with the API enabled, then list its lights:

```
hlp-spice2x --port 1337 --list-lights
```

This prints every light and its current value. Pressing buttons while
watching the values shows which name is which.

Where a fixture's red, green and blue are three lights, the list shows them
on one line as the fixture, with each channel's value.

Write a skeleton listing every light:

```
hlp-spice2x --port 1337 --write-profile mygame.json
```

The skeleton writes each fixture as one entry, set out under
[Fixtures](#fixtures). Fill in the `button` field of each light or fixture
to map and leave the rest empty.
Check the result with no board:

```
hlp-spice2x --port 1337 --profile mygame.json --dry-run
```

A dry run has no board, so it shows an `index` entry as the control and index
the profile gives, such as `Up[1]`. The light it picks is known only with a
board attached.

## Entries

```json
{
  "lights": {
    "P1 Button 1": { "button": "B1", "colour": "FF0000" },
    "P1 Extra Up": { "button": "UP", "index": 1, "colour": "00FFFF" },
    "Neon Left":   { "range": [16, 30], "colour": "FF00FF" },
    "Woofer B":    { "button": "E2", "channel": "B" },
    "Top Panel":   { "button": "E1", "channels": { "R": "Top Panel R",
                                                   "G": "Top Panel G",
                                                   "B": "Top Panel B" } }
  }
}
```

### button

`button` names a control or a kind of light, and the board resolves it:

- `Up` `Down` `Left` `Right` `B1` `B2` `B3` `B4` `L1` `R1` `L2` `R2` `S1` `S2`
  `L3` `R3` `A1` `A2` `A3` `A4` and `E1`-`E12` name controls.
- `PLED1`-`PLED4`, `TURBO` and `CASE` name a kind of light: a player LED, the
  turbo lights, and the case strip.
- Any other name is the board's firmware's name for an action or a kind of
  light, as its page 3 gives it, such as `BUTTON_PRESS_TURBO` or
  `Player1Light`. Case does not matter. A name a later firmware adds works as
  soon as the board runs that firmware. `hardware_probe` shows a wired control
  that has no short name by this name. `hlp-caps --names --page names`, from
  gp2040ce-hostlighting, lists every name the board gives.

At startup the bridge looks each name up on the board's page 3, and stages it
by the value the board's own firmware gives it.

Where a control has several lights, its name lights all of them. `CASE` and
`TURBO` reach every light of their kind. An empty field ignores that light.

### index

`index` picks one light of a control, or of a kind, counting from 0 as the
board's Lights page lists them.

### range

`range` addresses raw LED indexes as `[start, count]`, for lights that belong
to no control. The bridge warns at startup if a range reaches past the last
LED the board has. LEDs past that can be staged but are never shown, and the
part of a range past the board's addressable limit is not sent.

### colour

`colour` is `RRGGBB` hex, scaled by how brightly the game drives that light.
`color` is accepted too. The default is white. A colour is six hex digits, or
eight for `RRGGBBWW`. A `0x` prefix, underscores, surrounding spaces and any
other length are refused.

Cabinet lights are mostly single-colour, so the game reports an intensity
from 0.0 to 1.0 and the profile supplies the colour. Two lights mapped to the
same control add together and clamp.

`colour` is for these intensity lights. A light that is one channel of an
RGB fixture takes a `channel` or belongs to a fixture entry instead.

### channel

`channel` marks a light as one channel of a fixture, in place of a `colour`:

```json
"Woofer R": { "button": "E1", "channel": "R" }
```

A channel is `R`, `G`, `B` or `W`, in upper or lower case. It lights in that
primary colour, scaled by how brightly the game drives it. `W` is sent as a
six-digit white, which works on any chain. On a white chain the firmware
moves a pure grey onto the white emitter. An entry with both a `channel` and
a `colour` is refused.

### Portability

| Form | Portability |
|---|---|
| `button` | Any board. The board resolves the name against its own configuration. |
| `index` | The control name is portable and the index is not. On another board it can pick a different light, and nothing reports it. |
| `range` | One board. Raw LED indexes depend on that board's layout. |

A `channel` entry or a fixture is as portable as the form it is mapped by.

## Reading a board's lights

The bridge prints at startup which controls have more than one light. The
probe prints every light, the control it belongs to, and its ordinal. Run it
from a checkout of this repository or the tools download:

```
python -m tests.hardware_probe
```

It is read-only and can run while a game is running. It prints the board's
Lights page grouped by control and marks any control with more than one
light, which an `index` entry needs. It also prints the Controls page, every
control the board has, lit or not.

## Fixtures

Some games drive an RGB fixture as three lights, its red, green and blue. A
fixture entry maps all three to one place:

```json
"WHITE Woofer": {
  "button": "E1",
  "channels": {
    "R": "WHITE Woofer R",
    "G": "WHITE Woofer G",
    "B": "WHITE Woofer B"
  }
}
```

The entry's own name is a label, and `channels` gives the game's light for
each channel. The fixture takes a `button`, an `index` or a `range` as a
light does. It takes no `colour`, since each channel lights in its own. The
channels add together, so the fixture shows the colour the game mixes.

The skeleton groups a fixture when the game has its R, G and B lights:

- Each name is the same up to a last letter of `R`, `G` or `B`.
- A space, an underscore, a hyphen or nothing comes before the letter.
- A `W` light with the same name before the letter joins the group.

A light whose name ends in R for right, such as `FX-R`, has no G or B to go
with it, so it stays a light of its own. The fixture is named for the part
before the letter. Where a light already has that name, RGB is added to it.

To send a fixture's channels to different controls, give each light a
`channel` entry. Leave the fixture's `button` empty or remove the fixture. A
light named by two mapped entries is refused.

## Controls with several lights

Some layouts give one control two buttons, such as a second Up. The control's
name lights all of them. An `index` lights one:

```json
"Extra Up": { "button": "UP", "index": 1, "colour": "0000FF" }
```

An `index` entry wins over the control name, including when it asks for
black. A profile can name `UP` to light both and then override one of them.

## Boards with a white channel

On a GRBW or RGBW chain, an eight-digit colour asks for the white emitter:

```json
"Marquee": { "button": "S2", "colour": "FFFFFF00" }
```

The bridge moves the achromatic part of the colour onto the white emitter. A
white component given in the colour is added to it, so `FF0000FF` is red plus
full white and `FF000080` is red plus half.

Where two entries name the same light and only one asks for white, the white
is kept.

On a chain with no white emitter the bridge drops the white and sends RGB. It
reports this once at startup.

A six-digit colour is always sent as RGB. On a white chain the firmware moves
a pure grey (`R` = `G` = `B`) onto the white emitter, as it does for its own
animations, so a six-digit white still lights the white LED.

## A profile that does not fit the board

A fault in the profile itself is reported before the bridge connects. The
faults are:

- a control name no board can give
- a malformed range or fixture
- a colour that is not hex, or a channel that is not R, G, B or W
- a light mapped by two entries

A board gives names of printable ASCII up to 40 bytes long.

A key given twice in one object, such as a light listed twice, is not a
fault. JSON keeps only the last of them, so the bridge names the key in a
warning at startup and uses the last.

A mapped control the board cannot light is not an error. The bridge names it
at startup, as not being in this board's firmware, as having no LED on this
board or as not being on this board, and
carries on.

An `index` entry is checked at connect, against the board. An index past the
end of a control is refused, and the message gives the number of lights that
control has.

A board can change while the bridge runs. Switching its input profile can
move a control to another button. The bridge reads the board again and
carries on. An `index` entry the board can no longer honour is held back with
a warning.
