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

## A worked example of the protocol

This bridge doubles as a real-world example of implementing Host Lighting
Protocol support in a host application, for anyone adding HLP to their own
tooling for GP2040-CE based controllers. It is a working tool first, but it was
built against the whole protocol rather than the parts one program happens to
need, and it drives every shipped version - v1.0 through v1.4 - from one code
path, deciding what a board can do by asking it.

The parts worth reading, in the order a host has to do them:

| Where | What it shows |
|---|---|
| `hlp_spice2x/hlp.py` | The wire format: report framing, sequence matching, every capability page decoded field by field, and the staging commands each version added |
| `hlp.negotiate` | Reading the version and the capability pages once, at connect, and refusing an unknown major version rather than driving it on assumptions that may no longer hold |
| `hlp.HostLightingCapabilities` | Turning a version and a feature bitmask into questions a call site can ask, so a later protocol version is one new field here rather than a hunt for version comparisons |
| `bridge.staging_for` | Choosing how each target reaches the board's lights, and what it falls back to when the newest command is not available |
| `bridge.send_frame` | The staging passes, and why the order they are emitted in is load-bearing |
| `tests/fake_board.py` | A board fake parameterised by protocol version, which gates its own replies the way firmware does, so every version can be tested without five boards |

Four things the protocol rewards, learned building this:

- **Negotiate once, at connect, and freeze the result.** Re-deriving what a board
  can do inside a streaming loop spends round trips on an answer that cannot have
  changed without the board saying so.
- **Branch on capabilities, not on version numbers.** Three of the capabilities
  here deliberately do not follow from the version: the light and control tables
  need a feature bit as well, and a host-supplied white component needs the LED
  chain to have somewhere to put it.
- **Give every capability a fallback.** The same profile has to work on a v1.0
  board, just with less finesse.
- **Clamp a newer minor version rather than refusing it.** Within a major version
  the protocol only ever adds, so a board newer than your client still keeps
  every promise your client relies on. Refusing it strands the user on firmware
  that is better at everything they asked for.

