"""Test how a frame reaches the board's lights.

A control name is staged by its action and a case, turbo or player light by
its kind, and the board resolves either to every light carrying it. One light
of a control is staged by its ordinal, and a raw range as an LED run. Where two
of these reach the same LED the order they are staged in decides the colour.

Most of these assert on the frame the fake board ends up showing, LED by LED.
The few that read the requests are about how a frame travels, which report is
answered and which carries the commit.

SPDX-FileCopyrightText: (C) 2026 Jacob Simpson
SPDX-License-Identifier: GPL-3.0-or-later
"""
import pytest
from gp2040ce_hostlighting import protocol as hl
from gp2040ce_hostlighting.testing import HELLO_PIXEL_FORMATS

from hlp_spice2x import bridge, hlp
from hlp_spice2x import profile as profile_module

from .boards import M_ULTRA_CONTROLS, M_ULTRA_LIGHTS, Board, OneShotConnection

T = hlp.CONTROL_TARGETS
RED = (255, 0, 0)
GREEN = (0, 255, 0)
BLUE = (0, 0, 255)
WHITE = (255, 255, 255)
UP, L3, B1, CASE = T['UP'], T['L3'], T['B1'], T['CASE']


def m_ultra(**kwargs):
    """Build the reference board."""
    return Board(lights=M_ULTRA_LIGHTS, controls=M_ULTRA_CONTROLS, **kwargs)


def publish(board, profile, states=None, clear_first=False):
    """Resolve a profile against a board, publish one frame and return the result."""
    device = board.open()
    caps = hlp.negotiate(device, bridge.addressing_used(profile))
    staging = bridge.staging_for(profile, caps)
    frame = profile_module.resolve_frame(states or {name: 1.0 for name in profile}, profile)
    return bridge.send_frame(device, frame, staging, clear_first=clear_first)


def shown(board) -> dict:
    """Give the frame the board published last, LED by LED, or nothing before the first."""
    return board.frames[-1] if board.frames else {}


def run_against(board, profile, **kwargs):
    """Run the bridge for one poll against a board and return what it reported."""
    lines = []
    bridge.run(OneShotConnection({name: 1.0 for name in profile}), profile, device=board.open(),
               report=lines.append, **kwargs)
    return '\n'.join(lines)


def test_a_control_name_lights_every_light_of_the_control():
    """Test that Up, which this board wires to two buttons, lights both from one entry."""
    board = m_ultra()
    assert publish(board, {'up': (('button', UP), RED)})
    assert shown(board) == {3: RED, 12: RED}
    stage = board.sent(hl.CMD_STAGE)
    assert len(stage) == 1 and stage[0][3:10] == bytes.fromhex('06 01 01 00 ff 00 00')


def test_a_frame_is_one_answered_report_carrying_the_commit():
    """Test that a frame's only reply is to its last report, which also publishes it."""
    board = m_ultra()
    assert publish(board, {'up': (('button', UP), RED)})
    assert [(request[0], request[2] & 0x0F) for request in board.requests[-1:]] == [
        (hl.CMD_STAGE, hl.FLAG_COMMIT_AFTER)]
    assert not board.sent(hl.CMD_COMMIT)
    assert board.live


def test_every_report_but_the_last_is_sent_without_a_reply():
    """Test a frame of several reports: NO_REPLY on each but the last, COMMIT_AFTER on that alone."""
    board = m_ultra()
    profile = {'case': (('button', CASE), BLUE), 'up': (('button', UP), RED),
               'l3': (('light', L3, 1), GREEN), 'strip': (('range', 16, 30), WHITE)}
    assert publish(board, profile)
    flags = [request[2] & 0x0F for request in board.sent(hl.CMD_STAGE)]
    assert flags == [hl.FLAG_NO_REPLY] * 4 + [hl.FLAG_COMMIT_AFTER]
    assert not board.queue and not board.outbox and len(board.frames) == 1


