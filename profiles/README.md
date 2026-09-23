# Profiles

A profile maps one game's light names onto controls on your board. Light names
come from the game, so a profile is per-game.

How far it travels between boards depends on which kind of entry you write.
Naming a control lets the firmware resolve it to whatever LEDs that board
actually has, so those entries work anywhere. Picking one light of a control, or
addressing raw LED indexes, ties the entry to the board it was written against
in different ways and to different degrees. The three forms are set out below.

`example.json` is illustrative only. Its names are made up, and no real game
reports exactly those.

## Capturing one

Start the game under spice2x with the API enabled, then:

```
hlp-spice2x --port 1337 --list-lights
```

That prints every light and its current value. Pressing buttons and watching
the values change is the quickest way to work out which name is which.

Then generate a skeleton with every light listed:

```
hlp-spice2x --port 1337 --write-profile mygame.json
```

Fill in the `button` field for the lights you want and leave the rest empty.
Check the result without touching a board:

```
hlp-spice2x --port 1337 --profile mygame.json --dry-run
```

A dry run has no board, so it shows an `index` entry as the control and index
you wrote, such as `Up[1]`, rather than the light that would end up lit. Which
light that is only becomes knowable once a board is attached.

## What an entry can say

```json
{
  "lights": {
    "P1 Button 1": { "button": "B1", "colour": "FF0000" },
    "P1 Extra Up": { "button": "UP", "index": 1, "colour": "00FFFF" },
    "Neon Left":   { "range": [16, 30], "colour": "FF00FF" }
  }
}
```

**`button`** names a control and lets the board resolve it:

> `Up` `Down` `Left` `Right` `B1` `B2` `B3` `B4` `L1` `R1` `L2` `R2` `S1` `S2`
> `L3` `R3` `A1` `A2`, plus `PLED1`-`PLED4`, `TURBO`, and `CASE` for the whole
> case strip.
>
> `A3`, `A4` and `E1`-`E12` are also accepted, on boards whose firmware reports
> them. That needs HLP v1.1 or newer, and they only stage by name from v1.2 - on
> v1.1 the bridge reaches them by raw index instead, which it works out for
> itself.

Where the board says a control owns several lights, naming it lights all of
them. Leave the field empty to ignore that light.

**`index`** picks out one light of a control, counting as the board's own light
table lists them, starting at 0. Needs HLP v1.2 or newer.

**`range`** addresses raw LED indexes as `[start, count]`, for lights that do not
correspond to any control. The bridge warns at startup if a range reaches past
the last LED the board has, because staging is fire-and-forget and the board
simply drops what it cannot address, so the lights would otherwise just never
come on.

**`colour`** is `RRGGBB` hex, scaled by how brightly the game is driving that
light. `color` is accepted too. Defaults to white. It must be exactly six hex
digits, or eight for `RRGGBBWW` - a `0x` prefix, underscores, surrounding spaces
or any other length are refused, because telling a six-digit colour from an
eight-digit one is only safe when the length means something.

Cabinet lights are mostly single-colour, so the game only reports an intensity
from 0.0 to 1.0 and the colour is your choice. Two lights mapped to the same
control add together and clamp.

### How far each form travels

| Form | Portable? |
|---|---|
| `button` | Anywhere. The board resolves the name against its own wiring. |
| `index` | The control name travels; the index does not. It picks a light by a position only that board's light table defines, so on another board it may pick a different light - and it will still be a valid light, so nothing reports it. |
| `range` | Nowhere. Raw LED indexes mean whatever that board's layout says they mean. |

## Finding out what your board has

Running the bridge prints which controls own more than one light. For the full
picture - every light, which control owns it, and its ordinal - use the probe,
from a checkout of this repository or the tools download:

```
python -m tests.hardware_probe
```

It is read-only, so it is safe to run while a game is going. It prints the
board's light table grouped by control, marking any control that owns more than
one light, which is exactly what you need before writing an `index` entry. On
v1.4 firmware it also prints the control table: every control the board has, lit
or not.

## Boards that give one control several lights

Some layouts wire two buttons to the same control, such as a second Up. Naming
the control lights all of them. To light just one, add an `index`:

```json
"Extra Up": { "button": "UP", "index": 1, "colour": "0000FF" }
```

An `index` entry always wins over the control name, even when it is asking for
black. So a profile can name `UP` to light both and then override one of them,
which is the usual reason to reach for it.

## Boards with a white channel

On a GRBW or RGBW chain, an eight-digit colour asks for the white emitter:

```json
"Marquee": { "button": "S2", "colour": "FFFFFF00" }
```

The achromatic part of the colour is moved onto the white emitter, so white is
sent as white rather than as equal parts of the three colour emitters. Any white
you give explicitly is added on top of that, so `FF0000FF` is red plus full
white and `FF000080` is red plus half.

Where two entries name the same light and only one of them asks for white, the
white is kept. Whether a light uses the white channel is a property of the light,
not of whichever entry happens to be listed last.

This needs HLP v1.3 as well as a chain with a white emitter. On anything older
the firmware maps achromatic colours onto the white emitter itself and ignores
what the host sent, so the bridge drops the white and sends plain RGB, which is
what renders correctly there. It says so once at startup rather than per frame.

## When a profile does not fit the board

Anything wrong with the profile itself - an unknown control name, a malformed
range, a colour that is not hex - is reported before the bridge connects to
anything.

A mapped control the board cannot light is not an error. The bridge names it at
startup and carries on: as having no LED on this board, or, from v1.4, as not
being on this board at all.

Whether the board can honour an `index` entry is a different question, and one
only the board can answer, so it is settled at connect:

- **Firmware older than v1.2** cannot colour a single light at all. The bridge
  refuses to start and names the version needed. Lighting something other than
  what the profile asked for would be worse than not starting.
- **A board whose light table is synthesised** from its per-control
  configuration holds one row per control however many lights that control
  really drives, so it can never describe the second one. The bridge refuses and
  says so, because this is a limitation of the board rather than of its firmware
  and upgrading would change nothing.
- **An index past the end of a control** is refused, and the message says how
  many lights that control actually has.
- **A board that has not published its light table yet** is none of the above.
  Its light registry is filled in while it sets its LEDs up, so a host that
  connects in the moment before that finishes sees nothing there. Those entries
  are held back with a warning and start working on their own once the table
  appears.

## Contributing

Pull requests adding a profile are welcome. Please name the file after the
game, and note in the PR which game and version you captured it from, since
light names can change between releases.
