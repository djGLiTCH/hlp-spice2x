"""Run the whole program against a board, with no game and no spice2x.

Everything else here reaches into the bridge and drives a piece of it. This does
not: it stands up a fake Spice API on a loopback port, writes a profile to disk,
and invokes the command line entry point exactly as a shell would. Argument
parsing, profile loading, device discovery, the handshake, the polling loop, the
keepalive, the periodic LED-map check and the shutdown are all the real ones.

It is the closest thing to running the bridge for real that does not need a
cabinet, and it is the only check here that exercises them together rather than
one at a time.

The fake API animates its lights, so the board shows a travelling chase around
its buttons and a wave through whatever strip it has. That makes two things
visible that a static frame cannot:

* whether the loop keeps up. It reports the rate it achieved and how many
  commits went unacknowledged, and a chase that stutters says more about the
  loop than any number does.
* whether anything survives between runs. Run it twice in a row and watch the
  second run's first second: a light holding a colour from the previous run is
  a pixel nobody staged, which means the staging buffer was not cleared at
  startup.

The profile it writes is generated from what the board reports, so it maps only
controls that board actually has. It is written next to this file and left
behind on purpose - it is a worked example of every profile entry kind, and it
is the easiest way to see what one looks like for your own board.

Run it from the repository root:

    python -m tests.hardware_bridge

SPDX-FileCopyrightText: © 2026 Jacob Simpson
SPDX-License-Identifier: GPL-3.0-or-later
"""
import colorsys
import json
import math
import pathlib
import sys
import threading
import _thread

from hlp_spice2x import hlp
from hlp_spice2x.__main__ import main as cli_main

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
    :return: (chase control IDs, strip control ID or None, strip light count,
              a control owning more than one light or None)
    """
    device = hlp.open_device(prefix)
    try:
        caps = hlp.negotiate(device)
        controls = sorted({record['button_id'] for record in caps.lights})
        chase = [button_id for button_id in controls if button_id in hlp.ONE_LAMP_CONTROLS]
        # a strip is any control the board gives many lights and that is not
        # meant to be one lamp: the case, typically
        strips = [(button_id, len(caps.lights_owned_by(button_id))) for button_id in controls
                  if button_id not in hlp.ONE_LAMP_CONTROLS
                  and len(caps.lights_owned_by(button_id)) > 1]
        strip, strip_lights = strips[0] if strips else (None, 0)
        multi = next((button_id for button_id in chase
                      if len(caps.lights_owned_by(button_id)) > 1), None)
        return chase, strip, strip_lights, multi
    finally:
        device.close()


def build_profile(chase, strip, strip_lights, multi):
    """Write a profile covering every kind of entry the board can take.

    :param chase: control IDs to put in the travelling chase
    :param strip: a control with many lights, addressed one light at a time
    :param strip_lights: how many lights that control has
    :param multi: a control owning more than one light, or None
    :return: (the light names the fake API should report, the profile written)
    """
    lights, entries = [], {}

    for index, button_id in enumerate(chase):
        name = f"Chase {hlp.control_name(button_id)}"
        entries[name] = {'button': hlp.control_name(button_id),
                         'colour': wheel(index, len(chase))}
        lights.append(name)

    # every light of the strip addressed on its own, which is what shows it is a
    # group of individually addressable lights rather than one lamp
    strip_names = []
    for index in range(strip_lights):
        name = f"Strip {index}"
        entries[name] = {'button': hlp.control_name(strip), 'index': index,
                         'colour': wheel(index, strip_lights)}
        strip_names.append(name)
    lights.extend(strip_names)

    # one entry picking a single light of a control that owns several, so the
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
    chase_names = [f"Chase {hlp.control_name(button_id)}" for button_id in chase]
    server = StubServer(lights=[[name, 0.0, True] for name in lights],
                        mutate=animator(chase_names, strip_names))
    print(f"fake Spice API on port {server.port}")
    print(f"profile written to {PROFILE_PATH.name}: {len(chase)} chase control(s), "
          f"{strip_lights} strip light(s)"
          + (f", one entry on {hlp.control_name(multi)}[1]" if multi is not None else ""))
    print(f"running the real entry point for {SECONDS:.0f}s, then interrupting it\n")

    # a real interrupt, so the shutdown path and the release are the real ones
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