def test_the_passes_go_kind_then_name_then_light_then_range():
    """Test the order the passes are emitted in, which decides who wins a shared LED."""
    board = m_ultra()
    profile = {'strip': (('range', 16, 2), WHITE), 'l3': (('light', L3, 1), GREEN),
               'up': (('button', UP), RED), 'case': (('button', CASE), BLUE)}
    assert publish(board, profile)
    assert [request[3] & 0x0F for request in board.sent(hl.CMD_STAGE)] == [
        hl.ADDRESSING_KIND, hl.ADDRESSING_ACTION, hl.ADDRESSING_LIGHT, hl.ADDRESSING_LED_RUN]


def test_naming_one_light_beats_reaching_it_through_its_control():
    """Test that an indexed entry wins over the control name, whatever order the profile lists them."""
    for profile in ({'one': (('light', UP, 1), GREEN), 'both': (('button', UP), RED)},
                    {'both': (('button', UP), RED), 'one': (('light', UP, 1), GREEN)}):
        board = m_ultra()
        assert publish(board, profile)
        assert shown(board) == {3: RED, 12: GREEN}


def test_an_explicit_entry_wins_even_when_it_asks_for_black():
    """Test that one light of a control can be turned off while the control is lit."""
    board = m_ultra()
    profile = {'both': (('button', UP), RED), 'one': (('light', UP, 1), GREEN)}
    assert publish(board, profile, {'both': 1.0, 'one': 0.0})
    assert shown(board) == {3: RED, 12: (0, 0, 0)}


def test_a_raw_range_beats_the_case_strip_it_lies_in():
    """Test that a range staged over part of the case strip wins there and nowhere else."""
    board = m_ultra()
    assert publish(board, {'case': (('button', CASE), BLUE), 'part': (('range', 20, 3), RED)})
    assert {led: colour for led, colour in shown(board).items() if colour == RED} == {
        20: RED, 21: RED, 22: RED}
    assert sum(1 for colour in shown(board).values() if colour == BLUE) == 27


def test_an_index_counts_a_controls_lights_in_ordinal_order():
    """Test that index n is the nth light the Lights page gives the control."""
    board = m_ultra()
    assert publish(board, {'a': (('light', L3, 0), RED), 'b': (('light', L3, 1), GREEN)})
    assert shown(board) == {13: RED, 15: GREEN}
    assert board.sent(hl.CMD_STAGE)[0][3:15] == bytes.fromhex('04 02 0d 00 ff 00 00 0f 00 00 ff 00')


def test_an_index_reaches_one_light_of_the_case_strip():
    """Test that a kind can be indexed too, with the fifth case light at LED 20."""
    board = m_ultra()
    assert publish(board, {'a': (('light', CASE, 4), RED)})
    assert shown(board) == {20: RED}


def test_an_index_past_a_controls_lights_is_refused_at_startup():
    """Test that an index the board cannot honour names the control and how many lights it has."""
    caps = hlp.negotiate(m_ultra().open())
    with pytest.raises(profile_module.ProfileError, match=r"Up\[2\] is out of range.*2 lights"):
        bridge.staging_for({'a': (('light', UP, 2), RED)}, caps)
    with pytest.raises(profile_module.ProfileError, match=r"B1\[1\] is out of range.*1 light$"):
        bridge.staging_for({'a': (('light', B1, 1), RED)}, caps)


def test_an_index_the_board_no_longer_has_is_held_back_mid_run():
    """Test that with a problems list the entry is named and staged as nothing, not raised."""
    caps = hlp.negotiate(m_ultra().open())
    problems = []
    staging = bridge.staging_for({'a': (('light', UP, 2), RED), 'b': (('button', UP), RED)},
                                 caps, problems)
    assert staging[('light', UP, 2)] is None
    assert staging[('button', UP)] == (('entry',) + UP, False)
    assert len(problems) == 1 and 'Up[2]' in problems[0]


def test_a_held_back_entry_stages_nothing_and_the_rest_still_light():
    """Test that send_frame skips a target staged as nothing."""
    board = m_ultra()
    device = board.open()
    staging = {('light', UP, 2): None, ('button', B1): (('entry',) + B1, False)}
    assert bridge.send_frame(device, {('light', UP, 2): RED, ('button', B1): GREEN}, staging)
    assert shown(board) == {8: GREEN}


def test_a_target_never_resolved_is_an_error_not_a_dark_light():
    """Test that a frame target with no staging raises."""
    with pytest.raises(ValueError, match='never resolved'):
        bridge.send_frame(m_ultra().open(), {('button', B1): RED}, {})


