# hlp-spice2x (spice2x to GP2040-CE Host Lighting bridge)
# Filename: test_hardware_checks.py
# Folder: tests
# https://github.com/djGLiTCH/hlp-spice2x
# SPDX-FileCopyrightText: (C) 2026 Jacob Simpson (DJ GLiTCH)
# SPDX-License-Identifier: GPL-3.0-or-later
# See LICENSES/GPL-3.0-or-later.txt at the repository root.

"""Test the hardware checks against the model board.

The hardware checks are run by eye against a real board. Run against the model
board they are held to the same wire as the bridge, so a check that sends
something the protocol refuses fails here first.
"""
import json

import pytest
from gp2040ce_hostlighting import protocol as hl
from gp2040ce_hostlighting.testing import (HELLO_COMMANDS, HELLO_PAGES, attach, boot_hold_record, control_record,
                                           input_mode_record, summary_record)

from hlp_spice2x import hlp
from hlp_spice2x import profile as profile_module

from . import hardware_bridge, hardware_probe, hardware_lights
from .boards import M_ULTRA_CONTROLS, M_ULTRA_LIGHTS, Board, on_clock

T = hlp.CONTROL_TARGETS
RED = (255, 0, 0)


def m_ultra(**kwargs):
    """Build the reference board."""
    return Board(lights=M_ULTRA_LIGHTS, controls=M_ULTRA_CONTROLS,
                 label='Haute42 COSMOX M Ultra', **kwargs)


def lights_check_on_clock(monkeypatch, board):
    """Run the lights check, the bridge and the library on the board's clock, so that a hold takes no real time."""
    on_clock(monkeypatch, board)
    monkeypatch.setattr(hardware_lights, 'time', hardware_lights.bridge.time)


def test_the_probe_prints_a_board_and_finds_it_coherent(monkeypatch, capsys):
    """Test the probe end to end: read-only, every section printed, exit status 0."""
    board = m_ultra()
    attach(monkeypatch, board)
    assert hardware_probe.main(['']) == 0
    out = capsys.readouterr().out
    assert 'HELLO: protocol 2.0, board ID 0011223344556677, reply queue limit 16' in out
    assert 'the bridge needs     nothing this board lacks' in out
    assert 'page 0, identity: Haute42 COSMOX M Ultra (v0.0.0-fake)' in out
    assert 'lights claimed by    nobody, claim generation 1' in out
    assert 'page 3, names: 36 action(s), 7 light kind(s)' in out
    assert 'controls             21 (16 lit, 5 unlit)' in out
    assert 'Up         x2   #3@LED3+1 on GP2, #12@LED12+1 on GP27   <-- more than one light' in out
    assert 'CASE       x30' in out
    assert 'GP14   action 32   BUTTON_PRESS_TURBO unlit' in out
    assert 'GP27   action 1    Up         lit (duplicate)' in out
    assert 'short names          38 of 38 on this firmware' in out
    assert 'USB host peripheral  disabled' in out
    assert 'page 2, input modes: 4 record(s), 3 accepted' in out
    assert ('0    XInput                 accepted lighting once a PC is detected, USB host optional   <-- now'
            in out)
    assert '5    Xbox One               refused  no lighting, USB host required' in out
    assert 'page 4, boot holds: 5 record(s)' in out
    assert 'S1 + S2 + Up             boots UF2 bootloader' in out
    assert 'B1                       boots PS4 (4)' in out
    assert ' ms, as the bridge reads it before each claim' in out
    assert out.count('coherent') == 7 and 'MISMATCH' not in out
    assert {request[0] for request in board.requests} == {hl.CMD_HELLO, hl.CMD_GET_PAGE}
    assert board.closed


def test_the_probe_passes_a_board_without_pages_2_and_4(monkeypatch, capsys):
    """Test that the probe, as the bridge, needs neither the input modes nor the boot holds."""
    attach(monkeypatch, m_ultra(pages=HELLO_PAGES - {hl.PAGE_INPUT_MODES, hl.PAGE_BOOT_HOLDS}))
    assert hardware_probe.main(['']) == 0
    out = capsys.readouterr().out
    assert 'page 2, input modes: this firmware does not list the page' in out
    assert 'page 4, boot holds: this firmware does not list the page' in out
    assert 'the bridge needs     nothing this board lacks' in out and 'MISMATCH' not in out


