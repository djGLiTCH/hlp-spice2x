<!--
hlp-spice2x (spice2x to GP2040-CE Host Lighting bridge)
Filename: CHANGELOG.md
Folder: (repository root)
https://github.com/djGLiTCH/hlp-spice2x
SPDX-FileCopyrightText: (C) 2026 Jacob Simpson (DJ GLiTCH)
SPDX-License-Identifier: GPL-3.0-or-later
See LICENSES/GPL-3.0-or-later.txt at the repository root.
-->

# Changelog

Notable changes to this project, newest first.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
versions follow [semantic versioning](https://semver.org/spec/v2.0.0.html). While
the major version is 0, a minor bump is where breaking changes go.

## [0.6.0] - 2026-10-28

This version speaks HLP v2.0 release candidate 10 (`HLP_v2.0-rc10`). It is a
pre-release.

### Breaking

- Boards on HLP v2.0 rc9 and earlier are not driven. Release candidate 10
  moves the Lights page from page 11 to page 12, and page 1's State group grows
  from 9 to 10 bytes. On such a board the bridge stops and names the page the
  firmware lays out differently. Update the board to an HLP v2.0 rc10 build.

### Changed

- gp2040ce-hostlighting 1.0.0rc2 speaks release candidate 10. pip installs it
  with the bridge from its `v1.0.0rc2` tag on GitHub until 1.0.0 is on PyPI,
  with HLP v2.0 final.
- The bridge's messages, the README and `hardware_probe` name the Lights page
  as page 12.
- Firmware and protocol links point at the `HLP_v2.0-rc10` tag.
- Each file opens with the header gp2040ce-hostlighting uses, naming the
  project, the file and its folder, the repository, the copyright and the
  licence. The licence moves to `LICENSES/GPL-3.0-or-later.txt` as the FSF's
  plain text, and `REUSE.toml` covers the example profile.

## [0.5.0] - 2026-10-26

This version is built on [gp2040ce-hostlighting][hostlighting], the reference
host for the Host Lighting Protocol. It is a pre-release, built against the
protocol's release candidate (`HLP_v2.0-rc9`).

### Breaking

- Python 3.14 or later is required. Python 3.9 to 3.13 are no longer
  supported.
- Boards on HLP v2.0 rc7 and earlier are not driven. The Summary (page 8) has
  grown from 28 to 30 bytes. Update the board to an HLP v2.0 rc9 build.

### Changed

- The protocol, the claim, the frames and the configuration pages come from
  gp2040ce-hostlighting, which pip installs with the bridge. hidapi comes
  through it.
- pip installs gp2040ce-hostlighting from its `v1.0.0rc1` tag on GitHub, which
  needs git. The library moves to PyPI at 1.0.0, with HLP v2.0 final.
- Whether a chain has a white emitter is the Summary's white channel flag. It
  was taken from the colour format.
- The first frame after each start, and after the board returns, clears the
  staged frame first.
- Page 1 is read at least once a second, with events or without, so a change
  whose event was lost is still seen. Without events it was read every 5 s.
- A configuration page read again after a change that goes unanswered is read
  at the next poll, without a warning. One the board refuses is warned of with
  its cause and tried again after 1 s.
- A give up that goes unanswered at the end of a run is warned of. The
  keepalive timeout restores the animations.
- The lines for the claim are the library's: "took the lights over" and "the
  lights are unclaimed again, so the claim was taken".
- A board of another protocol major, or one that does not list what the
  bridge needs, is refused in the library's words.

### Fixed

- On HLP v2.0 rc9 builds, the Summary was misread. A change to the
  board's lights or controls mid-run went unnoticed, and `hardware_probe`
  reported a mismatch.
- A refusal (status 4) another application received is no longer taken as the
  bridge's own. A refusal is the bridge's only when the holder and generation
  it shows would refuse the bridge's request.
- An interface that is found but does not answer as a board is reported as
  such. It was reported as no board matching an empty ID prefix.

### Removed

- The README's note on the first-boot fault of HLP v2.0 release candidates 1
  to 4.
- `tests/fake_board.py`. The tests run on gp2040ce-hostlighting's model board.

## [0.4.0] - 2026-10-13

This version speaks Host Lighting Protocol 2.0 only. It is a pre-release,
built against the protocol's release candidate (`HLP_v2.0-rc7`).

### Breaking

- Boards on the pre-release 1.x protocol (v1.0 to v1.4) are not driven.
  Update the board to an HLP 2.0 build, or stay on hlp-spice2x 0.3.0. A 1.x
  board is not found by its USB usage, and the bridge reports when one is
  attached.
- `--force` takes the board's lights over from another application that holds
  them. It no longer overrides the protocol version check.
- `--timeout` accepts 100 to 10000 ms, the range the firmware clamps to. It
  accepted up to 65535.
- A `range` is no longer refused at load for reaching past LED 99. The
  board's addressable limit is read from the board, and the part of a range
  past it is not sent.
- `TURBO`, `CASE` and `PLED1`-`PLED4` reach every light of their kind. On 1.x
  firmware `TURBO` and a player LED lit the first such light only.

### Added

- The claim on the board's lights. The bridge claims them as `hlp-spice2x`,
  stops if another application holds them, and renews the claim while it
  runs. If another application takes the lights over, the bridge waits and
  claims them again when the board is unclaimed.
- State changes by event. Where the board offers `SUBSCRIBE`, the bridge acts
  on a change of the board's configuration, of the holder or of the takeover
  when the event arrives. Otherwise it reads page 1 every 5 s.
- A frame the board dropped is staged again. When the takeover ends while the
  bridge holds the claim, the frame is staged again without waiting for the
  game to change a light.
- A board that leaves the bus is waited for. When another application or the
  web configurator reboots the board, the bridge reports it, looks for the
  same board once a second, and starts again on it when it returns. A board
  that stops answering on a static game screen is treated the same way.
- `--list-boards` and the no-board error report a board speaking another
  protocol major.
- `tests/test_hardware_checks.py` runs the hardware checks against the fake
  board.

### Changed

- Profiles are unchanged, and every control name maps to the same control.
  Names are staged by GP2040-CE's own action and light-kind values, which the
  firmware resolves to every light carrying them.
- The board is gated on the capabilities `HELLO` lists, never on its minor
  version.
- A frame has one answered report. Every report but the last is sent without
  a reply, and the last carries the commit. An unacknowledged frame is sent
  once more, then staged whole on the next change. The board is treated as
  lost only if it does not answer `HELLO` either.
- The default stream rate follows the board's render rate up to 120 frames
  per second. The limit was 60.
- The run ends by giving the claim up, which restores the board's animations.
  `RELEASE` is not sent.
- `hardware_probe` and the lights check are rewritten for 2.0. The lights
  check is now `python -m tests.hardware_lights`, and it claims the lights
  as the bridge does.
- Firmware and protocol links point at the `HLP_v2.0-rc7` tag.

### Removed

- `hardware_precedence`. Its four steps are phase 7 of `hardware_lights`.
- The startup probe of each control, the wait for a light table that had not
  been published, and the check of which lights the board skipped. HLP 2.0
  reports its lights completely from the start and announces every change.

## [0.3.0] - 2026-09-23

Host Lighting Protocol v1.4 support. Nothing that worked under 0.2.0 changes.

### Added

- The board's control table (page 6, protocol v1.4) is read at connect and
  whenever the LED map changes. A mapped control the board does not have is
  reported as not on this board. It was reported as having no LED. Page 6 is
  advisory. If it cannot be read, the bridge behaves as it did at v1.3.
- `hardware_probe` prints the control table, checks it against its own counts
  and against the light table's pins, and exits non-zero on a mismatch.
- `--list-boards`, listing each attached board's factory ID, label, protocol
  version and firmware without taking any over.

### Changed

- A board reporting v1.4 is driven as v1.4. It was clamped to v1.3.
- With several boards attached and no `--board-id`, the error names each
  board by label as well as by ID.
- Firmware links point at the HLP v1.4 test builds, and protocol links at the
  v1.4 specification by tag. They pointed at a branch that moves.

### Fixed

- `hlp_spice2x.__version__` still said 0.1.0 in 0.2.0.
- `hardware_lights` and `hardware_precedence` took the board over without
  clearing the staged frame, so a pixel staged by an earlier session or phase
  stayed lit. They now clear on every takeover, as the bridge does.

## [0.2.0] - 2026-08-18

Multi-version Host Lighting support. The bridge speaks v1.0 to v1.3, works out
at connect which one the attached board offers, and uses what that version
gives it. One profile and one command line work across all of them.

### Breaking

- A colour must be six hex digits, or eight for `RRGGBBWW`. A `0x` prefix,
  underscores, surrounding whitespace and any other length are refused.
  `"0xFF0000"` did not fail before. It lit blue.
- A `range` key holding nothing usable (`[]`, `0`, `null`, `""`) is an error.
  The light was dropped without a message.
- `--fps` no longer defaults to 60. It follows the board's own render rate where
  that is lower, so a board rendering at 40 Hz is streamed at 40. Passing `--fps`
  still overrides it.

### Added

- The board's light table (protocol v1.1) is read. Targets were found by
  writing to the board. The stream rate follows the board's reported render
  rate, and every light of a control is lit where the board says a control
  owns more than one.
- An `index` key in profiles (protocol v1.2) picks one light of a control.
  `{ "button": "UP", "index": 1 }` is the second Up button on a layout with two.
- The white channel (protocol v1.3), through an eight-digit `RRGGBBWW` colour
  on a GRBW or RGBW chain. The board's per-entry outcome mask is read, so a light
  that has moved is noticed and the light table read again.
- Extended control names `A3`, `A4` and `E1`-`E12`, on boards whose firmware
  reports them.
- `--force`, to run against a protocol major version this bridge does not
  support. Unsupported, and for testing only.
- Hardware checks in `tests/`: a read-only probe of what a board reports, a
  functional lights check, a staging-order check, and a driver that runs the
  whole program against a fake Spice API. Each adapts to whatever board is
  attached.
- `tests/README.md`, explaining what every test file is for and when to use it.

### Changed

- Capabilities are negotiated once at connect and then kept. Call sites ask
  what the board can do, and compare no version numbers.
- A board reporting a newer minor version than this bridge knows is driven as
  the newest it knows. It was refused.
- A board reporting a different major version is refused, with `--force` to
  override.
- Target discovery sends no writes on a board that publishes a light table.
- The vendored protocol client grew from 314 to 1046 lines, gaining the page 5
  light table, the full page 1 and page 2 decodes, and the `SET_RANGE_RGBW`,
  `SET_LIGHT` and `SET_LIGHT_RGBW` commands.

### Fixed

- A board reporting protocol major version 2 or higher was accepted and then
  driven with v1.0 assumptions. The version check was a lower bound, so anything
  newer than v1.0 passed it.
- The staged frame was not cleared at startup on any board with a light table,
  which is every board from v1.1 onward. Pixels left staged by a previous
  session were published again by every frame and never overwritten.
- A dropped or refused reply to the bridge's own housekeeping ended the run. The
  run now survives one to the five-second LED-map poll, the keepalive and the
  per-light staging receipts. A board that has gone away still ends the run.
- Raw ranges were bounded by the firmware's buffer size. A profile written for
  a larger board staged past the board's last LED without a message and lit
  nothing there. Such a range is reported at connect.
- A profile that stopped fitting the board mid-session ended the run. The
  entries that no longer fit are held back and named.
- Whether a light uses the white channel no longer depends on the order entries
  are listed in the profile.
- Several help strings, docstrings and documented defaults described behaviour
  the code did not have.

### Notes

Each protocol version was verified against a Haute42 COSMOX M Ultra Gen 2 by
flashing that firmware and running the same checks: negotiation and refusal at
every version, the light table, expansion by raw range on v1.1 and by ordinal
from v1.2, the `index` syntax end to end, the outcome mask zero-filled on real
v1.2 firmware and populated on v1.3, and the whole program run from its own entry
point at 40 fps with no unacknowledged commits.

The white channel is covered by tests only. The reference board's chain is GRB,
so it has no white emitter to drive.

## [0.1.0] - 2026-08-12

First tagged release. Polls spice2x's cabinet lighting API, maps named lights to
controls through a JSON profile, and streams frames to a GP2040-CE board over the
Host Lighting Protocol. Profiles name controls. The firmware resolves them
against each board's own LED map, so one profile works across layouts.

[hostlighting]: https://github.com/djGLiTCH/gp2040ce-hostlighting
[0.6.0]: https://github.com/djGLiTCH/hlp-spice2x/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/djGLiTCH/hlp-spice2x/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/djGLiTCH/hlp-spice2x/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/djGLiTCH/hlp-spice2x/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/djGLiTCH/hlp-spice2x/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/djGLiTCH/hlp-spice2x/releases/tag/v0.1.0