def test_a_range_is_staged_as_an_led_run_split_at_the_capacity():
    """Test that 30 LEDs of one colour go as runs of 19 and 11."""
    board = m_ultra()
    assert publish(board, {'strip': (('range', 16, 30), RED)})
    assert [(request[4], request[6]) for request in board.sent(hl.CMD_STAGE)] == [(16, 19), (35, 11)]
    assert shown(board) == {led: RED for led in range(16, 46)}


def test_a_range_is_held_to_the_addressable_limit():
    """Test that a run reaching past the limit is cut to it.

    Sent whole it would be an invalid argument that stages none of it, and sent
    without a reply nothing would say so.
    """
    board = Board(lights=[('B1', 0)], led_limit=20)
    profile = {'over': (('range', 15, 10), RED), 'gone': (('range', 20, 5), GREEN)}
    assert publish(board, profile)
    assert shown(board) == {led: RED for led in range(15, 20)}
    assert len(board.sent(hl.CMD_STAGE)) == 1


def test_a_range_past_the_extent_is_warned_about_once():
    """Test the startup warning for a range reaching past the board's last LED."""
    text = run_against(m_ultra(), {'a': (('range', 40, 10), RED), 'b': (('range', 0, 46), RED)})
    assert "1 raw range(s) reach past this board's 46 LEDs" in text and '[40, 10]' in text


def test_a_frame_with_fewer_targets_clears_first():
    """Test that CLEAR goes ahead of the staging when asked, without a reply of its own."""
    board = m_ultra()
    device = board.open()
    staging = {('button', UP): (('entry',) + UP, False), ('button', B1): (('entry',) + B1, False)}
    assert bridge.send_frame(device, {('button', UP): RED, ('button', B1): RED}, staging)
    assert shown(board) == {3: RED, 12: RED, 8: RED}
    assert bridge.send_frame(device, {('button', B1): GREEN}, staging, clear_first=True)
    assert shown(board) == {8: GREEN}
    clear = board.sent(hl.CMD_CLEAR)
    assert len(clear) == 1 and clear[0][2] & 0x0F == hl.FLAG_NO_REPLY


def test_an_empty_frame_is_still_published():
    """Test that a frame with no targets clears and commits in one answered report."""
    board = m_ultra()
    device = board.open()
    staging = {('button', B1): (('entry',) + B1, False)}
    assert bridge.send_frame(device, {('button', B1): RED}, staging)
    assert bridge.send_frame(device, {}, staging)
    assert shown(board) == {} and board.live and len(board.frames) == 2
    assert board.requests[-1][0] == hl.CMD_CLEAR
    assert board.requests[-1][2] & 0x0F == hl.FLAG_COMMIT_AFTER


def test_a_light_at_zero_is_staged_black_not_left_out():
    """Test that a mapped light the game reports off is staged black."""
    board = m_ultra()
    assert publish(board, {'b1': (('button', B1), RED)}, {'b1': 0.0})
    assert shown(board) == {8: (0, 0, 0)}


def test_many_controls_are_split_across_reports_at_the_capacity():
    """Test that sixteen controls by action go as eleven and five, all lit."""
    board = Board()
    profile = {name: (('button', T[name.upper()]), RED) for name in hlp.ACTION_NAMES[:16]}
    assert publish(board, profile)
    assert [request[4] for request in board.sent(hl.CMD_STAGE)] == [11, 5]
    assert shown(board) == {led: RED for led in range(16)}


def test_a_refused_frame_is_reported_with_the_holder():
    """Test that a frame sent under an outdated generation comes back naming who holds the claim."""
    board = m_ultra()
    device = board.open()
    device.claim(hl.CLAIM_TAKE, 0xA1, b'test')
    board.take_over()
    with pytest.raises(hl.HostLightingRejectedError) as refused:
        bridge.send_frame(device, {('button', B1): RED}, {('button', B1): (('entry',) + B1, False)})
    assert refused.value.status == hl.STATUS_CLAIMED and refused.value.holder.application_name == 'Other'
    assert board.frames == []