def no_lighting_now(board):
    """List the mode the board is in without the lighting interface."""
    board.modes[0] = input_mode_record(0, b'XInput')


def config_mode_listed(board):
    """List the config mode."""
    board.modes.append(input_mode_record(hl.INPUT_MODE_CONFIG, b'Config'))


def host_mode_accepted(board):
    """Accept Xbox One, which needs the USB host peripheral, while it is disabled."""
    board.modes[3] = input_mode_record(5, b'Xbox One', usb_host=hl.INPUT_MODE_USB_HOST_REQUIRED)


def hold_into_unlisted_mode(board):
    """Add a boot hold into a mode page 2 does not list."""
    board.boot_holds.append(boot_hold_record(hl.BOOT_HOLD_INPUT_MODE, 7, actions=(7,)))


@pytest.mark.parametrize('fault, line', [
    (no_lighting_now, 'the input mode now (0) is listed with the lighting interface: MISMATCH'),
    (config_mode_listed, 'the config mode (255) is not listed: MISMATCH'),
    (host_mode_accepted, '1 accepted mode(s) need the USB host peripheral, which is disabled: MISMATCH'),
    (hold_into_unlisted_mode, 'boot holds into modes [7] that page 2 does not list: MISMATCH')])
def test_the_probe_exits_non_zero_when_pages_2_and_4_disagree(monkeypatch, capsys, fault, line):
    """Test each check of page 2 against page 1 and the Summary, and of page 4 against page 2."""
    board = m_ultra()
    fault(board)
    attach(monkeypatch, board)
    assert hardware_probe.main(['']) == 1
    out = capsys.readouterr().out
    assert f"check: {line}" in out and out.count('MISMATCH') == 1


def test_the_probe_shows_an_action_page_3_does_not_name_by_its_value(monkeypatch, capsys):
    """Test that an action with no page 3 record and no profile name is shown by its value."""
    board = m_ultra()
    board.rename(hl.NAMES_GPIO_ACTION, 32)
    attach(monkeypatch, board)
    assert hardware_probe.main(['']) == 0
    assert 'GP14   action 32   action 32  unlit' in capsys.readouterr().out


def test_the_probe_names_each_value_as_the_boards_firmware_does(monkeypatch, capsys):
    """Test that a value page 3 names otherwise is shown by that name, and the short name it lacks is listed."""
    board = m_ultra()
    board.rename(hl.NAMES_GPIO_ACTION, 5, 'BUTTON_PRESS_B9')
    attach(monkeypatch, board)
    assert hardware_probe.main(['']) == 0
    out = capsys.readouterr().out
    assert 'short names          37 of 38 on this firmware, not B1' in out
    assert 'GP6    action 5    BUTTON_PRESS_B9 lit' in out


def test_the_probe_names_page_3_as_missing_on_a_board_without_it(monkeypatch, capsys):
    """Test that a board that does not list page 3 is shown as lacking it, then refused as the bridge refuses it."""
    board = m_ultra(pages=HELLO_PAGES - {hl.PAGE_NAMES})
    attach(monkeypatch, board)
    with pytest.raises(hl.HostLightingIncompatibleError, match='page 3'):
        hardware_probe.main([''])
    assert 'the bridge needs     page 3' in capsys.readouterr().out
    assert board.closed


def test_the_checks_pass_a_board_without_keyboard_mapping_changes(monkeypatch, capsys):
    """Test the probe and the lights check against a board at its default, which does not list SET_KEYBOARD_MAPPING."""
    commands = HELLO_COMMANDS - {hl.CMD_SET_KEYBOARD_MAPPING}
    attach(monkeypatch, m_ultra(commands=commands))
    assert hardware_probe.main(['']) == 0
    out = capsys.readouterr().out
    assert 'the bridge needs     nothing this board lacks' in out and '0x34' not in out
    board = m_ultra(commands=commands)
    attach(monkeypatch, board)
    lights_check_on_clock(monkeypatch, board)
    assert hardware_lights.main(['']) == 0
    assert '0 protocol-level failure(s)' in capsys.readouterr().out
    assert not board.sent(hl.CMD_SET_KEYBOARD_MAPPING)


