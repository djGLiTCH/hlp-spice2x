<!--
hlp-spice2x (spice2x to GP2040-CE Host Lighting bridge)
Filename: README.md
Folder: simulator
https://github.com/djGLiTCH/hlp-spice2x
SPDX-FileCopyrightText: (C) 2026 Jacob Simpson (DJ GLiTCH)
SPDX-License-Identifier: GPL-3.0-or-later
See LICENSES/GPL-3.0-or-later.txt at the repository root.
-->

# hlp-spice2x simulator

The simulator runs [hlp-spice2x][hlp-spice2x] from end to end against a
GP2040-CE board. hlp-spice2x is the bridge from spice2x to GP2040-CE. It
drives the board's LEDs over the Host Lighting Protocol (HLP). A stand-in for
spice2x takes the place of the game, so no game and no spice2x are needed.
Each hlp-spice2x release is tested with this check.

The stand-in serves the Spice API on a loopback port and animates its lights.
The bridge runs through its own command line for 30 seconds, as it would
beside a game, then gives the board's lights back. While it runs, the board
shows a chase around its buttons. A board with case lights also shows a wave
along them.

The simulator package is the `hlp-spice2x-simulator` zip on each
[release][releases]. It holds this README, the run scripts, the board checks
and the wheels to install. The `simulator` folder of the repository holds the
README and the run scripts.

## Requirements

- A board running GP2040-CE 0.8.0 or later, with the Host Lighting add-on
  enabled in the web configurator. The [GP2040-CE releases][gp2040-ce-releases]
  carry it. While no 0.8.0 or later release is out, the
  [HLP v2.0 builds][hlp-v2.0] carry it for every board, with flashing
  instructions.
- [Python][python] 3.14 or later. On Windows, leave the option to install the
  `py` launcher ticked.
- An internet connection on the first run, so pip can fetch hidapi. The
  bridge and its library come with the package, in `wheels/`.
- On Linux, access to the board's hidraw node. The
  [gp2040ce-hostlighting README][hostlighting] gives a udev rule for it.

## Usage

Unzip the package. On Windows, double-click `run-simulator.bat`, or run it
from a command prompt in the package folder.

On Linux and macOS, run `sh run-simulator.sh` in the package folder.

The first run makes a `.venv` folder in the package folder and installs into
it. Later runs reuse it. Nothing is installed anywhere else. Delete `.venv` to
start again.

The script prints the versions, lists the boards it finds, then runs the
simulator. With more than one board attached, give the start of a board's ID
from that list, for example `run-simulator.bat 4330303` or
`sh run-simulator.sh 4330303`.

## Expected output

A run on a Haute42 COSMOX M Ultra Gen 2 on the HLP v2.0 build printed these
lines:

```
fake Spice API on port 49882
profile written to hardware_bridge_profile.json: 14 chase control(s), 30 strip light(s), one entry on Up[1]
running the real entry point for 30s, then interrupting it

board: Haute42|COSMOX M Ultra (v0.7.12-164-g9435e1e)
board speaks HLP v2.0: 46 lights on 46 LEDs, renders at 40 Hz, GRB chain (white channel: no), state changes by event
takeover: whole frame, 2000 ms keepalive, applying board brightness
controls the board gives more than one light, all of which will be lit: L3, Up
streaming at 40 fps (matching the board's render rate)
1 light(s) not in the profile, ignoring: Unmapped Lamp

1162 frames in 29.1s (40.0/s published), 0 frames unacknowledged
claim given up, on-board animations restored

the entry point returned 0
the fake API answered 1162 polls
```

A good run streams at the board's render rate and leaves no frame
unacknowledged. It ends with the board back on its own animation. The
stand-in reports one light the profile leaves out on purpose, so the
`Unmapped Lamp` line is expected.

The simulator writes `tests/hardware_bridge_profile.json`, a profile that
maps the attached board's own controls. It stays there as an example profile
for that board.

## Board checks

These run from the package folder with the board attached. The Python to use
is `.venv\Scripts\python` on Windows and `.venv/bin/python` on Linux and
macOS. With more than one board attached, add the start of the board's ID.

- `python -m tests.hardware_probe` prints what the board reports and checks
  that it is coherent. It does not take the board over.
- `python -m tests.hardware_lights` steps through every way the bridge lights
  a board, in about 90 seconds, and says what each step should show.

## Use with spice2x

Start spice2x with `-api 1337`, then run
`.venv\Scripts\hlp-spice2x --port 1337 --profile <profile>`. The
[hlp-spice2x README][readme] describes the profile format and the command
line.

To install the bridge outside the package, run `pip install hlp-spice2x`.
It is on [PyPI][pypi].

## Notes

- Nothing here flashes a board or changes its settings. The checks take the
  board's lights over while they run and give them back at the end.
- Run one check at a time, with no other Host Lighting application using the
  board.
- Report problems with the package on the [hlp-spice2x issue
  tracker][issues]. The spice2x project does not support it.

## Contents

- `run-simulator.bat` and `run-simulator.sh` set up `.venv` and run the
  simulator.
- `wheels/` holds the release's hlp-spice2x wheel and the
  gp2040ce-hostlighting wheel it was tested with.
- `tests/` holds the simulator, the stand-in Spice API and the other board
  checks, from the release's tag. The simulator is `hardware_bridge.py` and
  the stand-in is `stub_server.py`.
- `LICENSE` and `LICENSES/` hold the licence text.

## Licence

hlp-spice2x and [gp2040ce-hostlighting][hostlighting] are (C) 2026 Jacob
Simpson (DJ GLiTCH), under GPL-3.0-or-later. The source of both is on
GitHub.

[hlp-spice2x]: https://github.com/djGLiTCH/hlp-spice2x
[releases]: https://github.com/djGLiTCH/hlp-spice2x/releases
[readme]: https://github.com/djGLiTCH/hlp-spice2x/blob/main/README.md
[issues]: https://github.com/djGLiTCH/hlp-spice2x/issues
[pypi]: https://pypi.org/project/hlp-spice2x/
[hostlighting]: https://github.com/djGLiTCH/gp2040ce-hostlighting
[gp2040-ce-releases]: https://github.com/OpenStickCommunity/GP2040-CE/releases
[hlp-v2.0]: https://github.com/djGLiTCH/GP2040-CE/releases/tag/HLP_v2.0
[python]: https://www.python.org/downloads/
