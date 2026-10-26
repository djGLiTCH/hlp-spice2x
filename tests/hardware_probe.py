"""Print what an attached board reports about itself, as the bridge reads it.

The first check to run against a new board, and the first to run when the
bridge behaves unexpectedly.

It is read-only. It sends HELLO and GET_PAGE and nothing else, and it claims
nothing and stages nothing, so it cannot disturb a game or another
application's lights.

What is printed, in order:

* HELLO carries the protocol version, the board ID and the capability bitmaps.
  The bridge decides everything from the bitmaps, never from the minor
  version. A profile or a script binds to the board ID, since device paths
  change with USB ports.
* Page 0 is identity, the board label and firmware version.
* Page 1 is state, which gives whether a host frame is shown, the holder, and
  the CONFIGURE settings in force.
* Page 8 is the Summary, which gives the LED extent and addressable limit, the
  render rate and colour format, and a change counter for each configuration
  page.
* Page 12 is the Lights page, which lists every light with its LEDs, its
  kind, and the pin and action it is tied to. A profile's `index` counts a
  control's lights in the order they appear here.
* Page 10 is the Controls page, with one record per control carrying an
  action, lit or not. A control it leaves out does not exist on the board.

The two tables are then checked against the Summary's counts and against each
other, and the probe exits non-zero if a check fails.

`hlp-caps` in gp2040ce-hostlighting prints every page and field the protocol
has. This prints the ones the bridge reads, as the bridge reads them.

Run it from the repository root:

    python -m tests.hardware_probe

SPDX-FileCopyrightText: (C) 2026 Jacob Simpson
SPDX-License-Identifier: GPL-3.0-or-later
"""
import sys

from gp2040ce_hostlighting import protocol as hl
from gp2040ce_hostlighting.protocol import key_name

from hlp_spice2x import hlp

KIND_NAMES = {0: 'button', 1: 'case', 2: 'turbo', 3: 'player 1', 4: 'player 2', 5: 'player 3',
              6: 'player 4'}


def light_mark(record) -> str:
    """Describe one Lights record by its ordinal, its LEDs and the key of the input it follows."""
    mark = f"#{record.ordinal}@LED{record.leds.first_led}+{record.leds.led_count}"
    return mark if record.owner.pin is None else f"{mark} on {key_name(record.owner.pin)}"


def action_name(action) -> str:
    """Name an action as a profile would, or by its value where a profile cannot name it."""
    if action is None:
        return 'none'
    return hlp.control_name((hl.ADDRESSING_ACTION, action))


def describe_hello(hello) -> None:
    """Print the decoded HELLO, naming the bitmaps the bridge gates on."""
    print(f"\nHELLO: protocol {hello.major}.{hello.minor}, board ID {hello.board_id}, "
          f"reply queue limit {hello.reply_queue_limit}")
    print(f"   request flags        {sorted(hello.request_flags)}")
    print(f"   commands             {', '.join(f'0x{command:02X}' for command in sorted(hello.commands))}")
    print(f"   pages                {sorted(hello.pages)}")
    print(f"   addressing modes     {sorted(hello.addressing_modes)}")
    print(f"   pixel formats        {sorted(hello.pixel_formats)}")
    print(f"   events               {sorted(hello.events)}, "
          f"enabled now {sorted(hello.events_enabled)}")
    missing = hlp.lacking(hello, (hl.ADDRESSING_LED_RUN, hl.ADDRESSING_LIGHT,
                                  hl.ADDRESSING_ACTION, hl.ADDRESSING_KIND))
    print(f"   the bridge needs     {'nothing this board lacks' if not missing else ', '.join(missing)}")


def describe_state(read) -> None:
    """Print the decoded page 1."""
    record = read.records[0]
    state, controller, settings = record.state, record.controller, record.settings
    print(f"\npage {hl.PAGE_STATE}, state:")
    print(f"   host frame shown     {state.takeover_live} (takeover number {state.takeover_number})")
    print(f"   input mode           {state.input_mode}")
    print(f"   profile              {state.profile}")
    print(f"   brightness step      {state.brightness_step_shown}")
    holder = (f"'{controller.application_name}' (token 0x{controller.holder_token:08X}"
              f"{', exclusive' if controller.exclusive else ''})"
              if controller.holder_token else 'nobody')
    print(f"   lights claimed by    {holder}, claim generation {controller.claim_generation}")
    print(f"   takeover settings    {'overlay' if settings.takeover == hl.TAKEOVER_OVERLAY else 'whole frame'}, "
          f"{settings.keepalive_ms} ms keepalive, board brightness "
          f"{'applied' if settings.brightness_applied else 'ignored'}")
    print(f"   state token          0x{read.token:08X} (changes when a configuration page does)")


