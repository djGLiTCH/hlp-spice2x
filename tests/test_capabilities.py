"""Test what the bridge agrees to at connect.

A host confirms the protocol major and then gates on HELLO's capability
bitmaps and on nothing else. The minor version decides nothing.

SPDX-FileCopyrightText: (C) 2026 Jacob Simpson
SPDX-License-Identifier: GPL-3.0-or-later
"""
import dataclasses

import pytest
from gp2040ce_hostlighting import protocol as hl
from gp2040ce_hostlighting.testing import (HELLO_ADDRESSING_MODES, HELLO_COMMANDS, HELLO_EVENTS, HELLO_PAGES,
                                           HELLO_PIXEL_FORMATS, HELLO_REQUEST_FLAGS)

from hlp_spice2x import bridge, hlp
from hlp_spice2x.__main__ import build_parser

from .boards import M_ULTRA_CONTROLS, M_ULTRA_LIGHTS, Board, OneShotConnection

T = hlp.CONTROL_TARGETS
RED = (255, 0, 0)
PROFILE = {'P1 Button 1': (('button', T['B1']), RED)}
# HELLO as the protocol reference's worked example gives it, captured from a Haute42 COSMOX
HELLO = bytes.fromhex('81 41 00 47 50 48 4c 02 00 433031343539302E 10 7f'
                      '3e 00 0f 00 03 00 0f 00 00 00 00 00 00 00 03 00 0f 1f 00 00 f3 00 07 00 01 00 00 00'
                      ).ljust(64, b'\x00')


def negotiated(addressing=(hl.ADDRESSING_ACTION,), **kwargs):
    """Negotiate against a board built from the given description."""
    return hlp.negotiate(Board(**kwargs).open(), addressing)


def run_against(board, profile=PROFILE, **kwargs):
    """Run the bridge for one poll against a board and return what it reported."""
    lines = []
    bridge.run(OneShotConnection({name: 1.0 for name in profile}), profile, device=board.open(),
               report=lines.append, **kwargs)
    return '\n'.join(lines)


def test_a_2_0_board_is_read_at_connect():
    """Test that negotiating reads the Summary, the lights and the controls under one token."""
    board = Board(lights=M_ULTRA_LIGHTS, controls=M_ULTRA_CONTROLS, colour_format=2)
    caps = hlp.negotiate(board.open(), {hl.ADDRESSING_ACTION})
    assert caps.connected and caps.token == board.token
    assert (caps.led_extent, caps.led_limit, caps.render_hz) == (46, 100, 40)
    assert len(caps.lights) == 46 and len(caps.controls) == 21
    assert caps.white_channel and caps.host_white and caps.events
    assert caps.summary_line() == ("board speaks HLP v2.0: 46 lights on 46 LEDs, renders at "
                                   "40 Hz, GRBW chain (white channel: yes), state changes by event")


def test_the_30_byte_summary_is_read_end_to_end():
    """Test that each Summary field the bridge uses is read from where page 8 puts it.

    Every field is set apart from its neighbours, so a field read at another
    offset would show another field's value.
    """
    board = Board(lights=M_ULTRA_LIGHTS, controls=M_ULTRA_CONTROLS, colour_format=3, render_hz=50,
                  led_limit=90)
    device = board.open()
    caps = hlp.negotiate(device)
    summary = caps.summary
    assert (caps.led_extent, caps.led_limit, caps.render_hz) == (46, 90, 50)
    assert (summary.colour_format, caps.white_channel) == (3, True)
    assert (summary.controls, summary.lit_controls, summary.unlit_controls, summary.lights) == (21, 16, 5, 46)
    assert "RGBW chain (white channel: yes)" in caps.summary_line()
    # the Lights page's change counter moves, and only the Lights page is read again
    board.rewire(lights=M_ULTRA_LIGHTS[:45])
    board.requests.clear()
    assert caps.refresh(device)
    assert {request[3] for request in board.sent(hl.CMD_GET_PAGE)} == {hl.PAGE_SUMMARY, hl.PAGE_LIGHTS}
    assert len(caps.lights) == 45 and caps.summary.lights == 45


@pytest.mark.parametrize('colour_format, flag, white', [(0, 0, False), (2, 1, True), (2, 0, False), (0, 1, True)])
def test_the_white_channel_is_the_summarys_flag(colour_format, flag, white):
    """Test that the white channel comes from the Summary's flag, whatever the colour format says."""
    caps = negotiated(colour_format=colour_format, white_channel=flag)
    assert caps.white_channel is white and caps.host_white is white
    assert f"white channel: {'yes' if white else 'no'}" in caps.summary_line()