@pytest.mark.parametrize('kind, names, name', [(hl.KIND_CASE, {'LightType': {1: 'Case'}}, 'CASE'),
                                               (hl.KIND_BUTTON, {'LightType': {0: 'ActionButton'}}, 'button'),
                                               (7, {'LightType': {7: 'NewLight'}}, 'NewLight'),
                                               (7, {}, 'kind 7')])
def test_a_light_kind_is_named_by_a_profile_then_the_probe_then_the_firmware(kind, names, name):
    """Test the order kind_name() names a light kind in."""
    assert hardware_probe.kind_name(kind, names) == name


def test_the_probe_exits_non_zero_when_the_tables_disagree(monkeypatch, capsys):
    """Test that a Summary count the records do not bear out fails the probe."""
    board = m_ultra()
    attach(monkeypatch, board)
    board.summary = [summary_record(led_extent=46, controls=21, lit_controls=16, unlit_controls=5,
                                    lights=46)[0], board.summary[1]]
    board.lights = board.lights[:-1]
    assert hardware_probe.main(['']) == 1
    assert 'check: 46 lights, 45 records: MISMATCH' in capsys.readouterr().out


def test_the_probe_with_no_board_says_where_to_get_firmware(monkeypatch, capsys):
    """Test that an empty enumeration fails and points at the firmware builds."""
    attach(monkeypatch)
    assert hardware_probe.main(['']) == 1
    assert 'releases/tag/HLP_v2.0-rc14' in capsys.readouterr().out


def test_the_table_checks_hold_for_a_coherent_board():
    """Test the three checks against the reference board's pages."""
    caps = hlp.negotiate(m_ultra().open())
    checks = hardware_probe.check_tables(caps.summary, caps.controls, caps.lights)
    assert [held for _, held in checks] == [True, True, True]
    assert checks[0][0] == '16 lit + 5 unlit = 21 controls, 16 of 21 records marked lit'


def test_the_probe_finds_a_board_with_a_light_on_a_hall_effect_trigger_coherent(monkeypatch, capsys):
    """Test that a light whose owner is a trigger key joins its control, and is named by that key."""
    board = Board(lights=[('B1', 4), ('B2', 5)], controls=[(0x83, 5), (6, 6), (0xC1, 7)])
    attach(monkeypatch, board)
    assert hardware_probe.main(['']) == 0
    out = capsys.readouterr().out
    assert 'B1         x1   #0@LED4+1 on HE3' in out
    assert 'HE3    action 5    B1         lit' in out
    assert 'PCF1   action 7    B3         unlit' in out
    assert out.count('coherent') == 7 and 'MISMATCH' not in out


def test_a_lit_control_with_no_light_behind_it_is_a_mismatch():
    """Test that the join between lit controls and the Lights page's owner pins is checked."""
    board = m_ultra()
    # GP16 (S1) is marked lit, though no light is tied to it, and the Summary still counts 16 lit
    board.controls = [control_record(16, 13) if control[0][0] == 16 else control for control in board.controls]
    caps = hlp.negotiate(board.open())
    checks = hardware_probe.check_tables(caps.summary, caps.controls, caps.lights)
    assert [held for _, held in checks] == [False, True, False]
    assert 'lit keys' in checks[2][0]


@pytest.mark.parametrize('key, name', [(5, 'GP5'), (0x30, 'key 0x30'), (0x83, 'HE3'),
                                       (0xA5, 'key 0xA5'), (0xC0, 'PCF0'), (0xE1, 'key 0xE1')])
def test_a_controls_key_is_named_by_its_source(key, name):
    """Test the key ranges: GPIO pin, Hall-effect trigger, expander pin, and a key with no source."""
    assert hardware_probe.key_name(key) == name


def test_the_lights_check_names_each_control_as_the_boards_firmware_does(monkeypatch, capsys):
    """Test that a phase names a control by page 3, so it names the control it lights."""
    board = m_ultra()
    board.rename(hl.NAMES_GPIO_ACTION, 3, 'BUTTON_PRESS_E13')
    attach(monkeypatch, board)
    lights_check_on_clock(monkeypatch, board)
    assert hardware_lights.main(['']) == 0
    out = capsys.readouterr().out
    assert '  BUTTON_PRESS_E13\n' in out and '  Left\n' not in out


