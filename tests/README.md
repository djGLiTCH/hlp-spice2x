# Tests

This directory holds automated tests and hardware checks.

The automated tests run under pytest and need no hardware. They show that the
code does what it is meant to.

The hardware checks need a board attached and take its lighting over while
they run. They show that a board behaves as the protocol says. Run them after
a change to staging, against a board that behaves unexpectedly, or to see
what a board reports when implementing Host Lighting support in another host.

## Running the automated tests

```
pip install -e ".[dev]"
pytest
```

The automated suite does not touch USB, so it can run with a board attached
and a game running.

The hardware checks are named `hardware_*.py`, so pytest does not collect
them, and importing one runs nothing.

## The automated tests

| File | What it covers |
|---|---|
| `test_hlp_decoders.py` | The framing, the `HELLO` and page decoders and the `STAGE` encoders, as pure functions over 64-byte reports built by hand. The fixtures are the protocol reference's worked examples. |
| `test_capabilities.py` | What the bridge agrees to at connect: the major version, each capability it needs and each it can do without, the minor version gating nothing, and reading again only the pages whose change counter moved. |
| `test_staging.py` | How a frame reaches the board's lights: control names by action, the case strip by kind, one light by index, raw ranges, the white channel, the order the passes are emitted in, and which report is answered. Also the startup check that names controls the board lacks or cannot light. |
| `test_loop_resilience.py` | The run loop against a board that changes, is shared, or stops answering: the claim and its renewal, another application taking the lights over and giving them up, a lapsed claim, a takeover that ended, a configuration change, an unacknowledged frame, and a board that leaves the bus and returns. |
| `test_profile.py` | Profile parsing and frame resolution. |
| `test_cli.py` | Argument handling, `--list-boards`, and the paths that exit before connecting. |
| `test_spiceapi.py` | The Spice API client against the stub server: one RC4 keystream per connection, NUL framing across repeated calls, and a game that closes the connection raising a ConnectionError, which the bridge treats as a normal ending. |
| `test_device_errors.py` | The transport: telling a late reply from a device that has gone away, telling a reply from another application's, keeping an event that arrives ahead of a reply, the claim generation on every request, and finding the right board when several are attached. |
| `test_hardware_checks.py` | The hardware checks below, run against the fake board: the probe and its table checks, the whole lights check on a clock that does not wait, and the profile the end-to-end check writes. |

## The fake board

`fake_board.py` is a fake board parameterised by what the board offers. It
sits where hidapi sits, so requests travel through the real transport, with
the same framing, sequence handling and reply matching that runs against real
hardware.

Hardware and firmware are described separately. `lights` and `controls`
describe what the board has wired, and the capability sets are what its
firmware lists in `HELLO`:

```python
from tests.fake_board import ALL_EVENTS, FakeBoard, M_ULTRA_CONTROLS, M_ULTRA_LIGHTS

board = FakeBoard(lights=M_ULTRA_LIGHTS, controls=M_ULTRA_CONTROLS, events=ALL_EVENTS - {0})
device = board.open()          # a real HostLightingDevice driving the fake
```

It answers by those sets as firmware does. A command it does not list is
status 1, and a page or addressing mode it does not list is an invalid
argument.

It keeps the state the protocol gives a board:

- the staged frame and every frame published (`board.frames`)
- the takeover
- the claim's holder and generation
- the `CONFIGURE` settings and the subscription
- a state token with a change counter for each configuration page

A request under an outdated generation is refused with status 4, and a change
of holder clears the staged frame and resets the settings.

It keeps no clock. A lease lapses when a test says so:

```python
board.take_over(name='MESH')   # another application takes the claim
board.let_go()                 # the claim ends
board.end_takeover()           # the takeover's keepalive runs out
board.reconfigure(controls=...)  # a profile switch, which changes the state token
```

It can also fail on request, which the resilience tests use:

```python
board.drop.add(hlp.CMD_GET_PAGE)     # leave that command unanswered
board.reject.add(hlp.CMD_STAGE)      # answer it with a status other than 0
board.hooks.append(callback)         # run something before each request is answered
```

