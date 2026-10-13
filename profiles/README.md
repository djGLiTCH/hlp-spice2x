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

Write a skeleton listing every light:

```
hlp-spice2x --port 1337 --write-profile mygame.json
```

Fill in the `button` field of each light to map and leave the rest empty.
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
    "Neon Left":   { "range": [16, 30], "colour": "FF00FF" }
  }
}
```

### button

`button` names a control or a kind of light, and the board resolves it:

- `Up` `Down` `Left` `Right` `B1` `B2` `B3` `B4` `L1` `R1` `L2` `R2` `S1` `S2`
  `L3` `R3` `A1` `A2` `A3` `A4` and `E1`-`E12` name controls.
- `PLED1`-`PLED4`, `TURBO` and `CASE` name a kind of light: a player LED, the
  turbo lights, and the case strip.

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

### Portability

| Form | Portability |
|---|---|
| `button` | Any board. The board resolves the name against its own configuration. |
| `index` | The control name is portable and the index is not. On another board it can pick a different light, and nothing reports it. |
| `range` | One board. Raw LED indexes depend on that board's layout. |

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

A fault in the profile itself is reported before the bridge connects: an
unknown control name, a malformed range, or a colour that is not hex.

A mapped control the board cannot light is not an error. The bridge names it
at startup, as having no LED on this board or as not being on this board, and
carries on.

An `index` entry is checked at connect, against the board. An index past the
end of a control is refused, and the message gives the number of lights that
control has.

A board can change while the bridge runs. Switching its input profile can
move a control to another button. The bridge reads the board again and
carries on. An `index` entry the board can no longer honour is held back with
a warning.

## Contributing

Pull requests adding a profile are welcome. Name the file after the game, and
state in the pull request which game and version it was captured from, since
light names can change between releases.
