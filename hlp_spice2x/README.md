# The package

This guide describes the five modules, for a reader changing the bridge or
implementing Host Lighting support in another host.

```
spice2x  --(Spice API, TCP/JSON)-->  hlp-spice2x  --(HID)-->  GP2040-CE board
             spiceapi.py                bridge.py               hlp.py
                                        profile.py
```

| Module | What it is |
|---|---|
| `hlp.py` | The protocol: framing, reply matching, `HELLO`, the pages, the `STAGE` encoders, and what a board offers. It depends only on `hidapi`. |
| `bridge.py` | The loop. It claims the lights, polls, resolves a frame, stages and publishes it, and follows the board's state. |
| `profile.py` | Profiles. It turns a JSON file into targets and colours, and a set of light levels into a frame. |
| `spiceapi.py` | The game side, a minimal Spice API client with the RC4 the password option needs. |
| `__main__.py` | The command line. It parses arguments, opens both ends, and passes them to the loop. |

Dependencies run one way. `hlp.py` and `spiceapi.py` import no other module
here, `profile.py` imports only `hlp.py`, and `bridge.py` imports `hlp.py` and
`profile.py`. Nothing imports `__main__.py`.

## Reading order

Start with `hlp.py`. Its module docstring lists the pages and says which the
bridge reads. Its parts, in the order a host uses them:

- `find_devices` and `open_device` do discovery by usage page and usage. The
  usage is the protocol's major version, so only boards of this major are
  found. A board is bound by the ID in its `HELLO` reply.
- `negotiate` is the handshake. It confirms the major version, checks
  `HELLO`'s capability bitmaps for what the bridge uses, and reads the pages
  once.
- `HostLightingCapabilities` is the result. Each property answers one question
  from a call site. `refresh` reads the Summary and then only the pages whose
  change counter moved.
- `decode_hello`, `decode_page_header` and `decode_records` decode the
  replies, field group by field group. They are pure functions over a 64-byte
  report.
- `send`, `request`, `drain` and `wait_reply` are the transport. Every
  application with the interface open receives every reply and event. The
  reports already waiting are read before each request, and events are kept.
  A reply is matched by its command, its sequence and, for `GET_PAGE` and
  `CLAIM`, what it echoes.
- `entry_payloads` and `run_payloads` encode `STAGE`, for entries addressed by
  action, kind or light ordinal, and for LED runs.

Then `bridge.py`:

- `LightsClaim` is the claim on the board's lights: take, renew, lose, wait,
  take again, give up. It also sends `CONFIGURE`, since a change of holder
  resets the settings.
- `staging_for` decides how each of the profile's targets reaches the board's
  lights, at connect and again when the board's configuration changes. A
  control name becomes an entry by action or kind, and an `index` becomes a
  light ordinal.
- `send_frame` emits the passes that put a frame on the wire. Their order must
  be kept, as its docstring describes.
- `run` is the loop and its housekeeping: renewing the claim, watching the
  board's state, and handling failures.

`profile.py` and `spiceapi.py` are small and self-contained.

## Concepts

### Targets

A target is not a light. `('button', (6, 5))` names a control by the
addressing mode and address `STAGE` reaches it by, here action 5, B1. The
board resolves it. `('light', (6, 1), 1)` names one light of a control by its
position in the board's Lights page. `('range', 16, 30)` names raw LED
indexes. The three differ in portability and in how they fail, which
`profiles/README.md` covers.

### Frames and staging

A frame is at the level of controls, and staging is at the level of the
board. `profile.resolve_frame` produces targets and colours without knowledge
of the attached board. `bridge.staging_for` then resolves each target for
that board. This boundary lets one profile work across layouts.

### Capabilities

`HELLO` says which commands, pages, request flags, addressing modes, pixel
formats and events a board implements. The bridge checks those. The minor
version is printed and never compared.

### The claim

The claim is a lease, and its generation travels on every request. Only the
holder's own `CLAIM` renews it. When the holder changes, the board steps the
generation and refuses every request still carrying the old one, so a host
that has not seen the change cannot stage over the new holder's frame. One
`CLAIM` answers every case: still held, lapsed and taken again, or taken over
by another application.

### The staged frame

The staged frame lasts as long as the takeover. The board keeps staged pixels
until they are overwritten, so a target that leaves the frame is cleared.
When the takeover ends or the holder changes, the board clears the staged
frame, and the bridge stages the whole frame again.

## Origin of the client

`hlp.py` follows the Host Lighting helpers proposed for gp2040ce-binary-tools
in [PR #12][pr-12], the protocol's reference host. It is kept as its own
module so the bridge needs only `hidapi`. The framing, the reply matching and
the decoders follow those helpers, so a fix in one applies to the other. The
capability layer (`negotiate` and `HostLightingCapabilities`) and the timeout
and disconnect handling are this project's own, so the module is not a
drop-in for `gp2040ce_bintools.hostlighting`.

## Testing a change

`tests/README.md` covers the suite and the checks that need a board. `pytest`
tests a change against a fake board, and `python -m tests.hardware_lights`
confirms it on a real one.

[pr-12]: https://github.com/OpenStickCommunity/gp2040ce-binary-tools/pull/12
