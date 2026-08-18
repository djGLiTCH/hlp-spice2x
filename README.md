# hlp-spice2x

Drive a [GP2040-CE](https://github.com/OpenStickCommunity/GP2040-CE) controller's
RGB LEDs from the cabinet lighting of games running under
[spice2x](https://github.com/spice2x/spice2x.github.io).

spice2x reports every light the running game drives through its Spice API. This
polls that list and paints the matching buttons on your board, so an IIDX or
SDVX cabinet's lighting shows up on your controller.

## How it works

The board's Host Lighting add-on exposes a vendor HID interface carrying the
Host Lighting Protocol, which lets a host take over the LEDs frame by frame.
The bridge sits between the two:

```
spice2x  --(Spice API, TCP/JSON)-->  hlp-spice2x  --(HID)-->  GP2040-CE board
```

Mapping game lights to your buttons lives in a **profile**, a JSON file, because
light names differ from game to game. Profiles name *controls* (`B1`, `S2`,
`Up`), not LED indexes: the board reports its own button-to-LED map, and the
firmware resolves the control to whatever LEDs that user actually wired. One
profile therefore works across board layouts.

## Requirements

- A GP2040-CE board running firmware with the **Host Lighting** add-on, enabled
  in the web configurator (Configuration -> Add-Ons -> Host Lighting; off by
  default). Not in an official release yet - see [Firmware](#firmware).
- spice2x started with `-api PORT`, plus `-apipass PASS` if you set a password
- Python 3.9 or later

## Firmware

Host Lighting is a proposed add-on rather than a shipped feature. It is open
upstream as
[PR #1691](https://github.com/OpenStickCommunity/GP2040-CE/pull/1691), so until
that merges you need a test build:

**[HLP v1.3 test builds](https://github.com/djGLiTCH/GP2040-CE/releases/tag/HLP_v1.3)**

There is a UF2 for every board GP2040-CE supports, on both the classic and
LED-refactor pipelines, along with flashing and setup instructions. Enable the
add-on in the web configurator once the board is running one of them.

The protocol itself is documented in
[docs/host-lighting.md](https://github.com/djGLiTCH/GP2040-CE/blob/20260811-host-lighting-protocol/docs/host-lighting.md).

## Install

```
pip install git+https://github.com/djGLiTCH/hlp-spice2x
```

## Quick start

With the game running, see what lights it exposes:

```
hlp-spice2x --port 1337 --list-lights
```

Generate a profile skeleton listing every one of them:

```
hlp-spice2x --port 1337 --write-profile iidx.json
```

Open `iidx.json` and fill in the `button` field for each light you care about,
leaving the rest empty. Then run the bridge:

```
hlp-spice2x --port 1337 --profile iidx.json
```

Press Ctrl-C to stop. The board returns to its own animations, and does so by
itself within two seconds if the bridge is killed or the game exits.

## Profile format

```json
{
  "lights": {
    "P1 Button 1": { "button": "B1", "colour": "FF0000" },
    "P1 Start":    { "button": "S2", "colour": "FFAA00" },
    "Neon Left":   { "range": [16, 30], "colour": "FF00FF" }
  }
}
```

- **`button`** names a control: `Up Down Left Right B1 B2 B3 B4 L1 R1 L2 R2 S1
  S2 L3 R3 A1 A2`, plus `PLED1`-`PLED4`, `TURBO` and `CASE` for the whole case
  strip. Leave it empty to ignore that light.
- **`range`** addresses raw LED indexes as `[start, count]`, for lights that do
  not correspond to a control. Unlike `button`, this is tied to the board it was
  written for.
- **`colour`** is `RRGGBB` hex, scaled by how brightly the game is driving that
  light. `color` is accepted too. Defaults to white.

Cabinet lights are mostly single-colour, so the game only reports an intensity
from 0.0 to 1.0 - the colour is your choice. Two lights mapped to the same
control add together and clamp.

## Options

| Option | Effect |
|---|---|
| `--port N` | The port you passed to spice2x's `-api`. 1337 by convention, but spice2x has no default of its own. |
| `--password PASS` | Matches spice2x's `-apipass`. Required if you set one, since the traffic is then encrypted. |
| `--host ADDR` | Where spice2x is listening, default `127.0.0.1`. Only needed if the game runs on a different machine from the board. |
| `--overlay` | Paint only the mapped controls and let everything else keep animating. Without it the whole frame is taken and unmapped LEDs go dark. |
| `--full-brightness` | Ignore the board's configured brightness and render the colours the game asks for. |
| `--fps N` | Poll rate, default 60. |
| `--board-id PREFIX` | Pick one of several connected boards, by the prefix of its factory ID. |
| `--dry-run` | Print frames instead of driving a board. Never opens the HID interface. |
| `--timeout MS` | Takeover keepalive, default 2000 ms. The board restores its animations if it stops hearing from the host for this long. |

`--list-lights`, `--write-profile` and `--dry-run` talk to spice2x alone, so you
can build and check a profile on the game PC with no board attached.

## Notes

Frames are only sent when something actually changes; quiet frames send a single
keepalive instead. Polling at 60 Hz against a stand-in server with static
lighting, 703 polls produced one board update.

The bridge does not use spice2x's built-in HID lights feature. That expects a
device declaring its lights in its HID descriptor, which cannot describe a board
whose LED map the user configures. Going through the Spice API instead is what
lets one profile work on any board.

## Contributing profiles

Profiles for real games are welcome. Generate one with `--write-profile`, fill
it in, check it with `--dry-run`, and open a pull request adding it to
`profiles/`. Please say which game and version it was captured from.

## Licence

GPL-3.0-or-later. See [LICENSE](LICENSE).

`hlp_spice2x/hlp.py` is a trimmed copy of the Host Lighting helpers from
[gp2040ce-binary-tools](https://github.com/OpenStickCommunity/gp2040ce-binary-tools),
vendored so this tool needs nothing but `hidapi`.
