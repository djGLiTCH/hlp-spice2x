# hlp-spice2x (spice2x to GP2040-CE Host Lighting bridge)
# Filename: hardware_probe.py
# Folder: tests
# https://github.com/djGLiTCH/hlp-spice2x
# SPDX-FileCopyrightText: (C) 2026 Jacob Simpson (DJ GLiTCH)
# SPDX-License-Identifier: GPL-3.0-or-later
# See LICENSES/GPL-3.0-or-later.txt at the repository root.

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
* Page 3 gives the firmware's names for its actions and light kinds. A profile
  can name a control by any of them, and the bridge resolves each short name
  through them. Each action and kind is shown by its short name, else by the
  firmware's name, as a profile names it. The probe times the read as the
  bridge makes it before each claim.
* Page 8 is the Summary, which gives the LED extent and addressable limit, the
  render rate and colour format, and a change counter for each configuration
  page.
* Page 12 is the Lights page, which lists every light with its LEDs, its
  kind, and the pin and action it is tied to. A profile's `index` counts a
  control's lights in the order they appear here.
* Page 10 is the Controls page, with one record per control carrying an
  action, lit or not. A control it leaves out does not exist on the board.
* Page 2 lists the input modes, with the web configurator's group for each,
  whether SET_INPUT_MODE accepts it, its lighting interface and its use of the
  USB host peripheral. A group the probe does not know is shown by its value.
  The bridge does not read it.
* Page 4 lists the boot holds, the buttons or pins held at plug-in that boot an
  input mode, the web configurator or the UF2 bootloader. A hold into a mode
  with the lighting interface is how to return to the bridge without a host.
  The bridge does not read it.

The two tables are then checked against the Summary's counts and against each
other, and pages 2 and 4 against page 1 and each other. The probe exits
non-zero if a check fails.

`hlp-caps` in gp2040ce-hostlighting prints every page and field the protocol
has. This prints the ones the bridge reads, as the bridge reads them, and pages
2 and 4.

Run it from the repository root:

    python -m tests.hardware_probe
