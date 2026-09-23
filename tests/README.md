# Tests

Two kinds of thing live here, and they answer different questions.

The **automated tests** run under pytest, need no hardware, and answer "does the
code still do what it is supposed to". Run them constantly.

The **hardware checks** need a board plugged in, take its lighting over while
they run, and answer "does this board actually behave the way the protocol says".
Run them when you change staging, when you meet a board that behaves oddly, or
when you are implementing Host Lighting support of your own and want to see what
a board really reports.

## Running the automated tests

```
pip install -e ".[dev]"
pytest
```

Nothing in the automated suite touches USB, so it is safe to run with a board
attached and a game running.

The hardware checks are named `hardware_*.py` rather than `test_*.py`, so pytest
never collects them, and importing one does nothing until you call it.

## The automated tests

| File | What it covers |
|---|---|
| `test_hlp_decoders.py` | The capability page decoders and the per-light staging commands, as pure functions over hand-built 64-byte replies. Hand-built on purpose: decoding a reply the fake board generated would test the fake as much as the decoder. Also the probe's control-table checks, and the whole probe run against the fake for its exit code. |
| `test_version_policy.py` | What the bridge agrees to at connect across v1.0 to v1.4, the clamping of an unknown minor, the refusal of an unknown major, and `--force`. Also that a page 6 read which fails costs neither the connect nor the light table. |
| `test_staging.py` | How a frame reaches the board's lights: control names, the expansion to all of a control's lights, per-light targeting by index, raw ranges, the white channel, and the order the passes are emitted in. Also the startup check that names controls the board lacks or cannot light. |
| `test_loop_resilience.py` | That a bad reply does not end a working run, and that a board going away still does. Covers the LED-map poll, the keepalive, the staging receipts read back from v1.3 firmware, and a receipt that arrives truncated rather than not at all. Also that startup clears the staging buffer, so a previous session's pixels are not inherited. |
| `test_profile.py` | Profile parsing and frame resolution. |
| `test_cli.py` | Argument handling, `--list-boards`, and the paths that exit before connecting to anything. |
| `test_spiceapi.py` | The Spice API client against the stub server: one RC4 keystream per connection rather than per message, NUL framing across repeated calls, and a game that hangs up surfacing as a ConnectionError, which the bridge relies on to treat a closed game as a normal ending. |
| `test_device_errors.py` | Telling a late reply apart from a device that has gone away, and finding the right board when several are attached. |

## The fake board

`fake_board.py` is the piece most likely to be useful outside this project.

It is a board fake **parameterised by protocol version**, sitting where hidapi
sits rather than replacing the client, so requests travel through the real
transport: the same framing, sequence handling and reply matching that runs
against real hardware.

Hardware and firmware are described separately, because they are separate. A
board wires the lights it wires whatever firmware it runs, so `lights` and
`controls` describe the hardware and `version` decides how much of it the
firmware will admit to:

```python
from tests.fake_board import FakeBoard, M_ULTRA_LIGHTS

board = FakeBoard(version=(1, 1), lights=M_ULTRA_LIGHTS)
device = board.open()          # a real HostLightingDevice driving the fake
```

It gates its own replies the way firmware does. Page 5 does not exist before
v1.1. The extended controls are named from v1.1 but do not stage by name until
v1.2. `SET_LIGHT_RGBW` does not exist before v1.3. Page 6 does not exist before
v1.4. And the per-entry outcome mask reads as all zeroes on anything older than
v1.3, which is the trap worth being able to reproduce: zeroes mean "every entry
was skipped" to a host that reads them without checking the version first.

It can also misbehave on demand, which is how the resilience tests are written
without a flaky board to hand:

```python
board.drop.add(hlp.CMD_GET_CAPS)     # leave that command unanswered
board.reject.add(hlp.CMD_PING)       # answer it with a non-OK status
```

`attach(monkeypatch, *boards)` makes device discovery find several fake boards,
which is how picking one of several is tested.

`M_ULTRA_LIGHTS` is a real board's light table - a Haute42 COSMOX M Ultra Gen 2,
46 lights, checked record for record against the hardware. It is a useful
fixture because it is awkward in the ways real boards are: two controls own two
lights each, and the case strip owns thirty. `M_ULTRA_CONTROLS` is the same
board's page 6: 21 pins, Up and L3 on two pins each, a turbo pin with no button
ID, and four controls wired but unlit.