def test_a_frame_the_board_calls_invalid_is_raised_not_swallowed():
    """Test that a refusal other than the claim is an error naming the request byte."""
    board = m_ultra(led_limit=10)
    staging = {('range', 30, 4): (('run', 30, 4), False)}
    with pytest.raises(hl.HostLightingRejectedError, match='INVALID_ARG at request byte 4'):
        bridge.send_frame(board.open(), {('range', 30, 4): RED}, staging)


@pytest.mark.parametrize('colour_format', [2, 3])
def test_white_is_sent_as_rgbw_to_a_white_chain(colour_format):
    """Test that an RRGGBBWW colour goes as an RGBW pixel, by name, with the white moved over."""
    board = m_ultra(colour_format=colour_format)
    profile = {'up': (('button', UP), (255, 255, 255, 0)), 'b1': (('button', B1), (255, 0, 0, 128))}
    assert publish(board, profile)
    assert shown(board) == {3: (0, 0, 0, 255), 12: (0, 0, 0, 255), 8: (255, 0, 0, 128)}
    stage = board.sent(hl.CMD_STAGE)
    assert len(stage) == 1 and stage[0][3] == 0x16


def test_white_reaches_a_light_a_range_and_a_kind_the_same_way():
    """Test that every way of naming a light carries RGBW."""
    board = m_ultra(colour_format=2)
    grey = (10, 10, 10, 5)
    profile = {'a': (('light', L3, 0), grey), 'b': (('range', 0, 2), grey),
               'c': (('button', T['PLED1']), grey), 'd': (('button', CASE), grey)}
    assert publish(board, profile)
    assert set(shown(board).values()) == {(0, 0, 0, 15)}
    assert {request[3] >> 4 for request in board.sent(hl.CMD_STAGE)} == {hl.PIXEL_RGBW}


def test_a_six_digit_colour_stays_rgb_on_a_white_chain():
    """Test that only an entry asking for white is sent as RGBW."""
    board = m_ultra(colour_format=2)
    assert publish(board, {'up': (('button', UP), WHITE), 'b1': (('button', B1), (9, 9, 9, 9))})
    assert shown(board) == {3: WHITE, 12: WHITE, 8: (0, 0, 0, 18)}
    assert [request[3] for request in board.sent(hl.CMD_STAGE)] == [0x06, 0x16]


def test_white_is_a_property_of_the_target_not_of_one_entry():
    """Test that two entries on one control, only one asking for white, are sent as one RGBW pixel."""
    board = m_ultra(colour_format=2)
    profile = {'a': (('button', B1), (10, 20, 30)), 'b': (('button', B1), (1, 2, 3, 40))}
    assert publish(board, profile)
    assert shown(board) == {8: (0, 11, 22, 51)}


def test_white_is_dropped_with_a_reason_on_a_chain_without_it():
    """Test that an RGB chain gets the colour as RGB, and the user is told why once."""
    board = m_ultra(colour_format=0)
    profile = {'b1': (('button', B1), (255, 0, 0, 128)), 'up': (('button', UP), (1, 2, 3, 4))}
    text = run_against(board, profile)
    assert board.frames == [{8: RED, 3: (1, 2, 3), 12: (1, 2, 3)}]
    assert text.count('white component') == 1
    assert '2 profile entries ask for a white component' in text
    assert 'LED chain is GRB and has no white emitter' in text


def test_white_is_dropped_with_a_reason_on_firmware_without_the_format():
    """Test that a white chain whose firmware does not list RGBW is sent RGB, with the reason."""
    board = m_ultra(colour_format=2, pixel_formats=HELLO_PIXEL_FORMATS - {hl.PIXEL_RGBW})
    text = run_against(board, {'b1': (('button', B1), (255, 0, 0, 128))})
    assert board.frames == [{8: RED}]
    assert '1 profile entry asks for a white component' in text
    assert 'does not list the RGBW pixel format' in text


def test_no_white_warning_without_white_in_the_profile():
    """Test that a profile asking for no white is told nothing about it."""
    assert 'white component' not in run_against(m_ultra(), {'b1': (('button', B1), RED)})