"""
import sys
import time

from gp2040ce_hostlighting import protocol as hl
from gp2040ce_hostlighting.protocol import key_name

from hlp_spice2x import bridge, hlp

KIND_NAMES = {0: 'button', 1: 'case', 2: 'turbo', 3: 'player 1', 4: 'player 2', 5: 'player 3',
              6: 'player 4'}
# page 2's USB host field and page 4's kinds
USB_HOST_USES = {hl.INPUT_MODE_USB_HOST_UNUSED: '', hl.INPUT_MODE_USB_HOST_OPTIONAL: ', USB host optional',
                 hl.INPUT_MODE_USB_HOST_REQUIRED: ', USB host required'}
BOOT_KINDS = {hl.BOOT_HOLD_WEB_CONFIGURATOR: 'web configurator', hl.BOOT_HOLD_UF2_BOOTLOADER: 'UF2 bootloader'}
CATEGORIES = {hl.INPUT_MODE_CATEGORY_PRIMARY: 'primary', hl.INPUT_MODE_CATEGORY_MINI: 'console mini',
              hl.INPUT_MODE_CATEGORY_SONY: 'Sony', hl.INPUT_MODE_CATEGORY_NINTENDO: 'Nintendo',
              hl.INPUT_MODE_CATEGORY_MICROSOFT: 'Microsoft'}


def light_mark(record) -> str:
    """Describe one Lights record by its ordinal, its LEDs and the key of the input it follows."""
    mark = f"#{record.ordinal}@LED{record.leds.first_led}+{record.leds.led_count}"
    return mark if record.owner.pin is None else f"{mark} on {key_name(record.owner.pin)}"


def action_name(action, names=None) -> str:
    """Name an action as a profile would: its short name, else the firmware's name for it, else its value.

    :param action: the action's value, or None
    :param names: page 3's names, as read_names() returns them
    """
    if action is None:
        return 'none'
    return hlp.control_name((hl.ADDRESSING_ACTION, action), names)


def kind_name(kind, names=None) -> str:
    """Name a light kind: its short name, else as the probe does, else the firmware's name for it.

    :param kind: the kind's value
    :param names: page 3's names, as read_names() returns them
    """
    named = hlp.page_name((hl.ADDRESSING_KIND, kind), names or {})
    if named is not None and named.upper() in hlp.SHORT_NAMES:
        return hlp.SHORT_NAMES[named.upper()]
    return KIND_NAMES.get(kind) or named or f"kind {kind}"


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
    state, holder, settings = record.state, record.holder, record.settings
    print(f"\npage {hl.PAGE_STATE}, state:")
    print(f"   host frame shown     {state.takeover_live} (takeover number {state.takeover_number})")
    print(f"   input mode           {state.input_mode}")
    print(f"   profile              {state.profile}")
    print(f"   brightness step      {state.brightness_step_shown}")
    claimed_by = (f"'{holder.application_name}' (token 0x{holder.holder_token:08X}"
                  f"{', exclusive' if holder.exclusive else ''})"
                  if holder.holder_token else 'nobody')
    print(f"   lights claimed by    {claimed_by}, claim generation {holder.claim_generation}")
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
    print(f"   USB host peripheral  {'enabled' if summary.usb_host else 'disabled'}")
    print(f"   controls             {summary.controls} "
          f"({summary.lit_controls} lit, {summary.unlit_controls} unlit)")
    print(f"   lights               {summary.lights}")
    changes = ', '.join(f"{hl.PAGE_SUMMARY + index}: {count}" for index, count in enumerate(counters))
    print(f"   change counters      {changes}")


def describe_names(names) -> None:
    """Print how many names page 3 gives, and any short name this firmware has no value for."""
    print(f"\npage {hl.PAGE_NAMES}, names: {len(names.get('GpioAction', {}))} action(s), "
          f"{len(names.get('LightType', {}))} light kind(s)")
    index = hlp.name_index(names)
    missing = [hlp.CONTROL_NAMES[control] for control in hlp.FIRMWARE_NAMES
               if hlp.resolve_control(control, index) is None]
    print(f"   short names          {len(hlp.FIRMWARE_NAMES) - len(missing)} of {len(hlp.FIRMWARE_NAMES)} "
          f"on this firmware{', not ' + ', '.join(missing) if missing else ''}")


def describe_lights(records, names=None) -> None:
    """Print the Lights page grouped by what a profile would name each light by."""
    print(f"\npage {hl.PAGE_LIGHTS}, lights: {len(records)} record(s)")
    owners = {}
    for record in records:
        if record.kind.value == hl.KIND_BUTTON and record.owner.action is not None:
            owner = action_name(record.owner.action, names)
        else:
            owner = kind_name(record.kind.value, names)
        owners.setdefault(owner, []).append(record)
    for owner, rows in owners.items():
        marks = ', '.join(light_mark(row) for row in rows)
        note = '   <-- more than one light' if len(rows) > 1 else ''
        print(f"   {owner:10} x{len(rows):<3} {marks}{note}")


def describe_controls(records, names=None) -> None:
    """Print the Controls page one input per line, marking duplicated controls."""
    lit = sum(1 for record in records if record.input.lit)
    print(f"\npage {hl.PAGE_CONTROLS}, controls: {len(records)} record(s), {lit} lit, {len(records) - lit} unlit")
    for record in records:
        print(f"   {key_name(record.input.key):6} action {record.input.action:<4} "
              f"{action_name(record.input.action, names):10} {'lit' if record.input.lit else 'unlit'}"
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


def mode_name(value, modes) -> str:
    """Name an input mode by page 2's name for it, else by its value."""
    named = next((record.name for record in modes if record.mode.value == value), None)
    return f"{named} ({value})" if named else f"input mode {value}"


def describe_input_modes(records, current) -> None:
    """Print page 2 one input mode per line, marking the mode the board is in.

    :param records: every page 2 record
    :param current: page 1's input mode
    """
    accepted = sum(1 for record in records if record.mode.accepted)
    print(f"\npage {hl.PAGE_INPUT_MODES}, input modes: {len(records)} record(s), {accepted} accepted")
    for record in records:
        mode = record.mode
        lighting = ('lighting once a PC is detected' if mode.detected else 'lighting') if mode.lighting else \
            'no lighting'
        category = record.capabilities.category
        print(f"   {mode.value:<4} {record.name:22} {CATEGORIES.get(category, f'category {category}'):12} "
              f"{'accepted' if mode.accepted else 'refused '} {lighting}"
              f"{USB_HOST_USES.get(record.authentication.usb_host, '')}"
              f"{'   <-- now' if mode.value == current else ''}")