def test_only_the_groups_the_bridge_uses_are_asked_for():
    """Test that every page read names its field groups."""
    board = Board()
    hlp.negotiate(board.open())
    masks = {(request[3], int.from_bytes(request[10:12], 'little'))
             for request in board.sent(hl.CMD_GET_PAGE)}
    assert masks == {(hl.PAGE_SUMMARY, 0x03), (hl.PAGE_LIGHTS, 0x07), (hl.PAGE_CONTROLS, 0x01)}


def test_this_firmware_lacks_nothing_the_bridge_needs():
    """Test the capability check against the worked HELLO, for every addressing mode used."""
    assert hlp.lacking(hl.decode_hello(HELLO), {0, 4, 6, 7}) == []


def test_each_missing_capability_is_named():
    """Test that what a board does not list is named by kind and number."""
    hello = hl.decode_hello(HELLO)
    hello = dataclasses.replace(hello, commands=hello.commands - {hl.CMD_CLAIM},
                                request_flags=hello.request_flags - {1}, pages=hello.pages - {hl.PAGE_LIGHTS},
                                addressing_modes=hello.addressing_modes - {hl.ADDRESSING_ACTION})
    assert hlp.lacking(hello, {hl.ADDRESSING_ACTION}) == [
        'command 0x03 (CLAIM)', 'request flag bit 1', 'page 11', 'addressing mode 6 (action)']


@pytest.mark.parametrize('minor', [0, 1, 7, 255])
def test_the_minor_version_gates_nothing(minor):
    """Test that a board of any 2.x minor is driven by what it lists, not by its number."""
    board = Board(version=(2, minor))
    assert f"HLP v2.{minor}" in run_against(board)
    assert board.frames == [{4: RED}]


@pytest.mark.parametrize('major', [0, 1, 3])
def test_another_major_is_refused_and_says_both_versions(major):
    """Test that a board answering HELLO with another major is not driven.

    The usage a board is found by is its major, so this is a board that says
    one thing in its descriptor and another in HELLO. Nothing is sent to it
    after HELLO.
    """
    board = Board(version=(major, 4))
    with pytest.raises(hl.HostLightingIncompatibleError) as caught:
        hlp.negotiate(board.open())
    assert f"protocol {major}.4" in str(caught.value) and 'speaks 2.x' in str(caught.value)
    assert [request[0] for request in board.requests] == [hl.CMD_HELLO]


def test_force_does_not_override_a_refused_major():
    """Test that --force takes lights over from an application, and nothing else."""
    board = Board(version=(3, 0))
    with pytest.raises(hl.HostLightingIncompatibleError):
        run_against(board, force=True)
    assert 'lights over' in ' '.join(build_parser().format_help().split())


def test_something_that_is_not_a_host_lighting_interface_is_reported_as_such():
    """Test that a reply without the GPHL magic is not read as a board."""
    with pytest.raises(hl.HostLightingIncompatibleError, match='did not answer as a Host Lighting one'):
        negotiated(magic=b'NOPE')
    with pytest.raises(hl.HostLightingIncompatibleError, match='did not answer as a Host Lighting one'):
        run_against(Board(magic=b'NOPE'))


@pytest.mark.parametrize('lacks, named', [
    ({'commands': HELLO_COMMANDS - {hl.CMD_CLAIM}}, 'command 0x03'),
    ({'commands': HELLO_COMMANDS - {hl.CMD_STAGE}}, 'command 0x10'),
    ({'commands': HELLO_COMMANDS - {hl.CMD_CLEAR}}, 'command 0x13'),
    ({'request_flags': HELLO_REQUEST_FLAGS - {0}}, 'request flag bit 0'),
    ({'request_flags': HELLO_REQUEST_FLAGS - {1}}, 'request flag bit 1'),
    ({'request_flags': HELLO_REQUEST_FLAGS - {2}}, 'request flag bit 2'),
    ({'request_flags': HELLO_REQUEST_FLAGS - {4, 5, 6}},
     'request flag bit 4, request flag bit 5, request flag bit 6'),
    ({'pages': HELLO_PAGES - {hl.PAGE_SUMMARY}}, 'page 8'),
    ({'pages': HELLO_PAGES - {hl.PAGE_LIGHTS}}, 'page 11'),
    ({'pixel_formats': HELLO_PIXEL_FORMATS - {0}}, 'pixel format 0'),
    ({'addressing_modes': {0, 1, 4, 5, 7}}, 'addressing mode 6'),
])
def test_a_board_that_does_not_list_what_the_bridge_needs_is_refused(lacks, named):
    """Test that each missing capability refuses the connect and is named, whatever the minor."""
    board = Board(version=(2, 9), **lacks)
    with pytest.raises(hl.HostLightingIncompatibleError) as caught:
        hlp.negotiate(board.open(), {hl.ADDRESSING_ACTION})
    assert named in str(caught.value) and 'protocol 2.9' in str(caught.value)
    assert [request[0] for request in board.requests] == [hl.CMD_HELLO]