The protocol itself is specified in
[docs/host-lighting.md](https://github.com/djGLiTCH/GP2040-CE/blob/HLP_v1.4/docs/host-lighting.md).

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

**[HLP v1.4 test builds](https://github.com/djGLiTCH/GP2040-CE/releases/tag/HLP_v1.4)**
- the current release, and the one to use

There is a UF2 for every board GP2040-CE supports, on the merged LED pipeline,
along with flashing and setup instructions. Enable the add-on in the web
configurator once the board is running one of them.

Earlier releases are kept because the bridge supports all of them, and because
they are what makes each version gate testable on real hardware rather than only
against a fake:
[v1.3](https://github.com/djGLiTCH/GP2040-CE/releases/tag/HLP_v1.3) |
[v1.2](https://github.com/djGLiTCH/GP2040-CE/releases/tag/HLP_v1.2) |
[v1.1](https://github.com/djGLiTCH/GP2040-CE/releases/tag/HLP_v1.1) |
[v1.0](https://github.com/djGLiTCH/GP2040-CE/releases/tag/HLP_v1.0)

The protocol itself is documented in
[docs/host-lighting.md](https://github.com/djGLiTCH/GP2040-CE/blob/HLP_v1.4/docs/host-lighting.md).

## Protocol versions

The bridge speaks every shipped version of the Host Lighting Protocol and works
out which one it is talking to at connect, so the same profile and the same
command line work on any of them. What differs is how much finesse is available.

| Version | What the board gains | What the bridge does with it |
|---|---|---|
| v1.0 | the baseline | Control names and raw LED ranges. Finds out which controls have lights by writing a black frame to each and seeing what sticks. |
| v1.1 | the light table, the reported render rate | Reads which controls have lights instead of probing for them, streams at the board's own rate, and lights **every** light of a control that has more than one. |
| v1.2 | colouring a single light by its ordinal | The `index` profile entry. Extended controls (`A3`, `A4`, `E1`-`E12`) stage by name. |
| v1.3 | a per-entry outcome mask, and the RGBW form of per-light staging | Notices when the board skipped a light and re-reads its map, and honours a white component in a profile colour. |
| v1.4 | the control table: every control the board has, lit or not | Tells a mapped control the board does not have apart from one it has with no LED, in the startup warnings. |

A board reporting a **newer minor version** than this bridge knows is driven as
the newest it does know, never refused: within a major version the protocol only
ever adds, so such a board still keeps every promise being relied on.

A board reporting a **different major version** is refused, because a major
version is the one thing allowed to change what existing commands mean. `--force`
overrides that for testing, behind a warning that the run is not reliable.

The negotiated result is printed at startup, so what the bridge decided is
visible rather than guessed at:

```
board: Haute42 COSMOX M Ultra (v0.7.12-87-g6ed54b5)
board speaks HLP v1.4 - light table, control table, per-light staging, outcome mask, renders at 40 Hz (white channel: no)
takeover: whole frame, 2000 ms keepalive, applying board brightness
controls the board gives more than one light, all of which will be lit: L3, Up
streaming at 40 fps (matching the board's render rate)
```

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

The run reports what it negotiated, warns about anything in the profile this
board cannot light, and prints a summary when it stops:

```
1186 frames in 29.7s (40.0/s published), 0 commits unacknowledged
released - on-board animations restored
```

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
  strip. `A3`, `A4` and `E1`-`E12` are also accepted on boards whose firmware
  reports them, which needs HLP v1.1 or newer. Leave it empty to ignore that
  light. Where the board says a control owns several lights, such as a layout
  with two Up buttons, all of them are lit.
- **`index`** picks out one light of a control, counting as the board's own
  light table lists them, so `{ "button": "UP", "index": 1 }` is the second Up
  button on a layout that has two. It needs HLP v1.2 or newer. On older
  firmware, or where the board's table cannot say which lights belong to that
  control, the bridge refuses to start rather than lighting something else. A
  board that simply has not published its table yet is a different case: those
  entries are held back with a warning and resolve on their own once it
  appears.
- **`range`** addresses raw LED indexes as `[start, count]`, for lights that do
  not correspond to a control. Unlike `button`, this is tied to the board it was
  written for.
- **`colour`** is `RRGGBB` hex, scaled by how brightly the game is driving that
  light. `color` is accepted too. Defaults to white. Eight digits as `RRGGBBWW`
  asks for the board's white channel, which needs a GRBW or RGBW chain and HLP
  v1.3; anywhere else the white is dropped, the colour is sent as RGB, and the
  reason is printed once at startup.

The three forms differ in how portable they are. A `button` entry works on any
board. An `index` entry names a portable control but picks a light by a position
only that board's light table defines, so it may pick a different light
elsewhere. A `range` entry is raw LED indexes and is portable nowhere.

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
| `--fps N` | Poll rate. Defaults to the board's own render rate where it reports one slower than 60, and to 60 otherwise. Passing this overrides both. |
| `--list-boards` | List the attached boards - factory ID, label, protocol version and firmware - and exit. Takes no board over and needs no game. |
| `--board-id PREFIX` | Pick one of several connected boards, by the prefix of its factory ID. `--list-boards` shows the IDs. |
| `--dry-run` | Print frames instead of driving a board. Never opens the HID interface. |
| `--timeout MS` | Takeover keepalive, default 2000 ms. The board restores its animations if it stops hearing from the host for this long. |
| `--force` | Run against a protocol major version this bridge does not support. Unsupported and for testing only; commands may not mean what the bridge thinks they mean. |

`--list-lights`, `--write-profile` and `--dry-run` talk to spice2x alone, so you
can build and check a profile on the game PC with no board attached.
`--list-boards` talks to the boards alone. A dry run
has no light table to resolve an `index` entry against, so it shows those
entries as the profile wrote them, such as `Up[1]`.

## Notes

Frames are only sent when something actually changes; quiet frames send a single
keepalive instead. Against a stand-in server with static lighting, 703 polls
produced one board update.

Streaming faster than the board renders is wasted work, so the bridge matches a
board that reports a slower rate and caps a faster one at 60: cabinet lighting
gains nothing visible above that, and the headroom is worth more than the rate
when a poll runs late.

The bridge does not use spice2x's built-in HID lights feature. That expects a
device declaring its lights in its HID descriptor, which cannot describe a board
whose LED map the user configures. Going through the Spice API instead is what
lets one profile work on any board.

## Tests

```
pip install -e ".[dev]"
pytest
```

The suite covers all five protocol versions without needing a board, by running
the real client against a fake that is parameterised by version and gates its own
replies the way firmware does.

There are also checks that need a board, for confirming behaviour on real
hardware: what a board reports about itself, whether each light is addressed
correctly, whether the staging order holds, and whether the whole program works
driven from its own entry point.

```
python -m tests.hardware_probe        # read-only, safe to run mid-game
python -m tests.hardware_sweep        # eleven phases, watched by eye
```

[tests/README.md](tests/README.md) explains what every test file is for, when to
reach for it, and how to run it.

## Contributing profiles

Profiles for real games are welcome. Generate one with `--write-profile`, fill
it in, check it with `--dry-run`, and open a pull request adding it to
`profiles/`. Please say which game and version it was captured from.

## Changelog

What changed in each release, and anything that needs action when upgrading:
[CHANGELOG.md](CHANGELOG.md).

## Licence

GPL-3.0-or-later. See [LICENSE](LICENSE).

`hlp_spice2x/hlp.py` is a trimmed copy of the Host Lighting helpers from
[gp2040ce-binary-tools](https://github.com/OpenStickCommunity/gp2040ce-binary-tools),
vendored so this tool needs nothing but `hidapi`.