def check_input_modes(records, summary, current) -> list:
    """Check page 2 against page 1 and the Summary.

    :param records: every page 2 record
    :param summary: the decoded Summary
    :param current: page 1's input mode
    :return: (what was checked, whether it held) for each check made
    """
    values = [record.mode.value for record in records]
    now = next((record for record in records if record.mode.value == current), None)
    needs_host = [record.mode.value for record in records if record.mode.accepted
                  and record.authentication.usb_host == hl.INPUT_MODE_USB_HOST_REQUIRED]
    return [(f"the config mode ({hl.INPUT_MODE_CONFIG}) is not listed", hl.INPUT_MODE_CONFIG not in values),
            (f"the input mode now ({current}) is listed with the lighting interface",
             now is not None and now.mode.lighting),
            (f"{len(needs_host)} accepted mode(s) need the USB host peripheral, which is "
             f"{'enabled' if summary.usb_host else 'disabled'}", summary.usb_host or not needs_host)]


def describe_boot_holds(records, names=None, modes=()) -> None:
    """Print page 4 one hold per line: what it boots, then what is held.

    :param records: every page 4 record
    :param names: page 3's names, as read_names() returns them
    :param modes: every page 2 record, to name a hold's input mode
    """
    print(f"\npage {hl.PAGE_BOOT_HOLDS}, boot holds: {len(records)} record(s)")
    for record in records:
        target, hold = record.target, record.hold
        boots = (mode_name(target.mode, modes) if target.kind == hl.BOOT_HOLD_INPUT_MODE
                 else BOOT_KINDS.get(target.kind, f"kind {target.kind}"))
        if target.profile is not None:
            boots += f", profile {target.profile}"
        held = (' + '.join(action_name(action, names) for action in hold.actions) if hold.source == hl.BOOT_HOLD_BUTTONS
                else 'pins ' + ' + '.join(f"GP{pin}" for pin in hold.pins))
        print(f"   {held:24} boots {boots}{' (locked)' if target.locked else ''}")


def check_boot_holds(records, modes) -> list:
    """Check that each input mode a boot hold boots is one page 2 lists.

    :param records: every page 4 record
    :param modes: every page 2 record
    :return: (what was checked, whether it held) for each check made
    """
    listed = {record.mode.value for record in modes}
    booted = [record.target.mode for record in records if record.target.kind == hl.BOOT_HOLD_INPUT_MODE]
    unlisted = sorted(set(booted) - listed)
    return [(f"the {len(booted)} input mode hold(s) boot modes page 2 lists" if not unlisted
             else f"boot holds into modes {unlisted} that page 2 does not list", not unlisted)]


def report_checks(checks) -> bool:
    """Print each check and say whether all held."""
    for what, held in checks:
        print(f"   check: {what}: {'coherent' if held else 'MISMATCH'}")
    return all(held for _, held in checks)


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
        state = device.read_record(hl.PAGE_STATE, hlp.STATE_GROUPS)
        describe_state(state)
        current = state.records[0].state.input_mode

        # page 3 as the bridge reads it before each claim, timed
        started = time.monotonic()
        bridge.names_before_claim(device)
        names_ms = round((time.monotonic() - started) * 1000)

        # what a host would decide from all of the above
        caps = hlp.negotiate(device)
        names = caps.names
        describe_names(names)
        print(f"   read in              {names_ms} ms, as the bridge reads it before each claim")
        describe_summary(caps.summary, caps.cache.counters)
        describe_lights(caps.lights, names)
        coherent = True
        if caps.controls is None:
            print(f"\npage {hl.PAGE_CONTROLS}, controls: this firmware does not list the page")
        else:
            describe_controls(caps.controls, names)
            coherent = report_checks(check_tables(caps.summary, caps.controls, caps.lights))
        modes = ()
        if hl.PAGE_INPUT_MODES in hello.pages:
            modes = device.read_table(hl.PAGE_INPUT_MODES).records
            describe_input_modes(modes, current)
            coherent = report_checks(check_input_modes(modes, caps.summary, current)) and coherent
        else:
            print(f"\npage {hl.PAGE_INPUT_MODES}, input modes: this firmware does not list the page")
        if hl.PAGE_BOOT_HOLDS in hello.pages:
            holds = device.read_table(hl.PAGE_BOOT_HOLDS).records
            describe_boot_holds(holds, names, modes)
            if modes:
                coherent = report_checks(check_boot_holds(holds, modes)) and coherent
        else:
            print(f"\npage {hl.PAGE_BOOT_HOLDS}, boot holds: this firmware does not list the page")
        print(f"\nas the bridge sees it: {caps.summary_line()}")
    finally:
        device.close()
    return 0 if coherent else 1


if __name__ == '__main__':
    sys.exit(main())
