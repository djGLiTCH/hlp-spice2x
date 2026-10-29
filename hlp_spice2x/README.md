<!--
hlp-spice2x (spice2x to GP2040-CE Host Lighting bridge)
Filename: README.md
Folder: hlp_spice2x
https://github.com/djGLiTCH/hlp-spice2x
SPDX-FileCopyrightText: (C) 2026 Jacob Simpson (DJ GLiTCH)
SPDX-License-Identifier: GPL-3.0-or-later
See LICENSES/GPL-3.0-or-later.txt at the repository root.
-->

# The package

This guide describes the five modules, for a reader changing the bridge or
writing another host on gp2040ce-hostlighting.

```
spice2x  --(Spice API, TCP/JSON)-->  hlp-spice2x  --(HID)-->  GP2040-CE board
             spiceapi.py                bridge.py   gp2040ce-hostlighting
                                        profile.py
                                        hlp.py
```

| Module | What it is |
|---|---|
| `hlp.py` | What the bridge needs of a board: the control names a profile uses, the capabilities it requires, its view of what the board offers, and its messages for finding a board. |
| `bridge.py` | The loop, on gp2040ce-hostlighting's `Session`. It polls the game, resolves a frame, stages and publishes it, and follows the board's state. |
| `profile.py` | Profiles. It turns a JSON file into targets and colours, and a set of light levels into a frame. |
| `spiceapi.py` | The game side, a minimal Spice API client with the RC4 the password option needs. |
| `__main__.py` | The command line. It parses arguments, starts the debug log `-d` asks for, opens both ends, and passes them to the loop. |

Dependencies run one way. `hlp.py` and `spiceapi.py` import no other module
here, `profile.py` imports only `hlp.py`, and `bridge.py` imports `hlp.py` and
`profile.py`. `hlp.py`, `bridge.py` and `__main__.py` import
gp2040ce-hostlighting. Nothing imports `__main__.py`.

## Reading order

Start with `hlp.py`. Its module docstring lists the pages the bridge reads.
Its parts, in the order a host uses them:

- `open_device` and `describe_boards` find and list boards through the
  library, in the bridge's own words when no board can be chosen.
- `REQUIRED` and `requirements` name what the bridge needs a board to list in
  `HELLO`, with the addressing modes a profile stages by.
- `FIRMWARE_NAMES` gives the name `proto/enums.proto` gives each short
  name's value. `name_index` and `resolve_control` look a control up on a
  board's page 3, and `control_name` names one as a profile would, by page 3
  once it is read.
- `HostLightingCapabilities` answers call sites from a `ConfigurationCache`,
  the session's or its own. `lights_of` gives the lights `STAGE` reaches
  through a control, and `lacks_control` says whether page 10 shows the board
  has no such control.
- `negotiate` is the handshake the hardware checks use: `HELLO`, the
  capability check and the pages once. The run makes it through `Session`.

Then `bridge.py`:

- `start_board` resolves the profile through the board's page 3, with
  `profile.resolve`, at connect and when the board returns.
- `staging_for` decides how each of the profile's targets reaches the board's
  lights, at connect and again when the board's configuration changes. A
  control name becomes an entry by action or kind, and an `index` becomes a
  light ordinal.
- `frame_for` turns a resolved frame into the library's `Frame`, pass by pass.
  The order of the passes must be kept, as its docstring describes.
  `send_frame` publishes one outside a `Session`.
- `run` is the loop. The `Session` keeps the claim, the events, the pages read
  again, the frames and the reconnect. The bridge resolves the profile again
  when the board's configuration changes, and reports what happens.

`profile.py` and `spiceapi.py` are small and self-contained.

## Concepts

### Targets

A target is not a light. `('button', (6, 5))` names a control by the
addressing mode and address `STAGE` reaches it by, here action 5, B1. The
board resolves it. `('light', (6, 1), 1)` names one light of a control by its
position in the board's Lights page. A name the bridge has no value for is
`('button', ('firmware', 'BUTTON_PRESS_E13'))` until it is resolved on a
board. `('range', 16, 30)` names raw LED
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
that has not seen the change cannot stage over the new holder's frame. The
library's `LightsClaim` keeps the claim for the `Session`.

### The staged frame

The staged frame lasts as long as the takeover. The board keeps staged pixels
until they are overwritten, so a target that leaves the frame is cleared.
When the takeover ends or the holder changes, the board clears the staged
frame, and the bridge stages the whole frame again.

## The library

The protocol layer is gp2040ce-hostlighting. Its README describes the device,
the claim, the frames, the cache and the `Session`, and the failures a
`Session` recovers from.

## Testing a change

`tests/README.md` covers the suite and the checks that need a board. `pytest`
tests a change against the library's model board, and
`python -m tests.hardware_lights` confirms it on a real one.