def test_the_lights_check_runs_every_phase_and_hands_the_lights_back(monkeypatch, capsys):
    """Test the lights check end to end against the reference board, on a clock that does not wait."""
    board = m_ultra()
    attach(monkeypatch, board)
    lights_check_on_clock(monkeypatch, board)
    assert hardware_lights.main(['']) == 0
    out = capsys.readouterr().out
    assert all(f"PHASE {number}:" in out for number in range(1, 12))
    assert 'SKIPPED' not in out and 'FAIL' not in out
    assert '0 protocol-level failure(s)' in out
    assert 'claim given up, on-board animations restored' in out
    assert board.holder_token == 0 and board.closed
    # phase 7, third step: the control red, its second light explicitly black
    assert {3: RED, 12: (0, 0, 0)} in board.frames
    # phase 2 lit every light alone
    assert all({led: (255, 255, 255)} in board.frames for led in range(46))
    # phases 10 and 11 changed the takeover mode and the timeout
    assert [request[3:6] for request in board.sent(hl.CMD_CONFIGURE)] == [
        bytes.fromhex('00 10 27'), bytes.fromhex('01 10 27'), bytes.fromhex('00 d0 07')]


def test_the_lights_check_skips_what_a_plain_board_cannot_show(monkeypatch, capsys):
    """Test that phases needing a multi-light control or a case strip say so and are skipped."""
    board = Board()
    attach(monkeypatch, board)
    lights_check_on_clock(monkeypatch, board)
    assert hardware_lights.main(['']) == 0
    out = capsys.readouterr().out
    assert out.count('SKIPPED') == 4
    assert 'this board reports no case lights' in out


def test_the_lights_check_stops_when_another_application_holds_the_lights(monkeypatch):
    """Test that the lights check takes nothing over, and leaves the other application's claim alone."""
    board = m_ultra()
    board.take_over(name=b'MESH')
    attach(monkeypatch, board)
    with pytest.raises(hl.HostLightingError, match="'MESH' holds the lights"):
        hardware_lights.main([''])
    assert board.holder_token != 0 and not board.sent(hl.CMD_STAGE) and board.closed


def test_the_lights_check_reports_a_frame_the_board_refused(monkeypatch, capsys):
    """Test that losing the claim partway is a reported failure and a non-zero exit."""
    board = m_ultra()
    attach(monkeypatch, board)
    lights_check_on_clock(monkeypatch, board)

    def lose_the_claim_at_the_tenth_frame(fake, request):
        if request[0] == hl.CMD_STAGE and len(fake.frames) == 10:
            fake.hooks.clear()
            fake.take_over()

    board.hooks.append(lose_the_claim_at_the_tenth_frame)
    with pytest.raises(hl.HostLightingError, match='took the lights over'):
        hardware_lights.main([''])
    assert "FAIL frame refused: command 0x10 (STAGE) rejected: CLAIMED by 'Other'" in capsys.readouterr().out


def test_the_end_to_end_check_writes_a_profile_the_bridge_can_load(monkeypatch, tmp_path):
    """Test that the generated profile names this board's controls and loads cleanly."""
    attach(monkeypatch, m_ultra())
    monkeypatch.setattr(hardware_bridge, 'PROFILE_PATH', tmp_path / 'profile.json')
    chase, strip, strip_lights, multi, _ = hardware_bridge.describe_board('')
    assert len(chase) == 14 and (strip, strip_lights) == (T['CASE'], 30)
    assert multi == T['UP']
    lights, strip_names = hardware_bridge.build_profile(chase, strip, strip_lights, multi)
    assert len(strip_names) == 30 and 'Second light' in lights and 'Unmapped Lamp' in lights
    written = json.loads((tmp_path / 'profile.json').read_text(encoding='utf-8'))['lights']
    assert written['Chase Left'] == {'button': 'Left', 'colour': 'FF0000'}
    assert written['Strip 4']['button'] == 'CASE' and written['Strip 4']['index'] == 4
    assert written['Second light'] == {'button': 'Up', 'index': 1, 'colour': '00FFFF'}
    profile, warnings = profile_module.load(str(tmp_path / 'profile.json'))
    assert len(profile) == 45 and not warnings
