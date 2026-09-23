# The package

Five modules, about 2400 lines. This is a guide to reading them, for anyone
changing the bridge or implementing Host Lighting support of their own.

```
spice2x  --(Spice API, TCP/JSON)-->  hlp-spice2x  --(HID)-->  GP2040-CE board
             spiceapi.py                bridge.py               hlp.py
                                        profile.py
```

| Module | Lines | What it is |
|---|---|---|
| `hlp.py` | 1217 | The protocol. Framing, capability pages, staging commands, and what each version of the protocol offers. Depends on nothing but `hidapi`. |
| `bridge.py` | 747 | The loop. Polls, resolves a frame, stages it, publishes it, and keeps the board's takeover alive. |
| `profile.py` | 198 | Profiles. Turns a JSON file into targets and colours, and a set of light levels into a frame. |
| `spiceapi.py` | 122 | The game side. A minimal Spice API client, including the RC4 the password option needs. |
| `__main__.py` | 152 | The command line. Parses arguments, opens both ends, and hands them to the loop. |

Dependencies run one way. `hlp.py` and `spiceapi.py` know nothing about anything
else here, `profile.py` knows only `hlp.py`, and `bridge.py` knows both. Nothing
imports `__main__.py`.

## Reading it in order

Start with **`hlp.py`**, because everything else is shaped by it. Its module
docstring lists the capability pages and says which the bridge reads. The parts
worth reading first, in the order a host uses them:

- `negotiate` - the handshake. Reads the version and the capability pages once,
  and decides what this board can do. Everything downstream branches on its
  result rather than on a version number.
- `HostLightingCapabilities` - that result. Each property answers one question a
  call site wants to ask. Three of them deliberately do not follow from the
  version alone, and the docstrings say why.
- `decode_state`, `decode_led_map`, `decode_lights`, `decode_controls` - the
  capability pages, field by field. Pure functions over a 64-byte reply.
- `send`, `request`, `request_ok` - the transport. Staging is fire-and-forget for
  throughput; only the commit that publishes a frame is waited on.

Then **`bridge.py`**, which is where the protocol meets the loop:

- `staging_for` - decides how each of the profile's targets reaches the board's
  lights, once, at connect. This is where a control name becomes one entry or
  several, and where an `index` becomes an ordinal.
- `send_frame` and `stage_group` - the passes that put a frame on the wire. The
  order they are emitted in is load-bearing and the docstring says why.
- `run` - the loop itself, and the housekeeping around it: the keepalive, the
  five-second LED-map check, and what happens when any of it goes wrong.

**`profile.py`** and **`spiceapi.py`** are small and self-contained; read them
when you need them.

## Ideas worth having straight

**A target is not a light.** `('button', 4)` names a control and lets the board
resolve it. `('light', 0, 1)` names one light of a control by position in the
board's own table. `('range', 16, 30)` names raw LED indexes. They differ in how
portable they are and in how they fail, which `profiles/README.md` covers.

**A frame is control-level; staging is board-level.** `profile.resolve_frame`
produces targets and colours knowing nothing about the attached board.
`bridge.staging_for` then works out what each target means for *this* board.
Keeping that boundary is what lets one profile work across layouts, and it is why
expansion cannot be baked into profile loading.

**Capability, not version.** Call sites ask
`caps.per_light` rather than `caps.minor >= 2`. A later protocol version should
be one new property here, not a new comparison at every branch.

**The staging buffer outlives a frame, and a session.** The board holds staged
pixels until something overwrites them, so a target that leaves the frame has to
be cleared, and a run has to start from a known state rather than inheriting
whatever the last one left.

## Where the vendored client came from

`hlp.py` began as a trimmed copy of the Host Lighting helpers proposed for
gp2040ce-binary-tools in [PR #12](https://github.com/OpenStickCommunity/gp2040ce-binary-tools/pull/12), vendored so the bridge needs
nothing but `hidapi`. The framing, decoders and class names still follow those
helpers, so a fix in one carries over to the other. The capability layer -
`negotiate` and `HostLightingCapabilities` - and the timeout and disconnect
handling are this project's own, so the module is no longer a drop-in for
`gp2040ce_bintools.hostlighting`.

## Testing a change

`tests/README.md` covers the suite and the checks that need a board. The short
version: `pytest` proves a change against all five protocol versions with no
hardware, and `python -m tests.hardware_sweep` confirms it on a real one.
