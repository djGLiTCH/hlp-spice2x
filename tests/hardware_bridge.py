# hlp-spice2x (spice2x to GP2040-CE Host Lighting bridge)
# Filename: hardware_bridge.py
# Folder: tests
# https://github.com/djGLiTCH/hlp-spice2x
# SPDX-FileCopyrightText: (C) 2026 Jacob Simpson (DJ GLiTCH)
# SPDX-License-Identifier: GPL-3.0-or-later
# See LICENSES/GPL-3.0-or-later.txt at the repository root.

"""Run the whole program against a board, with no game and no spice2x.

The other checks drive one piece of the bridge. This one stands up a fake Spice
API on a loopback port, writes a profile to disk, and invokes the command line
entry point as a shell would. Argument parsing, profile loading, discovery, the
handshake, the claim and its renewal, the polling loop, the watch on the
board's state and the shutdown are all the real ones.

The fake API animates its lights, so the board shows a travelling chase around
its buttons and a wave through whatever strip it has. That makes two things
visible:

* Whether the loop keeps up. It reports the rate it achieved and how many
  frames went unacknowledged.
* Whether anything survives between runs. On a second run straight after the
  first, a light holding a colour from the previous run in the first second
  is a pixel that was not staged. Taking the claim clears the staged frame,
  so there should be none.

While it runs, another application can be tried against it. `hlp-fill` from
gp2040ce-hostlighting stops and names hlp-spice2x as the holder, and with
`--force` takes the lights over, which this run reports and then waits out.

The profile it writes is generated from what the board reports, so it maps only
controls that board has. It is written next to this file and left there as an
example of a profile for that board. It uses control names and per-light
indexes. Raw LED ranges are not generated.

Run it from the repository root:

    python -m tests.hardware_bridge
"""
import colorsys
import json
import math
import pathlib
import sys
import threading
import _thread

from gp2040ce_hostlighting.protocol import ADDRESSING_ACTION, ADDRESSING_KIND

from hlp_spice2x import hlp
from hlp_spice2x.__main__ import main as cli_main

from .hardware_lights import named_controls

from .stub_server import StubServer

SECONDS = 30.0
PROFILE_PATH = pathlib.Path(__file__).with_name('hardware_bridge_profile.json')


def wheel(index, total) -> str:
    """Pick a hex colour evenly spaced around the hue wheel.

    :param index: which colour to take
    :param total: how many are being spread around the wheel
    :return: the colour as RRGGBB
    """
    red, green, blue = colorsys.hsv_to_rgb(index / max(total, 1), 1.0, 1.0)
    return f"{int(red * 255):02X}{int(green * 255):02X}{int(blue * 255):02X}"


def describe_board(prefix):
    """Read what the board has, without taking its lighting over.

    :param prefix: board-ID prefix, or '' for the only board attached
    :return: (chase controls, strip control or None, strip light count,
              a control with more than one light or None)
    """
    device = hlp.open_device(prefix)
    try:
        caps = hlp.negotiate(device)
        controls = named_controls(caps)
        chase = [control for control in controls if control[0] == ADDRESSING_ACTION]
        # a strip is a kind of light the board has many of, typically the case
        strips = [(control, len(caps.lights_of(control))) for control in controls
                  if control[0] == ADDRESSING_KIND and len(caps.lights_of(control)) > 1]
        strip, strip_lights = strips[0] if strips else (None, 0)
        multi = next((control for control in chase if len(caps.lights_of(control)) > 1), None)
        return chase, strip, strip_lights, multi
    finally:
        device.close()


def build_profile(chase, strip, strip_lights, multi):
    """Write a profile naming this board's controls, and its strip light by light.

    :param chase: controls to put in the travelling chase
    :param strip: a kind of light the board has many of, addressed one light at a time
    :param strip_lights: how many lights of that kind it has
    :param multi: a control with more than one light, or None
    :return: (the light names the fake API should report, the profile written)
    """
    lights, entries = [], {}

    for index, control in enumerate(chase):
        name = f"Chase {hlp.control_name(control)}"
        entries[name] = {'button': hlp.control_name(control),
                         'colour': wheel(index, len(chase))}
        lights.append(name)

    # every light of the strip addressed on its own, which shows it is a group
    # of individually addressable lights
    strip_names = []
    for index in range(strip_lights):
        name = f"Strip {index}"
        entries[name] = {'button': hlp.control_name(strip), 'index': index,
                         'colour': wheel(index, strip_lights)}
        strip_names.append(name)
    lights.extend(strip_names)

    # one entry picking a single light of a control that has several, so the
    # chase and this entry fight over the same control every frame
    if multi is not None:
        entries['Second light'] = {'button': hlp.control_name(multi), 'index': 1,
                                   'colour': '00FFFF'}
        lights.append('Second light')

    # a light the profile deliberately does not map, so the run reports it
    lights.append('Unmapped Lamp')

    PROFILE_PATH.write_text(json.dumps({'lights': entries}, indent=2) + '\n', encoding='utf-8')
    return lights, strip_names


def animator(chase_names, strip_names):
    """Build the callable the fake API uses to animate its lights.

    :param chase_names: light names forming the travelling chase
    :param strip_names: light names forming the strip wave
    :return: a callable(lights, elapsed) returning the lights to report
    """
    def animate(lights, elapsed):
        """Drive a chase around the buttons and a wave along the strip."""
        step = elapsed * 6.0
        for entry in lights:
            name = entry[0]
            if name in chase_names:
                # a travelling peak, so one control is brightest at a time and
                # its neighbours are part-lit either side of it
                distance = (step - chase_names.index(name)) % len(chase_names)
                entry[1] = round(max(0.0, 1.0 - min(distance, len(chase_names) - distance)), 3)
            elif name in strip_names:
                offset = strip_names.index(name) / len(strip_names) - elapsed * 0.3
                entry[1] = round(((math.cos(2 * math.pi * offset) + 1) / 2) ** 3, 3)
            elif name == 'Second light':
                entry[1] = 1.0 if int(elapsed) % 2 else 0.0
        return lights
    return animate


def main(argv=None) -> int:
    """Drive the command line entry point against an attached board.

    :param argv: optional board-ID prefix as the only argument
    :return: whatever the bridge's own entry point returned
    """
    prefix = (argv or sys.argv[1:] or [''])[0]
    chase, strip, strip_lights, multi = describe_board(prefix)
    if not chase:
        print("this board reports no single-lamp controls to animate")
        return 1

    lights, strip_names = build_profile(chase, strip, strip_lights, multi)
    chase_names = [f"Chase {hlp.control_name(control)}" for control in chase]
    server = StubServer(lights=[[name, 0.0, True] for name in lights],
                        mutate=animator(chase_names, strip_names))
    print(f"fake Spice API on port {server.port}")
    print(f"profile written to {PROFILE_PATH.name}: {len(chase)} chase control(s), "
          f"{strip_lights} strip light(s)"
          + (f", one entry on {hlp.control_name(multi)}[1]" if multi is not None else ""))
    print(f"running the real entry point for {SECONDS:.0f}s, then interrupting it\n")

    # a real interrupt, so the shutdown path and the hand back are the real ones
    threading.Timer(SECONDS, _thread.interrupt_main).start()
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