def test_startup_names_controls_the_board_lacks_or_cannot_light():
    """Test the two startup warnings: wired with no LED, and not on the board at all."""
    profile = {'s1': (('button', T['S1']), RED), 'e1': (('button', T['E1']), RED),
               'a3': (('button', T['A3']), RED), 'pled': (('button', T['PLED1']), RED),
               'up': (('button', UP), RED)}
    text = run_against(m_ultra(), profile)
    assert '2 mapped control(s) are not on this board: A3, E1' in text
    assert '2 mapped control(s) have no LED on this board: S1, PLED1' in text


def test_startup_says_which_controls_have_more_than_one_light():
    """Test the note for a control wired to several lights, which leaves out the case strip."""
    profile = {'up': (('button', UP), RED), 'l3': (('button', L3), RED),
               'b1': (('button', B1), RED), 'case': (('button', CASE), RED)}
    text = run_against(m_ultra(), profile)
    assert 'more than one light, all of which will be lit: L3, Up' in text


def test_no_warnings_for_a_profile_the_board_fully_lights():
    """Test that a clean profile on a plain board warns about nothing."""
    text = run_against(Board(), {'b1': (('button', B1), RED)})
    assert 'warning' not in text and 'more than one light' not in text


def test_addressing_used_follows_the_profile():
    """Test which addressing modes a profile needs the board to list."""
    assert bridge.addressing_used({'a': (('button', B1), RED)}) == {hl.ADDRESSING_ACTION}
    assert bridge.addressing_used({'a': (('button', CASE), RED), 'b': (('light', CASE, 1), RED),
                                   'c': (('range', 0, 1), RED)}) == {
        hl.ADDRESSING_KIND, hl.ADDRESSING_LIGHT, hl.ADDRESSING_LED_RUN}


@pytest.mark.parametrize('fps, render_hz, rate, why', [
    (30.0, 40, 30.0, 'asked for'),
    (None, 40, 40.0, 'matching'),
    (None, 0, 60.0, 'did not state'),
    (None, 240, 120.0, 'capped below the board\'s 240 Hz'),
    (None, 120, 120.0, 'matching'),
    (None, 60, 60.0, 'matching'),
])
def test_the_stream_rate_follows_the_board(fps, render_hz, rate, why):
    """Test the rate chosen from --fps and the Summary's render rate."""
    caps = hlp.negotiate(Board(render_hz=render_hz).open())
    chosen, reason = bridge.resolve_stream_rate(fps, caps)
    assert chosen == rate and why in reason


def test_the_stream_rate_without_a_board_is_the_default():
    """Test that a dry run streams at the default rate."""
    assert bridge.resolve_stream_rate(None, hlp.HostLightingCapabilities.absent()) == (
        60.0, 'no board to ask')


def test_a_control_on_a_hall_effect_trigger_is_lit_like_any_other():
    """Test that a light whose owner is a trigger key is reached by name and by index, with no warning."""
    wiring = {'lights': [('B1', 4), ('B2', 5)], 'controls': [(0x83, B1[1]), (6, T['B2'][1])]}
    board = Board(**wiring)
    caps = hlp.negotiate(board.open())
    assert caps.lights[0].owner.pin == 0x83 and caps.lights[0].owner.action == B1[1]
    assert [(record.input.key, record.input.lit) for record in caps.controls] == [(0x83, True), (6, True)]
    assert publish(board, {'b1': (('button', B1), RED)})
    assert shown(board) == {4: RED}
    board = Board(**wiring)
    assert publish(board, {'one': (('light', B1, 0), GREEN)})
    assert shown(board) == {4: GREEN}
    assert 'warning' not in run_against(Board(**wiring), {'b1': (('button', B1), RED)})


def test_the_reports_already_waiting_are_read_before_a_frame_is_sent():
    """Test that an event waiting on the interface is taken in before the frame's first report."""
    board = Board()
    device = board.open()
    profile = {'b1': (('button', B1), RED)}
    staging = bridge.staging_for(profile, hlp.negotiate(device, bridge.addressing_used(profile)))
    board.outbox.append(bytes([hl.EVENT_MARKER, 9, 0]) + bytes(61))
    waiting = []
    board.hooks.append(lambda fake, request: waiting.append(len(fake.outbox)))
    assert bridge.send_frame(device, {('button', B1): RED}, staging, clear_first=True)
    assert waiting[0] == 0 and len(device.events) == 1
