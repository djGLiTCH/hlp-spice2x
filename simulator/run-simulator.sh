#!/bin/sh
# hlp-spice2x (spice2x to GP2040-CE Host Lighting bridge)
# Filename: run-simulator.sh
# Folder: simulator
# https://github.com/djGLiTCH/hlp-spice2x
# SPDX-FileCopyrightText: (C) 2026 Jacob Simpson (DJ GLiTCH)
# SPDX-License-Identifier: GPL-3.0-or-later
# See LICENSES/GPL-3.0-or-later.txt at the repository root.
#
# Sets up .venv on the first run, then runs the bridge against a stand-in
# for spice2x and the attached board. It runs from the root of the
# simulator package, which holds the wheels in wheels/.
# Usage: sh run-simulator.sh [start of the board ID]
set -e
cd "$(dirname "$0")"

VENV=.venv/bin/python
if [ ! -f .venv/installed.txt ]; then
    PYTHON=""
    for candidate in python3.14 python3.15 python3; do
        if command -v "$candidate" >/dev/null 2>&1 &&
           "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 14))'; then
            PYTHON=$candidate
            break
        fi
    done
    if [ -z "$PYTHON" ]; then
        echo "Python 3.14 or later was not found. Install it from python.org or your package manager."
        exit 1
    fi
    echo "Setting up .venv with $PYTHON, which takes a minute the first time."
    [ -x "$VENV" ] || "$PYTHON" -m venv .venv
    "$VENV" -m pip install --disable-pip-version-check --quiet hidapi
    "$VENV" -m pip install --disable-pip-version-check --quiet --no-deps --no-index --force-reinstall \
        --find-links wheels gp2040ce-hostlighting hlp-spice2x
    echo installed > .venv/installed.txt
fi

"$VENV" -m hlp_spice2x --version
echo
"$VENV" -m hlp_spice2x --list-boards
echo
"$VENV" -m tests.hardware_bridge "$@"