@pytest.mark.parametrize('profile, mode', [
    ({'a': (('button', T['CASE']), RED)}, 7),
    ({'a': (('light', T['UP'], 0), RED)}, 4),
    ({'a': (('range', 0, 4), RED)}, 0),
])
def test_an_addressing_mode_is_needed_only_by_a_profile_that_uses_it(profile, mode):
    """Test that a profile is refused for a mode the board lacks, and other profiles are not."""
    board = Board(addressing_modes=HELLO_ADDRESSING_MODES - {mode})
    with pytest.raises(hl.HostLightingIncompatibleError, match=f"addressing mode {mode}"):
        run_against(board, profile)
    assert 'streaming' in run_against(Board(addressing_modes=HELLO_ADDRESSING_MODES - {mode}))


def test_a_board_without_a_controls_page_cannot_say_a_control_is_absent():
    """Test that without page 10 a control with no light is reported unlit, not absent."""
    profile = dict(PROFILE, e=(('button', T['E1']), RED))
    board = Board(pages=HELLO_PAGES - {hl.PAGE_CONTROLS})
    text = run_against(board, profile)
    assert '1 mapped control(s) have no LED on this board: E1' in text
    assert 'not on this board' not in text
    assert all(request[3] != hl.PAGE_CONTROLS for request in board.sent(hl.CMD_GET_PAGE))
    assert '1 mapped control(s) are not on this board: E1' in run_against(Board(), profile)


def test_a_board_without_an_identity_page_is_still_driven():
    """Test that page 0 is for display only."""
    board = Board(pages=HELLO_PAGES - {hl.PAGE_IDENTITY})
    assert 'board: ? (?)' in run_against(board)
    assert board.frames == [{4: RED}]


@pytest.mark.parametrize('lacks', [{'events': HELLO_EVENTS - {0}},
                                   {'commands': HELLO_COMMANDS - {hl.CMD_SUBSCRIBE}}])
def test_a_board_without_events_is_polled_instead(lacks):
    """Test that without STATE_CHANGED SUBSCRIBE is never sent."""
    board = Board(**lacks)
    assert 'state changes by poll' in run_against(board)
    assert not board.sent(hl.CMD_SUBSCRIBE)
    assert board.frames == [{4: RED}]


def test_a_board_with_events_is_subscribed_then_read_once():
    """Test that page 1 is read after SUBSCRIBE, since a change before it raises no event."""
    board = Board()
    run_against(board)
    order = [(request[0], request[3]) for request in board.requests]
    subscribed = order.index((hl.CMD_SUBSCRIBE, 0x01))
    assert order[subscribed + 1] == (hl.CMD_GET_PAGE, hl.PAGE_STATE)
    assert order.index((hl.CMD_CLAIM, hl.CLAIM_TAKE)) < order.index((hl.CMD_CONFIGURE, 0))
    assert order.index((hl.CMD_CONFIGURE, 0)) < subscribed


def test_a_configuration_that_changed_during_the_handshake_is_read_again():
    """Test that the token read after subscribing is checked against the one the pages carry.

    The profile names B1 by index, so it is staged by the ordinal the Lights
    page gave. A change before the subscription raises no event, and without
    the check the ordinal read before the change would be staged.
    """
    board = Board()

    def move_b1_once(fake, request):
        if request[0] == hl.CMD_CLAIM:
            fake.hooks.clear()
            fake.rewire(lights=[('B1', 20)])

    board.hooks.append(move_b1_once)
    run_against(board, {'P1 Button 1': (('light', T['B1'], 0), RED)})
    assert board.frames == [{20: RED}]


