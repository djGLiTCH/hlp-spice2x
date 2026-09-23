# Changelog

Notable changes to this project, newest first.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
versions follow [semantic versioning](https://semver.org/spec/v2.0.0.html). While
the major version is 0, a minor bump is where breaking changes go.

## [0.3.0] - 2026-09-23

Host Lighting Protocol v1.4 support. Additive: nothing that worked under 0.2.0
changes.

### Added

- Protocol v1.4: the board's control table (page 6) is read at connect and
  whenever the LED map changes. A mapped control the board does not have is now
  reported as not on this board, rather than as having no LED. Page 6 is
  advisory: if it cannot be read, the bridge behaves as it did at v1.3.
- `hardware_probe` prints the control table, checks it against its own counts
  and against the light table's pins, and exits non-zero on a mismatch.
- `--list-boards`, listing each attached board's factory ID, label, protocol
  version and firmware without taking any over.

### Changed

- A board reporting v1.4 is driven as v1.4 rather than clamped to v1.3.
- With several boards attached and no `--board-id`, the error names each
  board by label as well as by ID.
- Firmware links point at the HLP v1.4 test builds, and protocol links at the
  v1.4 specification by tag rather than at a branch that moves.

### Fixed

- `hlp_spice2x.__version__` still said 0.1.0 in 0.2.0.
- `hardware_sweep` and `hardware_precedence` took the board over without
  clearing its staging buffer, so a pixel staged by an earlier session or phase
  stayed lit. They now clear on every takeover, as the bridge does.

## [0.2.0] - 2026-08-18

Multi-version Host Lighting support: the bridge speaks v1.0 through v1.3, works
out which one the attached board offers at connect, and uses what that version
gives it. One profile and one command line work across all of them.

### Breaking

- A colour must now be exactly six hex digits, or eight for `RRGGBBWW`. A `0x`
  prefix, underscores, surrounding whitespace and any other length are refused.
  `"0xFF0000"` is the one to look for: it did not fail before, it quietly lit
  blue. Telling a six-digit colour from an eight-digit one is only safe once the
  length means something.
- A `range` key holding nothing usable (`[]`, `0`, `null`, `""`) is now an error
  rather than a silently dropped light.
- `--fps` no longer defaults to 60. It follows the board's own render rate where
  that is lower, so a board rendering at 40 Hz is streamed at 40. Passing `--fps`
  still overrides it.

### Added

- Protocol v1.1: the board's light table is read instead of targets being
  discovered by writing to it, the stream rate follows the board's reported
  render rate, and **every** light of a control is lit where the board says a
  control owns more than one.
- Protocol v1.2: an `index` key in profiles, picking out one light of a control.
  `{ "button": "UP", "index": 1 }` is the second Up button on a layout with two.
- Protocol v1.3: the white channel, through an eight-digit `RRGGBBWW` colour on a
  GRBW or RGBW chain. The board's per-entry outcome mask is read, so a light that
  has moved is noticed and the light table re-read.
- Extended control names `A3`, `A4` and `E1`-`E12`, on boards whose firmware
  reports them.
- `--force`, to run against a protocol major version this bridge does not
  support. Unsupported, and for testing only.
- Hardware checks in `tests/`: a read-only probe of what a board reports, a
  functional sweep, a staging-order check, and a driver that runs the whole
  program against a fake Spice API. Each adapts to whatever board is attached.
- `tests/README.md`, explaining what every test file is for and when to use it.

### Changed

- Capabilities are negotiated once at connect and then frozen; call sites ask
  what the board can do rather than comparing version numbers.
- A board reporting a newer minor version than this bridge knows is driven as the
  newest it does know rather than refused, since within a major version the
  protocol only ever adds.
- A board reporting a different major version is refused, with `--force` to
  override.
- Target discovery issues no writes at all on a board that publishes a light
  table.
- The vendored protocol client grew from 314 to 1046 lines, gaining the page 5
  light table, the full page 1 and page 2 decodes, and the `SET_RANGE_RGBW`,
  `SET_LIGHT` and `SET_LIGHT_RGBW` commands.

### Fixed

- A board reporting protocol major version 2 or higher was accepted and then
  driven with v1.0 assumptions. The version check was a lower bound, so anything
  newer than v1.0 passed it.
- The staging buffer was not cleared at startup on any board with a light table,
  which is every board from v1.1 onward. Pixels left staged by a previous session
  were republished by every frame and never overwritten.
- A dropped or refused reply to the bridge's own housekeeping ended the run. The
  five-second LED-map poll, the keepalive and the per-light staging receipts are
  all survivable now. A board that has gone away still ends the run.
- Raw ranges were bounded by the firmware's buffer size rather than the board's
  actual LED count, so a profile written for a larger board staged silently and
  lit nothing beyond the end. Now reported at connect.
- A profile that stopped fitting the board mid-session ended the run. The entries
  that no longer fit are held back and named instead.
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
Host Lighting Protocol. Profiles name controls rather than LED indexes, so the
firmware resolves them against each board's own LED map and one profile works
across layouts.

[0.3.0]: https://github.com/djGLiTCH/hlp-spice2x/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/djGLiTCH/hlp-spice2x/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/djGLiTCH/hlp-spice2x/releases/tag/v0.1.0
