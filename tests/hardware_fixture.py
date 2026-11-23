# hlp-spice2x (spice2x to GP2040-CE Host Lighting bridge)
# Filename: hardware_fixture.py
# Folder: tests
# https://github.com/djGLiTCH/hlp-spice2x
# SPDX-FileCopyrightText: (C) 2026 Jacob Simpson (DJ GLiTCH)
# SPDX-License-Identifier: GPL-3.0-or-later
# See LICENSES/GPL-3.0-or-later.txt at the repository root.

"""Show two RGB fixtures on a board, through the whole program.

Some games drive an RGB fixture as three lights, its red, green and blue.
This check stands up a fake Spice API reporting two such fixtures, and writes
a profile that maps each one to a control of the attached board:

* the first by a fixture entry, whose one button maps all three lights
* the second by three channel entries, one per light

It then runs the command line entry point against the board. The fake API
steps both fixtures through colours that are easy to tell apart, and prints
each step as it starts. The two fixtures always show contrasting colours, so
a swap between them shows as well.

The profile is written next to this file and left there as an example.

Run it from the repository root:

    python -m tests.hardware_fixture
"""
import json
import pathlib
import sys
import threading
import _thread

from hlp_spice2x import hlp
from hlp_spice2x.__main__ import main as cli_main

from .hardware_bridge import describe_board
from .stub_server import StubServer

STEP_SECONDS = 2.0
PROFILE_PATH = pathlib.Path(__file__).with_name('hardware_fixture_profile.json')
# each colour as the game's red, green and blue levels
COLOURS = {'red': (1.0, 0.0, 0.0), 'green': (0.0, 1.0, 0.0), 'blue': (0.0, 0.0, 1.0),
           'yellow': (1.0, 1.0, 0.0), 'cyan': (0.0, 1.0, 1.0), 'magenta': (1.0, 0.0, 1.0),
           'white': (1.0, 1.0, 1.0), 'orange': (1.0, 0.5, 0.0)}
# Each step's colours for the two fixtures. A watcher compares them, so each
# pair is of contrasting colours and never neighbouring hues.
STEPS = [('red', 'cyan'), ('green', 'magenta'), ('blue', 'yellow'), ('yellow', 'blue'),
         ('cyan', 'red'), ('magenta', 'green'), ('white', 'red'), ('orange', 'blue')]
FIXTURES = ('Fixture One', 'Fixture Two')


def build_profile(first, second, names=None) -> list:
    """Write a profile mapping one fixture by a fixture entry and the other by channel entries.

    :param first: the control for the first fixture
    :param second: the control for the second fixture
    :param names: the board's page 3 names
    :return: the light names the fake API should report
    """
    entries = {FIXTURES[0]: {'button': hlp.control_name(first, names),
                             'channels': {letter: f"{FIXTURES[0]} {letter}" for letter in 'RGB'}}}
    for letter in 'RGB':
        entries[f"{FIXTURES[1]} {letter}"] = {'button': hlp.control_name(second, names), 'channel': letter}
    PROFILE_PATH.write_text(json.dumps({'lights': entries}, indent=2) + '\n', encoding='utf-8')
    return [f"{fixture} {letter}" for fixture in FIXTURES for letter in 'RGB']


def step_at(elapsed) -> int:
    """Give the step both fixtures show at a time into the run, or len(STEPS) once all are shown."""
    return min(int(elapsed / STEP_SECONDS), len(STEPS))


def animator(first, second, names=None):
    """Build the callable the fake API uses to step both fixtures through the colours.

    :param first: the first fixture's control, for the printed steps
    :param second: the second fixture's control
    :param names: the board's page 3 names
    :return: a callable(lights, elapsed) returning the lights to report
    """
    shown = [-1]

    def animate(lights, elapsed):
        """Set each fixture's red, green and blue to its colour for the step, and say when a step starts."""
        step = step_at(elapsed)
        if step >= len(STEPS):
            return lights
        colours = STEPS[step]
        if step != shown[0]:
            shown[0] = step
            print(f"  step {step + 1} of {len(STEPS)}: {hlp.control_name(first, names)} {colours[0]}, "
                  f"{hlp.control_name(second, names)} {colours[1]}")
        levels = {f"{fixture} {letter}": level for fixture, colour in zip(FIXTURES, colours)
                  for letter, level in zip('RGB', COLOURS[colour])}
        for entry in lights:
            entry[1] = levels.get(entry[0], entry[1])
        return lights
    return animate


def main(argv=None) -> int:
    """Drive the command line entry point against an attached board with two fixtures.

    :param argv: optional board-ID prefix as the only argument
    :return: whatever the bridge's own entry point returned
    """
    prefix = (argv or sys.argv[1:] or [''])[0]
    chase, _, _, _, names = describe_board(prefix)
    if len(chase) < 2:
        print("this board reports fewer than two controls to show the fixtures on")
        return 1
    first, second = chase[0], chase[1]

    lights = build_profile(first, second, names)
    server = StubServer(lights=[[name, 0.0, True] for name in lights], mutate=animator(first, second, names))
    seconds = STEP_SECONDS * len(STEPS) + 1.0
    print(f"fake Spice API on port {server.port}")
    print(f"profile written to {PROFILE_PATH.name}: {FIXTURES[0]} on {hlp.control_name(first, names)} "
          f"by a fixture entry, {FIXTURES[1]} on {hlp.control_name(second, names)} by channel entries")
    print(f"running the real entry point for {seconds:.0f}s, then interrupting it\n")

    # a real interrupt, so the shutdown path and the hand back are the real ones
    threading.Timer(seconds, _thread.interrupt_main).start()
    status = 1
    try:
        arguments = ['--port', str(server.port), '--profile', str(PROFILE_PATH)]
        if prefix:
            arguments += ['--board-id', prefix]
        status = cli_main(arguments)
        print(f"\nthe entry point returned {status}")
    except KeyboardInterrupt:
        print("\nthe interrupt reached the top level instead of being handled")
    finally:
        server.stop()
        print(f"the fake API answered {server.requests} polls")
    return status


if __name__ == '__main__':
    sys.exit(main())