def describe_summary(summary, counters) -> None:
    """Print the decoded page 8."""
    print(f"\npage {hl.PAGE_SUMMARY}, summary:")
    print(f"   LED extent           {summary.led_extent} (the number to size a frame from)")
    print(f"   addressable limit    {summary.led_limit} (an LED run reaching past it stages nothing)")
    print(f"   render rate          {summary.render_hz} Hz"
          if summary.render_hz else "   render rate          not stated")
    print(f"   colour format        {summary.colour_format} "
          f"({hlp.LED_FORMAT_NAMES.get(summary.colour_format, 'unknown')}), "
          f"white channel {summary.white_channel}")
    print(f"   controls             {summary.controls} "
          f"({summary.lit_controls} lit, {summary.unlit_controls} unlit)")
    print(f"   lights               {summary.lights}")
    changes = ', '.join(f"{hl.PAGE_SUMMARY + index}: {count}" for index, count in enumerate(counters))
    print(f"   change counters      {changes}")


def describe_lights(records) -> None:
    """Print the Lights page grouped by what a profile would name each light by."""
    print(f"\npage {hl.PAGE_LIGHTS}, lights: {len(records)} record(s)")
    owners = {}
    for record in records:
        if record.kind.value == hl.KIND_BUTTON and record.owner.action is not None:
            owner = action_name(record.owner.action)
        else:
            owner = hlp.CONTROL_NAMES.get((hl.ADDRESSING_KIND, record.kind.value),
                                          KIND_NAMES.get(record.kind.value, f"kind {record.kind.value}"))
        owners.setdefault(owner, []).append(record)
    for owner, rows in owners.items():
        marks = ', '.join(light_mark(row) for row in rows)
        note = '   <-- more than one light' if len(rows) > 1 else ''
        print(f"   {owner:10} x{len(rows):<3} {marks}{note}")


def describe_controls(records) -> None:
    """Print the Controls page one input per line, marking duplicated controls."""
    lit = sum(1 for record in records if record.input.lit)
    print(f"\npage {hl.PAGE_CONTROLS}, controls: {len(records)} record(s), {lit} lit, {len(records) - lit} unlit")
    for record in records:
        print(f"   {key_name(record.input.key):6} action {record.input.action:<4} "
              f"{action_name(record.input.action):10} {'lit' if record.input.lit else 'unlit'}"
              f"{' (duplicate)' if record.input.duplicated else ''}")


def check_tables(summary, controls, lights) -> list:
    """Check the two tables against the Summary's counts and against each other.

    The Summary's counts come from the same walk as the records. A control is
    lit when at least one Lights record carries its key as owner pin, so the
    lit keys must equal the owner pins that are controls.

    :param summary: the decoded Summary
    :param controls: every page 10 record
    :param lights: every page 12 record
    :return: (what was checked, whether it held) for each check made
    """
    lit = sum(1 for record in controls if record.input.lit)
    counted = (summary.controls == len(controls) and summary.lit_controls == lit
               and summary.lit_controls + summary.unlit_controls == summary.controls)
    checks = [(f"{summary.lit_controls} lit + {summary.unlit_controls} unlit = "
               f"{summary.controls} controls, {lit} of {len(controls)} records marked lit", counted),
              (f"{summary.lights} lights, {len(lights)} records", summary.lights == len(lights))]
    keys = {record.input.key for record in controls}
    lit_keys = sorted(record.input.key for record in controls if record.input.lit)
    owner_pins = sorted({record.owner.pin for record in lights
                         if record.owner.pin is not None and record.owner.pin in keys})
    joined = lit_keys == owner_pins
    checks.append((f"lit controls match the {len(owner_pins)} owner pins on the Lights page" if joined
                   else f"lit keys {lit_keys} against the Lights page's owner pins {owner_pins}",
                   joined))
    return checks


def main(argv=None) -> int:
    """Open a board, print what it reports, and leave its lighting alone.

    :param argv: optional board-ID prefix as the only argument
    :return: process exit status
    """
    prefix = (argv or sys.argv[1:] or [''])[0]
    found = hl.find_devices()
    print(f"{len(found)} Host Lighting interface(s) found")
    if not found:
        print(hlp.no_interface())
        return 1

    device = hlp.open_device(prefix)
    try:
        hello = device.hello()
        describe_hello(hello)
        if hl.PAGE_IDENTITY in hello.pages:
            label, firmware = hlp.label_and_firmware(device)
            print(f"\npage {hl.PAGE_IDENTITY}, identity: {label} ({firmware})")
        describe_state(device.read_record(hl.PAGE_STATE, hlp.STATE_GROUPS))

        # what a host would decide from all of the above
        caps = hlp.negotiate(device)
        describe_summary(caps.summary, caps.cache.counters)
        describe_lights(caps.lights)
        coherent = True
        if caps.controls is None:
            print(f"\npage {hl.PAGE_CONTROLS}, controls: this firmware does not list the page")
        else:
            describe_controls(caps.controls)
            for what, held in check_tables(caps.summary, caps.controls, caps.lights):
                print(f"   check: {what}: {'coherent' if held else 'MISMATCH'}")
                coherent = coherent and held
        print(f"\nas the bridge sees it: {caps.summary_line()}")
    finally:
        device.close()
    return 0 if coherent else 1


if __name__ == '__main__':
    sys.exit(main())
