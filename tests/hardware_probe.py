"""Print everything an attached board reports about itself.

The first thing to run against a new board, and the thing to run first when
something is behaving oddly, because almost every surprising behaviour further
up traces back to something on one of these pages.

Read-only: it sends PING and GET_CAPS and nothing else, so it never takes the
board's lighting over and cannot disturb a game.

What the pages mean, in the order they are printed:

* PING carries the protocol version. Everything else the bridge does is decided
  from it, and the rule is that a minor version only ever adds. A board
  reporting a minor newer than the host knows is still safe to drive as the
  newest the host does know.
* Page 0 is identity: the factory board ID is the right thing to bind a profile
  or a script to, because device paths change with USB ports.
* Page 1 is runtime state. Its feature bitmask, LED framework, animation
  namespace and render rate were added by protocol v1.1, and firmware older
  than that zero-fills them - which reads correctly as "not reported" rather
  than as a value.
* Page 2 is the per-control LED map: where to write a control. It reports at
  most one range per control, so it can never say that a control owns several
  lights.
* Page 5 is the light table, added by v1.1: the board's inventory of its lights,
  naming the control that owns each. This is the page that can say a control
  owns more than one light, and the one whose ordinals per-light staging
  addresses.

Two things on page 5 are worth understanding before trusting it. A record marked
synthesised was rebuilt from the board's per-control configuration rather than
read from a real per-light table, so such a board reports one row per control
however many lights that control really drives - it cannot show a duplicate even
when one exists. And the light registry is populated during LED setup, so a host
that enumerates very early can legitimately see the page 1 feature bit clear on a
board that would have answered a moment later.

Run it from the repository root:

    python -m tests.hardware_probe

SPDX-FileCopyrightText: (C) 2026 Jacob Simpson
SPDX-License-Identifier: GPL-3.0-or-later
"""
import sys

from hlp_spice2x import hlp


def describe_state(state) -> None:
    """Print the decoded page 1, flagging what each field is good for."""
    print("\npage 1, runtime state:")
    print(f"   input mode           {state['input_mode']}")
    print(f"   profile              {state['profile']}")
    print(f"   brightness step      {state['brightness_step']} "
          f"(a step index into the board's own steps, not a 0-255 level)")
    print(f"   host player          {state['host_player']}")
    print(f"   LED-map fingerprint  {state['fingerprint']} "
          f"(changes when the map does; re-read the pages you cache)")
    print(f"   animation index      {state['animation_index']}")
    print(f"   feature bitmask      0x{state['features']:08X}")
    print(f"      light table       {bool(state['features'] & hlp.FEATURE_LIGHT_TABLE)} "
          f"(clear is a promise page 5 returns nothing)")
    print(f"      positions         {bool(state['features'] & hlp.FEATURE_POSITIONS)}")
    print(f"   LED framework        {state['framework']} "
          f"(diagnostic only - branch on the feature bits, never on this)")
    print(f"   animation namespace  {state['animation_namespace']}")
    print(f"   render rate          {state['render_hz']} Hz"
          if state['render_hz'] else "   render rate          not stated")


def describe_led_map(led_map) -> None:
    """Print the decoded page 2, naming the controls it maps."""
    print("\npage 2, per-control LED map:")
    print(f"   LEDs per button      {led_map['leds_per_button']}")
    print(f"   colour format        {led_map['colour_format']} "
          f"({hlp.LED_FORMAT_NAMES.get(led_map['colour_format'], 'unknown')}), "
          f"white channel {hlp.has_white_channel(led_map['colour_format'])}")
    print(f"   layout               {led_map['layout']}")
    print(f"   LED extent           {led_map['led_extent']} "
          f"(the number to size a frame buffer from)")
    print(f"   brightness maximum   {led_map['brightness_maximum']}")
    print(f"   turbo                {led_map['turbo']}")
    print(f"   case                 {led_map['case']}")
    print(f"   players              {led_map['players'] or 'none'}")
    mapped = ', '.join(f"{hlp.control_name(button_id)}@{first}+{count}"
                       for button_id, (first, count) in sorted(led_map['buttons'].items()))
    print(f"   controls             {mapped or 'none'}")


def describe_lights(records) -> None:
    """Print the light table grouped by the control that owns each light."""
    print(f"\npage 5, light table: {len(records)} record(s)")
    if not records:
        print("   the board reported no lights. On firmware from v1.1 that means the")
        print("   feature bit is clear, which can simply mean LED setup has not")
        print("   finished; try again in a moment.")
        return

    synthesised = sum(1 for record in records if record['synthesised'])
    if synthesised:
        print(f"   {synthesised} record(s) are synthesised from per-control configuration.")
        print("   Those cannot show a control owning several lights, so per-light")
        print("   targeting into them is limited however the board is really wired.")

    owners = {}
    for record in records:
        owners.setdefault(record['button_id'], []).append(record)
    for button_id in sorted(owners):
        rows = owners[button_id]
        marks = ', '.join(f"#{row['ordinal']}@LED{row['first_led']}+{row['led_count']}"
                          f"{' synthesised' if row['synthesised'] else ''}" for row in rows)
        note = '   <-- owns more than one light' if len(rows) > 1 else ''
        print(f"   {hlp.control_name(button_id):8} ({button_id:3}) x{len(rows):<3} {marks}{note}")


def main(argv=None) -> int:
    """Open a board, print what it reports, and leave its lighting alone.

    :param argv: optional board-ID prefix as the only argument
    :return: process exit status
    """
    prefix = (argv or sys.argv[1:] or [''])[0]
    found = hlp.find_devices()
    print(f"{len(found)} Host Lighting interface(s) found")
    if not found:
        print("no board attached, or the Host Lighting add-on is not enabled on it")
        return 1

    device = hlp.open_device(prefix)
    try:
        major, minor = hlp.read_protocol_version(device)
        board_id, label, firmware = hlp.read_identity(device)
        print(f"\nPING: protocol {major}.{minor}")
        print(f"page 0, identity: {label} ({firmware}), board ID {board_id}")

        describe_state(hlp.read_state(device))
        describe_led_map(hlp.read_led_map(device))

        # An unknown page is answered INVALID_ARG, which the protocol says to
        # read as "not supported by this firmware" rather than as a failure. It
        # is how a v1.0 board tells a host that page 5 does not exist yet.
        try:
            records, fingerprint = device.read_lights()
        except hlp.HostLightingRejected:
            print("\npage 5, light table: not supported by this firmware (pre-v1.1)")
        else:
            describe_lights(records)
            print(f"   light table fingerprint {fingerprint}")

        # what a host would decide from all of the above
        caps = hlp.negotiate(device)
        print(f"\nnegotiated: {caps.summary()}")
    finally:
        device.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
