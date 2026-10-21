# hlp-spice2x

hlp-spice2x drives the RGB LEDs of a [GP2040-CE][gp2040-ce] controller from
the cabinet lighting of games running under [spice2x][spice2x].

spice2x reports every light the running game drives through its Spice API.
The bridge polls that list and colours the matching controls on the board, so
the lighting of an IIDX or SDVX cabinet shows on the controller.

## How it works

The board's Host Lighting add-on exposes a vendor HID interface carrying the
Host Lighting Protocol (HLP), which lets a host take over the LEDs frame by
frame. The bridge sits between the game and the board.

```
spice2x  --(Spice API, TCP/JSON)-->  hlp-spice2x  --(HID)-->  GP2040-CE board
```

A **profile** is a JSON file that maps the game's lights to the board's
controls. Light names differ from game to game, so each game has its own
profile. A profile names controls (`B1`, `S2`, `Up`) and not LED indexes. The
bridge stages a colour by the control's name and the firmware resolves it to
the LEDs that board has for it, so one profile works across board layouts.

This version speaks HLP 2.0 only. A board on the pre-release 1.x protocol
needs [hlp-spice2x v0.3.0][v0.3.0] or a firmware update. See
[Protocol version](#protocol-version).

## Example host implementation

The bridge is also an example of a host application built on
[gp2040ce-hostlighting][hostlighting], the reference host for the Host
Lighting Protocol. The library speaks the protocol and runs its whole host
flow. It claims the board, shares it with other applications, and follows
changes to the board's configuration. The bridge decides what to show.

The parts to read, in the order a host does them:

| Where | What it shows |
|---|---|
| `hlp.REQUIRED`, `hlp.requirements` | What the bridge needs a board to list in `HELLO`, with the addressing modes a profile uses |
| `hlp.HostLightingCapabilities` | Answering call sites from the library's cache of the pages |
| `bridge.run` | The loop on the library's `Session`: the claim, the events, the pages read again, the frames and the reconnect |
| `bridge.staging_for` | How each target reaches the board's lights: by action, by kind, by light ordinal or as an LED run |
| `bridge.frame_for` | A frame as the library's `Frame`, in the order of the passes |
| `tests/boards.py` | The library's model board, wired from lights and controls named as a profile names them, so the host flow is tested without hardware |

What a host on this protocol does:

- It gates on capabilities and never on the version. `HELLO` lists the
  commands, pages, request flags, addressing modes, pixel formats and events
  a board implements. The minor version decides nothing.
- It claims the board before driving it. The claim keeps applications sharing
  a board apart, and its generation lets the board refuse a host that has not
  yet seen a change of holder.
- It lets the board resolve names. `STAGE` by action reaches every light of a
  control, so a host that names only controls needs no table of LEDs.
- It watches the state token. The token changes whenever the board's lights
  or controls do, and the Summary's change counters name the pages to read
  again.

The protocol is specified in [docs/host-lighting.md][spec].

## Requirements

- A GP2040-CE board running firmware with the Host Lighting add-on, enabled
  in the web configurator under Configuration, Add-Ons, Host Lighting. The
  add-on is off by default and is not in an official release yet. See
  [Firmware](#firmware).
- spice2x started with `-api PORT`, and with `-apipass PASS` where a password
  is wanted.
- Python 3.14 or later, and [gp2040ce-hostlighting][hostlighting], which pip
  installs with the bridge.

## Firmware

Host Lighting is a proposed add-on, open upstream as [PR #1691][pr-1691].
Until it merges the board needs a test build. This version was written
against the [HLP v2.0 release candidate 9 test builds][rc9].

There is a UF2 for every board GP2040-CE supports, with flashing and setup
instructions. Enable the add-on in the web configurator once the board runs
one of them.

HLP 2.0 is a release candidate, so this version of the bridge is a
pre-release until the protocol is final.

## Protocol version

The bridge speaks HLP 2.0. It finds a board by the protocol's USB usage, which
carries the major version, and confirms the version with `HELLO`. It decides
everything else from the capabilities the board lists.

| The board lists | What the bridge does with it |
|---|---|
| `CLAIM`, `CONFIGURE`, `STAGE`, `CLEAR`, the request flags, and pages 1, 8 and 11 | Required. Without them the bridge stops and names what is missing. |
| The addressing modes | Required as the profile uses them. A control name is staged by action. `CASE`, `TURBO` and `PLED1`-`PLED4` are staged by kind, an `index` by light and a `range` as an LED run. |
| Page 10, the controls | The startup warnings tell a mapped control the board does not have from one it has with no LED. |
| `SUBSCRIBE` and the `STATE_CHANGED` event | The bridge learns of a change on the board from an event. With or without them it reads page 1 at least once a second. |
| The RGBW pixel format, on a chain the Summary flags as having a white channel | A white component in a profile colour is sent. |

A later 2.x board is driven the same way, by what it lists.

This version does not find firmware on the pre-release 1.x protocol. If such
a board is attached, the bridge reports it. Update the board to an HLP 2.0
build, or use hlp-spice2x v0.3.0 with it, which speaks v1.0 to v1.4.

The bridge prints what it read at startup. This example is from a Haute42
COSMOX M Ultra:

```
board: Haute42 COSMOX M Ultra (v0.7.12-138-ge9313e9)
board speaks HLP v2.0: 46 lights on 46 LEDs, renders at 40 Hz, GRB chain (white channel: no), state changes by event
takeover: whole frame, 2000 ms keepalive, applying board brightness
controls the board gives more than one light, all of which will be lit: L3, Up
streaming at 40 fps (matching the board's render rate)
```

## Sharing a board

Several applications can have a board's lighting interface open at once. They
coordinate through the protocol's claim.

- At startup the bridge claims the lights under the name `hlp-spice2x`. If
  another application holds them, it stops and names that application. Close
  that application, or pass `--force` to take the lights over from it.
- While it runs it renews the claim several times per keepalive timeout.
- If another application takes the lights over, the bridge reports it, stops
  sending frames, and waits. When the board is unclaimed again, the bridge
  claims the lights and carries on.
- When it stops it gives the claim up, and the board returns to its own
  animations at once.

## Install

```
pip install git+https://github.com/djGLiTCH/hlp-spice2x
```

## Quick start

With the game running, list the lights it exposes:

```
hlp-spice2x --port 1337 --list-lights
```

Write a profile skeleton listing every one of them:

```
hlp-spice2x --port 1337 --write-profile iidx.json
```

Open `iidx.json` and fill in the `button` field of each light to map. Leave
the rest empty. Then run the bridge:

```
hlp-spice2x --port 1337 --profile iidx.json
```

Press Ctrl-C to stop, or close the game. Both return the board to its own
animations. If the bridge is killed, the board restores its animations when
the keepalive timeout ends, 2 s by default.

If the board leaves the bus, as it does when another application or the web
configurator reboots it, the bridge reports it and waits. When the board
returns, the bridge starts again on it.

The run reports what it read from the board, warns about anything in the
profile this board cannot light, and prints a summary when it stops:

```
1185 frames in 29.6s (40.0/s published), 0 frames unacknowledged
claim given up, on-board animations restored
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

- `button` names a control: `Up Down Left Right B1 B2 B3 B4 L1 R1 L2 R2 S1
  S2 L3 R3 A1 A2 A3 A4` and `E1`-`E12`. `PLED1`-`PLED4`, `TURBO` and `CASE`
  name the player LEDs, the turbo lights and the case strip. An empty
  `button` ignores that light. Where a control has several lights, such as a
  layout with two Up buttons, all of them are lit.
- `index` picks one light of a control, counting as the board's Lights page
  lists them. `{ "button": "UP", "index": 1 }` is the second Up button on a
  layout that has two. Where the control has no such light, the bridge does
  not start.
- `range` addresses raw LED indexes as `[start, count]`, for lights that
  belong to no control. It is tied to the board it was written for.
- `colour` is `RRGGBB` hex, scaled by how brightly the game drives that
  light. `color` is accepted too. The default is white. Eight digits as
  `RRGGBBWW` ask for the board's white channel, which needs a GRBW or RGBW
  chain. On any other chain the white is dropped, the colour is sent as RGB,
  and the reason is printed once at startup.

The three forms differ in how portable they are:

| Form | Portability |
|---|---|
| `button` | Works on any board. |
| `index` | The control is portable. The light it picks depends on that board's Lights page. |
| `range` | Tied to one board. |

Cabinet lights are mostly single-colour, so the game reports an intensity
from 0.0 to 1.0 and the profile supplies the colour. Two lights mapped to the
same control add together and clamp.

## Options

| Option | Effect |
|---|---|
| `--port N` | The port passed to spice2x's `-api`. 1337 is the usual choice. spice2x has no default. |
| `--password PASS` | Matches spice2x's `-apipass`. Required where one is set, since the traffic is then encrypted. |
| `--host ADDR` | Where spice2x listens, default `127.0.0.1`. Needed only when the game runs on another machine. |
| `--overlay` | Colour only the mapped controls and leave the rest animating. Without it the whole frame is taken and unmapped LEDs are off. |
| `--full-brightness` | Ignore the board's configured brightness and render the colours the game asks for. |
| `--fps N` | Poll rate. The default is the board's render rate, up to 120. Where the board states none it is 60. |
| `--list-boards` | List the attached boards by factory ID, label, protocol version and firmware, then exit. Takes no board over and needs no game. |
| `--board-id PREFIX` | Pick one of several connected boards by the prefix of its factory ID. `--list-boards` shows the IDs. |
| `--dry-run` | Print frames and drive no board. The HID interface is not opened. |
| `--timeout MS` | Keepalive timeout, 100 to 10000 ms, default 2000. The board restores its animations when it hears nothing from the host for this long. |
| `--force` | Take the board's lights over from another application that holds them. Without it the bridge stops. See [Sharing a board](#sharing-a-board). |

`--list-lights`, `--write-profile` and `--dry-run` talk to spice2x alone, so a
profile can be built and checked on the game PC with no board attached. A dry
run has no board to resolve an `index` entry against, so it shows those
entries as the profile wrote them, such as `Up[1]`. `--list-boards` talks to
the boards alone.

## Notes

Frames are sent only when something changes. While nothing does, the bridge
renews the claim every third of the keepalive timeout, which holds the
takeover.

The bridge streams at the board's render rate, up to 120 frames per second.

In overlay mode the unmapped LEDs show the board's own animations. While those
animations are idle, after the board's LED idle timeout, the unmapped LEDs are
off. A press of a lit control wakes them.

The bridge does not use spice2x's built-in HID lights feature. That feature
expects a device declaring its lights in its HID descriptor, which cannot
describe a board whose LED map the user configures.

## Tests

```
pip install -e ".[dev]"
pytest
```

The suite needs no board. It runs the bridge against gp2040ce-hostlighting's
model board, which answers as the firmware does and keeps the protocol's
timing on a clock the tests control.

Some checks need a board. They show what a board reports about itself,
whether each light is addressed correctly, whether the staging order holds,
and whether the whole program works from its own entry point.

```
python -m tests.hardware_probe        # read-only, safe to run mid-game
python -m tests.hardware_lights        # eleven phases, watched by eye
```

[tests/README.md](tests/README.md) describes each test file and how to run
it.

## Contributing profiles

Profiles for real games are welcome. Write one with `--write-profile`, fill
it in, check it with `--dry-run`, and open a pull request adding it to
`profiles/`. State the game and version it was captured from.

## Changelog

[CHANGELOG.md](CHANGELOG.md) lists what changed in each release and anything
that needs action when upgrading.

## Licence

GPL-3.0-or-later. See [LICENSE](LICENSE).

The protocol layer is [gp2040ce-hostlighting][hostlighting], a separate
package, also GPL-3.0-or-later. `hlp_spice2x/hlp.py` holds the bridge's own
part: the control names, what it requires of a board and its messages.

[gp2040-ce]: https://github.com/OpenStickCommunity/GP2040-CE
[spice2x]: https://github.com/spice2x/spice2x.github.io
[v0.3.0]: https://github.com/djGLiTCH/hlp-spice2x/releases/tag/v0.3.0
[hostlighting]: https://github.com/djGLiTCH/gp2040ce-hostlighting
[spec]: https://github.com/djGLiTCH/GP2040-CE/blob/HLP_v2.0-rc9/docs/host-lighting.md
[pr-1691]: https://github.com/OpenStickCommunity/GP2040-CE/pull/1691
[rc9]: https://github.com/djGLiTCH/GP2040-CE/releases/tag/HLP_v2.0-rc9