def test_running_with_no_board_reads_nothing():
    """Test that a dry run answers every capability question with no."""
    caps = hlp.HostLightingCapabilities.absent()
    assert not caps.connected and not caps.events and not caps.host_white
    assert caps.render_hz is None and caps.lights_of(T['UP']) == []
    assert not caps.lacks_control(T['UP'])
    assert 'no board' in caps.summary_line()


def test_a_configuration_that_keeps_changing_is_given_up_on():
    """Test that reading pages under a token that never holds still comes to an end."""
    board = Board()
    flip = [[('B1', 1)], [('B1', 2)]]

    def change_under_every_walk(fake, request):
        if request[0] == hl.CMD_GET_PAGE and request[3] == hl.PAGE_LIGHTS:
            flip.reverse()
            fake.rewire(lights=flip[0])

    board.hooks.append(change_under_every_walk)
    with pytest.raises(hl.HostLightingError, match='kept changing'):
        hlp.negotiate(board.open())
    assert len([request for request in board.sent(hl.CMD_GET_PAGE)
                if request[3] == hl.PAGE_SUMMARY]) == 3


def test_only_the_pages_whose_counter_moved_are_read_again():
    """Test that a refresh reads the Summary, then only the pages its change counters name."""
    board = Board(lights=M_ULTRA_LIGHTS, controls=M_ULTRA_CONTROLS)
    device = board.open()
    caps = hlp.negotiate(device)
    board.requests.clear()
    caps.refresh(device)
    assert [request[3] for request in board.sent(hl.CMD_GET_PAGE)] == [hl.PAGE_SUMMARY]

    board.rewire(controls=[(pin, 2 if pin == 27 else action) for pin, action in M_ULTRA_CONTROLS])
    board.requests.clear()
    caps.refresh(device)
    assert {request[3] for request in board.sent(hl.CMD_GET_PAGE)} == {
        hl.PAGE_SUMMARY, hl.PAGE_CONTROLS, hl.PAGE_LIGHTS}
    assert caps.token == board.token
    assert [record.ordinal for record in caps.lights_of(T['DOWN'])] == [1, 12]


def test_a_light_past_the_addressable_limit_is_not_one_stage_reaches():
    """Test that a light whose LEDs all lie at or past the limit is left out, as STAGE skips it."""
    caps = negotiated(lights=[('Up', 3), ('Up', 100), ('Up', 99, 4)], led_limit=100)
    assert [record.leds.first_led for record in caps.lights_of(T['UP'])] == [3, 99]
    assert caps.led_extent == 100


def test_a_control_is_absent_only_when_the_controls_page_leaves_it_out():
    """Test the split between a control that is wired but unlit and one the board does not have."""
    caps = negotiated(lights=M_ULTRA_LIGHTS, controls=M_ULTRA_CONTROLS)
    assert not caps.lights_of(T['S1']) and not caps.lacks_control(T['S1'])
    assert not caps.lights_of(T['E1']) and caps.lacks_control(T['E1'])


def test_a_kind_of_light_is_never_an_absent_control():
    """Test that the Controls page is not asked about a kind, whose number is not an action.

    PLED1 is kind 3. This board has no control with action 3, so reading the
    kind as an action would report the player LED as a control it lacks.
    """
    caps = negotiated(lights=[('B1', 0)])
    assert caps.lacks_control(T['LEFT'])
    assert not caps.lacks_control(T['PLED1']) and not caps.lacks_control(T['CASE'])


def test_lights_that_change_under_their_own_walk_are_read_again_whole():
    """Test that the Lights walk names the token it expects, with no other page to catch a change.

    Without the Controls page the Lights walk is the only one. A walk that did
    not name its token would return the first records of one configuration
    followed by the rest of another.
    """
    board = Board(lights=[('B1', led) for led in range(12)], pages=HELLO_PAGES - {hl.PAGE_CONTROLS})

    def change_at_the_second_read(fake, request):
        if request[0] == hl.CMD_GET_PAGE and request[3] == hl.PAGE_LIGHTS and request[4]:
            fake.hooks.clear()
            fake.rewire(lights=[('B2', led) for led in range(12)])

    board.hooks.append(change_at_the_second_read)
    caps = hlp.negotiate(board.open())
    assert caps.token == board.token
    assert caps.lights_of(T['B1']) == [] and len(caps.lights_of(T['B2'])) == 12
    assert len([request for request in board.sent(hl.CMD_GET_PAGE)
                if request[3] == hl.PAGE_SUMMARY]) == 2