`attach(monkeypatch, *boards)` makes discovery find several fake boards, for
the tests that pick one of several. `ScriptedConnection` is a spice2x
connection that answers a scripted run of polls, each of which can act on the
board first, then ends the run as Ctrl-C does.

`M_ULTRA_LIGHTS` is the Lights page of a Haute42 COSMOX M Ultra, with 46
lights. Two controls have two lights each, and the case strip has 30.
`M_ULTRA_CONTROLS` is the same board's Controls page: 21 pins, Up and L3 on
two pins each, a turbo pin, and four controls wired but unlit.

The fake follows `FakeBoard` and `ModelBoard` in gp2040ce-binary-tools
(`tests/hostlighting_fakes.py`), the reference host's own fakes.

`stub_server.py` is the fake for the game side. It speaks the Spice API wire
format over a loopback socket, with a hook for animating its lights.

## The hardware checks

Each takes an optional board-ID prefix, for use with several boards attached.
`hlp-spice2x --list-boards` shows each board's ID. A check that lights the
board claims its lights first and stops if another application holds them. It
gives the claim up when it finishes, including on failure, which restores the
board's own animations.

The reference tools in [gp2040ce-binary-tools][binary-tools] complement
these. `hlp-conformance` shows that a board's firmware follows the protocol,
`hlp-caps` prints every page it has, and `hlp-fill --exclusive` or
`hlp-stream` is a second application to try the bridge against.

### hardware_probe.py

```
python -m tests.hardware_probe
```

The probe is read-only. It sends `HELLO` and `GET_PAGE` and nothing else, and
it claims nothing and stages nothing, so it can run during a game.

It prints, decoded by the bridge's own code:

- the protocol version and the capabilities `HELLO` lists
- the board's identity
- its state, including the holder
- the Summary
- the lights, grouped by control, with a note against any control that has
  more than one
- the controls, one per line

It checks the two tables against the Summary's counts and against each other,
and exits non-zero if a check fails.

Run it first against any board, and first when the bridge behaves
unexpectedly.

### hardware_lights.py

```
python -m tests.hardware_lights
```

The lights check has eleven phases and takes about a hundred seconds. Each
phase announces what should be visible before it starts. Run it after
changing anything that touches staging.

Every control it uses comes from what the board reports. A phase with nothing
to show on a given board says so and is skipped.

Phase 2 lights every light on the Lights page one at a time, by ordinal. A
control with two lights shows them separately, and an ordinal addressing the
wrong LED shows as a light out of sequence.

Phase 7 shows the staging order in four still frames on one control that has
two lights. An entry naming one light wins over the control's name, including
when it asks for black.

The lights check also reports an unacknowledged or refused frame and a
takeover that did not last its keepalive, and exits non-zero if there was any.

### hardware_bridge.py

```
python -m tests.hardware_bridge
```

This check runs the whole program. It stands up a fake Spice API on a
loopback port, writes a profile to disk, and invokes the command line entry
point as a shell would, for 30 s, ending with a real interrupt. Argument
parsing, profile loading, discovery, the handshake, the claim and its
renewal, the polling loop, the watch on the board's state and the shutdown
are all the real ones.

The profile it writes is generated from what the board reports and is left on
disk as an example for that board. It uses control names and per-light
indexes, and no raw ranges.

Two things to watch:

- Whether the loop keeps up. The check reports the rate achieved and how many
  frames went unacknowledged.
- Whether anything survives between runs. On a second run straight after the
  first, a light holding a colour from the previous run in the first second
  is a pixel that was not staged.

## Which to run

| Situation | Run |
|---|---|
| Any change to the code | `pytest` |
| A new board | `python -m tests.hardware_probe` |
| A change to how frames are staged, or to the order of the passes | `python -m tests.hardware_lights` |
| A change to the run loop, the claim handling, the command line or profile loading | `python -m tests.hardware_bridge`, twice |
| A board behaving unexpectedly | `hardware_probe` first, then the check closest to the behaviour |

A board is not required to contribute. The automated suite runs the bridge
and the hardware checks against the fake board, and a change is pinned there
first. The hardware checks confirm behaviour on a real board.

[binary-tools]: https://github.com/OpenStickCommunity/gp2040ce-binary-tools