`stub_server.py` is the matching fake for the other side, speaking the Spice API
wire format over a loopback socket, with a hook for animating its lights.

## The hardware checks

Each takes an optional board-ID prefix, which only matters with several boards
attached; `hlp-spice2x --list-boards` shows each board's ID. Those that take the
lighting over clear its staging buffer first, and release the board and restore
its own animations when they finish, including on failure.

### `hardware_probe.py` - what does this board say about itself

```
python -m tests.hardware_probe
```

**Read-only.** Sends `PING` and `GET_CAPS` and nothing else, so it never takes
the lighting over and is safe to run mid-game.

Prints the protocol version, the board's identity, and each capability page the
bridge reads, decoded field by field: the light table grouped by the control that owns each
light, with a note against any control that owns more than one, then from v1.4
the control table, one pin per line, marking controls wired to more than one pin.
It checks the control table against its own counts and against the light
table's pins, as `hlp-caps` does, and exits non-zero if either check fails.

Run this first against any board, and first whenever something further up is
behaving strangely: almost every surprising behaviour traces back to something
on one of these pages. The docstring explains what each page means and the two
traps in reading page 5.

### `hardware_precedence.py` - which way of naming a light wins

```
python -m tests.hardware_precedence
```

Four still pictures, five seconds each, on one control the board gives more than
one light. Nothing else is lit and nothing moves inside a step, so each step is
either right or wrong at a glance.

It demonstrates the ordering rule: a control name is staged first, the extra
lights it implies next, and anything the profile asked for explicitly last. The
third step is the one worth watching, because an explicit entry asking for black
must still win - otherwise a profile could never turn one light of a control off.

Getting this order wrong produces a frame that looks right on one of the two
render pipelines and wrong on the other, which is why it is worth checking by
eye on real hardware rather than trusting a unit test.

Skips itself on a board where no control owns more than one light.

### `hardware_sweep.py` - does everything still work

```
python -m tests.hardware_sweep
```

Eleven phases, about a hundred seconds, each announcing what should be visible
before it starts. Run it after changing anything that touches staging.

Every control it uses comes from what the board reports, so phases with nothing
to demonstrate on a given board say so and are skipped.

The second phase is the strongest check here: it lights every light in the table
one at a time, by ordinal, so a control owning two lights shows them separately
and any ordinal addressing the wrong physical LED shows up immediately as a light
appearing out of sequence.

It also reports anything the board itself objected to - an unacknowledged commit,
or an ordinal the board says it skipped - and exits non-zero if there was any, so
it is still worth something when nobody is watching the LEDs.

### `hardware_bridge.py` - does the whole program work

```
python -m tests.hardware_bridge
```

The only check here that does not reach into the bridge. It stands up a fake
Spice API on a loopback port, writes a profile to disk, and invokes the command
line entry point exactly as a shell would, for thirty seconds, ending with a real
interrupt. Argument parsing, profile loading, device discovery, the handshake,
the polling loop, the keepalive, the periodic LED-map check and the shutdown are
all the real ones.

The profile it writes is generated from what the board reports and left behind
on purpose: it is the easiest way to see what a profile looks like for a board
you have. It uses control names and per-light indexes, not raw ranges.

Two things to watch that no number can tell you:

* **Whether the loop keeps up.** It reports the rate achieved and how many
  commits went unacknowledged, but a chase that stutters says more than either.
* **Whether anything survives between runs.** Run it twice in a row and watch the
  second run's first second. A light holding a colour from the previous run is a
  pixel nobody staged, which means the staging buffer was not cleared at startup.
  That is a real bug this project shipped and fixed, and it was found exactly
  this way.

## Which to run when

| Situation | Run |
|---|---|
| Any change to the code | `pytest` |
| A new or unfamiliar board | `python -m tests.hardware_probe` |
| Changed how frames are staged | `python -m tests.hardware_sweep` |
| Changed the order passes are emitted in | `python -m tests.hardware_precedence` |
| Changed the run loop, the CLI, or profile loading | `python -m tests.hardware_bridge`, twice |
| A board behaving in a way you cannot explain | `hardware_probe` first, then the one closest to the behaviour |

A board is not required to contribute. The automated suite covers all five
protocol versions through the fake, and that is where a change should be pinned
first - the hardware checks confirm behaviour, they do not pin it.
