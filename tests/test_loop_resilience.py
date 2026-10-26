"""Test the run loop against a board that changes, is shared, or stops answering.

The loop holds a claim on the board's lights for as long as the game runs. It
has to renew that claim, notice when another application takes the lights or
the board's configuration changes, and put its frame back when the board has
dropped it. None of that may end a working run. A board that leaves the bus
is waited for, and the run starts again on it when it returns.

Each run is on the board's clock, so a wait takes no real time.

SPDX-FileCopyrightText: (C) 2026 Jacob Simpson
SPDX-License-Identifier: GPL-3.0-or-later
"""
import pytest
from gp2040ce_hostlighting import protocol as hl
from gp2040ce_hostlighting import session as session_module
from gp2040ce_hostlighting.testing import HELLO_EVENTS

from hlp_spice2x import bridge, hlp

from .boards import M_ULTRA_CONTROLS, M_ULTRA_LIGHTS, OTHER_TOKEN, Board, ScriptedConnection, on_clock

T = hlp.CONTROL_TARGETS
RED = (255, 0, 0)
PROFILE = {'P1 Up': (('button', T['UP']), RED)}
ON = {'P1 Up': 1.0}
DIM = {'P1 Up': 0.5}
DIM_RED = (127, 0, 0)
# a keepalive short enough that waiting 50 ms makes a renewal due
SHORT_MS = 100


def after(action, states=None, wait=0.0):
    """Build a poll that does something to the board first, as another application would.

    :param action: callable taking no arguments, run before the poll answers
    :param states: the light states the poll then reports
    :param wait: seconds to let pass first on the board's clock, so a renewal falls due
    """
    def poll():
        bridge.time.sleep(wait)
        action()
        return states or ON
    return poll


def waiting(states=None):
    """Build a poll that only lets a short keepalive's renewal fall due."""
    return after(lambda: None, states, wait=0.05)


def run_with(board, *polls, profile=PROFILE, **kwargs):
    """Run the bridge against a board through scripted polls, on its clock, and return what it reported."""
    lines = []
    kwargs.setdefault('fps', 1000)
    with pytest.MonkeyPatch.context() as patch:
        on_clock(patch, board)
        bridge.run(ScriptedConnection(*polls), profile, device=board.open(), report=lines.append,
                   **kwargs)
    return '\n'.join(lines)


def claims(board) -> list:
    """List the CLAIM requests a board received as (action, low flag bits, generation)."""
    return [(request[3], request[2] & 0x0F, request[2] >> 4) for request in board.sent(hl.CMD_CLAIM)]


def settings(board) -> tuple:
    """Give the CONFIGURE settings the board applies, as (takeover, keepalive ms, brightness applied)."""
    return board.overlay, board.keepalive_ms, board.brightness


def test_the_lights_are_claimed_before_anything_is_configured_or_staged():
    """Test the order at connect: CLAIM, then CONFIGURE, then the first frame."""
    board = Board()
    run_with(board, ON)
    order = [request[0] for request in board.requests
             if request[0] in (hl.CMD_CLAIM, hl.CMD_CONFIGURE, hl.CMD_STAGE)]
    assert order == [hl.CMD_CLAIM, hl.CMD_CONFIGURE, hl.CMD_STAGE, hl.CMD_CLAIM]
    assert board.sent(hl.CMD_CLAIM)[0][8:24] == b'hlp-spice2x'.ljust(16, b'\x00')


def test_the_run_ends_by_giving_the_claim_up_not_by_release():
    """Test the shutdown: CLAIM give up, which restores the animations and the settings."""
    board = Board()
    text = run_with(board, ON, overlay=True)
    assert claims(board)[-1][0] == hl.CLAIM_GIVE_UP
    assert not board.sent(hl.CMD_RELEASE)
    assert board.holder_token == 0 and not board.live and settings(board) == (0, 2000, 1)
    assert text.endswith('claim given up, on-board animations restored')


@pytest.mark.parametrize('kwargs, sent, line', [
    ({}, '00 d0 07 01', 'takeover: whole frame, 2000 ms keepalive, applying board brightness'),
    ({'overlay': True, 'timeout_ms': 5000, 'full_brightness': True}, '01 88 13 00',
     'takeover: overlay, 5000 ms keepalive, ignoring board brightness'),
    ({'timeout_ms': 50}, '00 32 00 01', 'takeover: whole frame, 100 ms keepalive'),
])
def test_the_settings_reported_are_the_ones_the_board_applied(kwargs, sent, line):
    """Test the CONFIGURE bytes, and that the startup line shows the board's reply, clamp included."""
    board = Board()
    text = run_with(board, ON, **kwargs)
    assert board.sent(hl.CMD_CONFIGURE)[0][3:7] == bytes.fromhex(sent)
    assert line in text


def test_a_board_another_application_holds_is_left_alone():
    """Test that the run stops, naming the holder, and sends nothing that changes the board."""
    board = Board()
    board.take_over(name=b'MESH')
    with pytest.raises(hl.HostLightingError, match="'MESH' holds this board's lights.*--force"):
        run_with(board, ON)
    assert board.holder_token == OTHER_TOKEN
    assert not board.sent(hl.CMD_CONFIGURE) and not board.sent(hl.CMD_STAGE)
    assert claims(board) == [(hl.CLAIM_TAKE, 0, 0)]


def test_force_takes_the_lights_over_from_another_application():
    """Test that --force sends CLAIM take over and then runs as usual."""
    board = Board()
    board.take_over(name=b'MESH')
    run_with(board, ON, force=True)
    assert claims(board)[0] == (hl.CLAIM_TAKE_OVER, 0, 0)
    assert board.frames == [{0: RED}]


def test_a_static_screen_keeps_the_claim_and_the_frame_without_republishing():
    """Test the renewal: CLAIM with KEEPALIVE under the generation, answered, and no new frame."""
    board = Board()
    run_with(board, ON, waiting(), waiting(), timeout_ms=SHORT_MS)
    renewals = claims(board)[1:-1]
    assert len(renewals) >= 2
    assert set(renewals) == {(hl.CLAIM_TAKE, hl.FLAG_KEEPALIVE, 2)}
    assert len(board.frames) == 1 and len(board.sent(hl.CMD_CONFIGURE)) == 1


def test_a_take_over_by_another_application_stops_the_frames_until_it_lets_go():
    """Test the whole of sharing a board mid-run.

    While taken over, it is reported once, nothing is staged, and the waiting
    CLAIMs carry neither a generation nor KEEPALIVE, which would refresh the
    other application's frame. Once the board is unclaimed the lights are
    taken back, CONFIGURE is sent again and the frame is staged again.
    """
    board = Board()
    text = run_with(board, ON, after(board.take_over), waiting(DIM), waiting(DIM),
                    after(board.let_go, DIM), DIM, timeout_ms=SHORT_MS, overlay=True)
    assert text.count("'Other' took the lights over") == 1
    assert text.count("the lights are unclaimed again, so the claim was taken") == 1
    assert board.frames == [{0: RED}, {0: DIM_RED}]
    asked_while_waiting = [flags for _, flags, generation in claims(board)[1:] if generation == 0]
    assert len(asked_while_waiting) >= 2 and set(asked_while_waiting) == {0}
    assert [request[3] for request in board.sent(hl.CMD_CONFIGURE)] == [1, 1]
    stage = board.sent(hl.CMD_STAGE)
    assert [request[2] >> 4 for request in stage] == [2, 5]


def test_nothing_is_given_up_while_another_application_holds_the_lights():
    """Test that a run ending while it waits sends no give up and claims nothing was restored."""
    board = Board()
    text = run_with(board, ON, after(board.take_over), ON)
    assert board.holder_token == OTHER_TOKEN
    assert all(action != hl.CLAIM_GIVE_UP for action, _, _ in claims(board))
    assert 'given up' not in text


def test_a_lapsed_claim_is_taken_again_with_its_settings_and_its_frame():
    """Test a claim that lapsed unnoticed: new generation, CONFIGURE again, frame again."""
    board = Board()
    text = run_with(board, ON, after(board.let_go), ON, timeout_ms=5000, overlay=True)
    assert board.frames == [{0: RED}, {0: RED}]
    assert [request[3:7] for request in board.sent(hl.CMD_CONFIGURE)] == [
        bytes.fromhex('01 88 13 01')] * 2
    assert [generation for _, _, generation in claims(board)[:2]] == [0, 2]
    assert board.sent(hl.CMD_STAGE)[-1][2] >> 4 == 4
    assert 'took' not in text


def test_a_frame_refused_under_an_old_generation_leads_to_a_renewal():
    """Test the same lapse on a board that sends no events, where the refused frame shows it."""
    board = Board(events=HELLO_EVENTS - {0})
    text = run_with(board, ON, after(board.let_go, DIM), DIM, DIM)
    assert board.frames == [{0: RED}, {0: DIM_RED}]
    assert len(board.sent(hl.CMD_CONFIGURE)) == 2
    assert '2 frames in' in text and '0 frames unacknowledged' in text


def test_a_takeover_that_ended_has_its_frame_staged_again():
    """Test that the frame returns after the board dropped it, though the game changed nothing.

    A USB suspend ends the takeover and keeps the claim.
    """
    board = Board()

    def suspend_and_resume():
        board.suspend()
        board.resume()

    run_with(board, ON, after(suspend_and_resume), ON)
    assert board.frames == [{0: RED}, {0: RED}]


def test_a_configuration_change_re_resolves_the_controls_and_restages():
    """Test that a profile switch on the board moves the frame to the control's new light."""
    board = Board(lights=M_ULTRA_LIGHTS, controls=M_ULTRA_CONTROLS)
    swapped = [(pin, {1: 2, 2: 1}.get(action, action)) for pin, action in M_ULTRA_CONTROLS]
    text = run_with(board, ON, after(lambda: board.rewire(controls=swapped)), ON)
    assert board.frames == [{3: RED, 12: RED}, {1: RED}]
    assert 'board configuration changed, controls re-resolved' in text
    assert 'raw ranges' not in text
    assert board.sent(hl.CMD_CLEAR)


def test_a_profile_switch_re_resolves_the_controls_once():
    """Test that a profile hotkey's switch, which page 1, its pages and the token show in one event, restages once."""
    board = Board(lights=M_ULTRA_LIGHTS, controls=M_ULTRA_CONTROLS)
    # profile 2's pages: Up and Down swapped, each light kept on its pin
    swapped = Board(lights=M_ULTRA_LIGHTS, controls=M_ULTRA_CONTROLS)
    swapped.rewire(controls=[(pin, {1: 2, 2: 1}.get(action, action)) for pin, action in M_ULTRA_CONTROLS])
    board.profile_pages[2] = (swapped.controls, swapped.lights)
    text = run_with(board, ON, after(lambda: board.store_profile(2)), ON)
    assert board.state[0][2] == 2 and board.frames == [{3: RED, 12: RED}, {1: RED}]
    assert text.count('board configuration changed, controls re-resolved') == 1


def test_a_configuration_change_points_at_the_profiles_raw_ranges():
    """Test that a profile using raw ranges is told to check them, since they do not follow."""
    board = Board()
    profile = dict(PROFILE, strip=(('range', 0, 2), RED))
    text = run_with(board, ON, after(lambda: board.rewire(lights=[('Up', 5)])), ON,
                    profile=profile)
    assert "controls re-resolved. Check the profile's raw ranges" in text


def test_an_entry_the_changed_board_cannot_honour_is_held_back_and_named():
    """Test that losing one indexed light mid-run costs that entry and not the session."""
    board = Board(lights=M_ULTRA_LIGHTS, controls=M_ULTRA_CONTROLS)
    profile = {'P1 Up': (('button', T['B1']), RED), 'second': (('light', T['UP'], 1), RED)}
    both = {'P1 Up': 1.0, 'second': 1.0}
    single = [(pin, 2 if pin == 27 else action) for pin, action in M_ULTRA_CONTROLS]
    text = run_with(board, both, after(lambda: board.rewire(controls=single), both), both,
                    profile=profile)
    assert board.frames == [{8: RED, 12: RED}, {8: RED}]
    assert "warning: after the board's configuration changed, Up[1] is out of range" in text


def test_a_dropped_page_read_after_a_change_does_not_end_the_run():
    """Test that an unanswered re-read is counted, not warned of, and made again at the next poll."""
    board = Board()

    def change_and_go_quiet():
        board.rewire(lights=[('Up', 5)])
        board.drop.add(hl.CMD_GET_PAGE)

    text = run_with(board, ON, after(change_and_go_quiet), after(board.drop.clear), ON)
    assert 'warning' not in text and 'board configuration changed' in text
    assert board.frames == [{0: RED}, {5: RED}]
    assert 'frames in' in text and 'disconnected' not in text


def test_a_dropped_renewal_does_not_end_the_run():
    """Test that an unanswered CLAIM is asked again later."""
    board = Board()
    text = run_with(board, ON, after(lambda: board.drop.add(hl.CMD_CLAIM), wait=0.05),
                    after(board.drop.clear, wait=0.05), ON, timeout_ms=SHORT_MS)
    assert 'frames in' in text and 'disconnected' not in text and 'warning' not in text
    assert len(claims(board)) > 3 and text.endswith('claim given up, on-board animations restored')


def test_an_unacknowledged_frame_is_sent_once_more_then_staged_whole_next_time():
    """Test the order of recovery: resend, then HELLO to see the board is there, then a whole frame."""
    board = Board()
    text = run_with(board, ON, after(lambda: board.drop.add(hl.CMD_STAGE), DIM),
                    after(lambda: board.drop.clear(), DIM))
    assert '1 frames unacknowledged' in text and 'disconnected' not in text
    assert board.frames == [{0: RED}, {0: DIM_RED}]
    dropped = [request for request in board.sent(hl.CMD_STAGE) if request[7:10] == bytes(DIM_RED)]
    assert len(dropped) == 3 and dropped[0][3:] == dropped[1][3:] == dropped[2][3:]
    assert len(board.sent(hl.CMD_HELLO)) == 2
    assert board.sent(hl.CMD_CLEAR)


def test_an_unacknowledged_frame_is_not_assumed_shown_or_unshown():
    """Test that after a lost acknowledgement the next frame goes out even if it equals the last shown.

    The board may be showing either frame, so the game returning to the earlier
    one is not a reason to send nothing.
    """
    board = Board()
    run_with(board, ON, after(lambda: board.drop.add(hl.CMD_STAGE), DIM),
             after(lambda: board.drop.clear(), ON))
    assert board.frames == [{0: RED}, {0: RED}]


def test_a_housekeeping_request_the_board_rejects_is_said_once_and_survived(monkeypatch):
    """Test that a refusal other than the claim's warns once and does not end the run."""
    monkeypatch.setattr(session_module, 'STATE_POLL_SECONDS', 0.0)
    board = Board(events=HELLO_EVENTS - {0})
    refuse = after(lambda: board.reject.update({hl.CMD_GET_PAGE: hl.STATUS_UNSUPPORTED}))
    text = run_with(board, ON, refuse, ON, ON, DIM)
    assert text.count("warning: the board's state could not be read") == 1
    assert 'UNSUPPORTED' in text
    assert board.frames == [{0: RED}, {0: DIM_RED}]
    assert text.endswith('claim given up, on-board animations restored')


def test_a_page_re_read_the_board_rejects_is_retried_not_fatal():
    """Test that a rejected re-read after a configuration change is warned about and tried again after 1 s."""
    board = Board()

    def change_and_reject():
        board.rewire(lights=[('Up', 5)])
        board.reject[(hl.CMD_GET_PAGE, hl.PAGE_SUMMARY)] = hl.STATUS_UNSUPPORTED

    text = run_with(board, ON, after(change_and_reject), after(board.reject.clear, wait=1.0), ON)
    assert text.count("warning: could not read the board's configuration again") == 1
    assert 'UNSUPPORTED' in text and 'Trying again in 1 s' in text
    assert board.frames == [{0: RED}, {5: RED}] and 'frames in' in text


def test_a_board_that_answers_nothing_at_all_is_lost():
    """Test that an unacknowledged frame followed by an unanswered HELLO ends the run as a disconnect."""
    board = Board()

    def go_silent():
        board.drop.update({hl.CMD_STAGE, hl.CMD_HELLO, hl.CMD_CLAIM})

    text = run_with(board, ON, after(go_silent, DIM), DIM)
    assert 'board stopped answering' in text and 'disconnected' not in text
    assert 'spice2x' not in text


def test_a_board_unplugged_mid_run_ends_it_and_is_not_blamed_on_spice2x():
    """Test that a failed write mid-run is reported as the board, with the statistics."""
    board = Board()

    def unplug(fake, request):
        raise OSError('device disconnected')

    text = run_with(board, ON, after(lambda: board.hooks.append(unplug), DIM), DIM)
    assert 'board disconnected' in text and 'spice2x' not in text
    assert '1 frames in' in text and 'given up' not in text


def test_the_game_closing_hands_the_lights_back():
    """Test that a lost spice2x connection is a normal ending that still gives the claim up."""
    board = Board()

    def game_exits():
        raise ConnectionError('closed by the game')

    text = run_with(board, ON, game_exits)
    assert 'spice2x connection lost (closed by the game)' in text
    assert board.holder_token == 0 and text.endswith('claim given up, on-board animations restored')


def test_a_profile_the_board_cannot_honour_still_hands_the_lights_back():
    """Test that a refusal after the claim was taken does not leave the board claimed."""
    board = Board()
    with pytest.raises(bridge.profile_module.ProfileError):
        run_with(board, ON, profile={'a': (('light', T['UP'], 3), RED)})
    assert board.holder_token == 0


def test_a_board_without_events_is_polled_for_its_state(monkeypatch):
    """Test that without events page 1 is read on a timer and a configuration change is still seen."""
    monkeypatch.setattr(session_module, 'STATE_POLL_SECONDS', 0.0)
    board = Board(events=HELLO_EVENTS - {0})
    text = run_with(board, ON, after(lambda: board.rewire(lights=[('Up', 5)])), ON)
    assert board.frames == [{0: RED}, {5: RED}]
    assert 'board configuration changed' in text
    assert not board.sent(hl.CMD_SUBSCRIBE)


def test_a_polled_board_notices_a_take_over_from_page_one(monkeypatch):
    """Test that a page 1 read refused under the old generation leads to a renewal, not a stall.

    Every request but CLAIM is refused once the holder has changed, the poll
    included, so the renewal has to come before the next read.
    """
    monkeypatch.setattr(session_module, 'STATE_POLL_SECONDS', 0.0)
    board = Board(events=HELLO_EVENTS - {0})
    text = run_with(board, ON, after(board.take_over), ON, DIM, DIM)
    assert "'Other' took the lights over" in text
    assert board.frames == [{0: RED}]
    assert len(board.sent(hl.CMD_STAGE)) == 1
    assert board.sent(hl.CMD_GET_PAGE)[-1][2] >> 4 == 0


def test_lights_the_profile_does_not_map_are_named_once():
    """Test that unmapped game lights are reported on the first poll only."""
    board = Board()
    states = dict(ON, Spot=1.0, Neon=0.0)
    text = run_with(board, states, states)
    assert text.count('2 light(s) not in the profile, ignoring: Neon, Spot') == 1


def test_a_frame_is_only_sent_when_the_game_changes_something():
    """Test that identical polls publish one frame and a changed one publishes another."""
    board = Board()
    text = run_with(board, ON, ON, ON, DIM, DIM)
    assert board.frames == [{0: RED}, {0: DIM_RED}]
    assert '2 frames in' in text


def test_a_light_leaving_the_frame_clears_the_staged_frame_first():
    """Test that a target the game stops reporting goes dark.

    The first frame after the start clears first too, so that no pixel an earlier host staged survives.
    """
    board = Board()
    profile = dict(PROFILE, b1=(('button', T['B1']), RED))
    run_with(board, {'P1 Up': 1.0, 'b1': 1.0}, {'b1': 1.0}, profile=profile)
    assert board.frames == [{0: RED, 4: RED}, {4: RED}]
    assert len(board.sent(hl.CMD_CLEAR)) == 2


def written_while_away(board) -> list:
    """Keep each request written to the board while it is off the bus, which the board refuses and does not keep."""
    written = []
    write = board.write

    def spy(data):
        if board.departed:
            written.append(bytes(data)[1:])
        return write(data)
    board.write = spy
    return written


def returning(board):
    """Build the callable the run uses to find a fake board again once it is back on the bus."""
    def reopen(board_id):
        assert board_id == board.board_id.hex().upper()
        return None if board.departed else board.open()
    return reopen


def test_a_board_that_leaves_the_bus_is_waited_for_and_started_again(monkeypatch):
    """Test a reboot mid-run: reported once, waited out, then claim, settings and frame again."""
    monkeypatch.setattr(bridge, 'RECONNECT_SECONDS', 0.0)
    board = Board()
    text = run_with(board, ON, after(board.unplug, DIM), DIM, DIM, after(board.replug, DIM), DIM,
                    reopen=returning(board), overlay=True)
    assert text.count('board disconnected (the board is no longer connected (reading failed') == 1
    assert text.count('Waiting for it to return') == 1
    assert text.count('board: ') == 2 and text.count('takeover: overlay') == 2
    assert [claim[0] for claim in claims(board)] == [hl.CLAIM_TAKE, hl.CLAIM_TAKE, hl.CLAIM_GIVE_UP]
    assert len(board.sent(hl.CMD_CONFIGURE)) == 2
    assert board.frames == [{0: RED}, {0: DIM_RED}]
    assert '2 frames in' in text and text.endswith('claim given up, on-board animations restored')


def test_a_board_that_never_returns_is_waited_for_until_the_run_ends(monkeypatch):
    """Test that the run stays up with no board, and gives up no claim when it is interrupted."""
    monkeypatch.setattr(bridge, 'RECONNECT_SECONDS', 0.0)
    board = Board()
    text = run_with(board, ON, after(board.unplug, DIM), DIM, DIM, reopen=returning(board))
    assert 'Waiting for it to return' in text
    assert '1 frames in' in text and 'given up' not in text


def test_a_board_is_looked_for_no_more_than_once_a_second():
    """Test that the polls made while a board is away do not each enumerate the bus."""
    board = Board()
    looked = []
    run_with(board, ON, after(board.unplug, DIM), DIM, DIM, DIM, reopen=looked.append)
    assert looked == []


@pytest.mark.parametrize('force', [False, True])
def test_a_returning_board_another_application_holds_is_waited_for(monkeypatch, force):
    """Test that a claim taken by another application while the board was away is not taken over."""
    monkeypatch.setattr(bridge, 'RECONNECT_SECONDS', 0.0)
    board = Board()

    def back_but_claimed():
        board.replug()
        board.take_over()

    text = run_with(board, ON, after(board.unplug, DIM), after(back_but_claimed, DIM), waiting(DIM),
                    after(board.let_go, DIM, wait=0.05), waiting(DIM),
                    reopen=returning(board), timeout_ms=SHORT_MS, force=force)
    assert "'Other' holds the lights. Waiting for it to give them up" in text
    assert 'the lights are unclaimed again, so the claim was taken' in text
    assert [action for action, _, _ in claims(board)].count(hl.CLAIM_TAKE_OVER) == int(force)
    assert board.frames == [{0: RED}, {0: DIM_RED}]


def test_a_returning_board_that_lost_an_indexed_light_holds_that_entry_back(monkeypatch):
    """Test that a board back with fewer lights costs the entry it cannot honour, and not the run."""
    monkeypatch.setattr(bridge, 'RECONNECT_SECONDS', 0.0)
    board = Board(lights=M_ULTRA_LIGHTS, controls=M_ULTRA_CONTROLS)
    profile = {'P1 Up': (('button', T['B1']), RED), 'second': (('light', T['UP'], 1), RED)}
    both = {'P1 Up': 1.0, 'second': 1.0}
    single = [(pin, 2 if pin == 27 else action) for pin, action in M_ULTRA_CONTROLS]

    def back_with_one_up():
        board.rewire(controls=single)
        board.replug()

    text = run_with(board, both, after(board.unplug, both), after(back_with_one_up, both), both,
                    reopen=returning(board), profile=profile)
    assert "warning: Up[1] is out of range" in text and text.count('board: ') == 2
    assert board.frames == [{8: RED, 12: RED}, {8: RED}]


def test_a_returning_board_that_does_not_answer_yet_is_tried_again(monkeypatch):
    """Test that a board back on the bus but silent is closed and looked for again."""
    monkeypatch.setattr(bridge, 'RECONNECT_SECONDS', 0.0)
    board = Board()

    def back_but_silent():
        board.replug()
        board.drop.add(hl.CMD_HELLO)

    closed = []

    def answering_again():
        closed.append(board.closed)
        board.drop.clear()

    text = run_with(board, ON, after(board.unplug, DIM), after(back_but_silent, DIM),
                    after(answering_again, DIM), DIM, reopen=returning(board))
    assert text.count('board: ') == 2 and closed == [True]
    assert board.frames == [{0: RED}, {0: DIM_RED}]


def test_a_returning_board_with_other_firmware_ends_the_run(monkeypatch):
    """Test that a board that comes back without what the bridge needs is not waited on."""
    monkeypatch.setattr(bridge, 'RECONNECT_SECONDS', 0.0)
    board = Board()

    def back_without_stage():
        board.replug()
        board.commands.discard(hl.CMD_STAGE)

    with pytest.raises(hl.HostLightingIncompatibleError, match='command 0x10'):
        run_with(board, ON, after(board.unplug, DIM), after(back_without_stage, DIM), DIM,
                 reopen=returning(board))
    assert board.closed


def test_a_board_that_stops_answering_on_a_static_screen_is_lost():
    """Test that unanswered renewals end the run when no frame is sent to show the board is gone."""
    board = Board()
    text = run_with(board, ON, after(lambda: board.drop.add(hl.CMD_CLAIM), wait=0.05), waiting(),
                    waiting(), waiting(), timeout_ms=SHORT_MS)
    assert 'board stopped answering' in text and 'disconnected' not in text
    assert board.frames == [{0: RED}]


def test_a_board_that_returns_on_a_static_screen_is_given_the_frame_again(monkeypatch):
    """Test that the frame is staged on the returning board when the game has changed no light."""
    monkeypatch.setattr(bridge, 'RECONNECT_SECONDS', 0.0)
    board = Board()
    run_with(board, ON, after(board.unplug), ON, after(board.replug), ON, reopen=returning(board))
    assert board.frames == [{0: RED}, {0: RED}]


def test_a_board_that_stays_away_is_looked_for_once_per_interval(monkeypatch):
    """Test that each look sets the time of the next one."""
    monkeypatch.setattr(bridge, 'RECONNECT_SECONDS', 0.2)
    board = Board()
    looked = []
    run_with(board, ON, after(board.unplug), after(lambda: None, wait=0.25), ON, ON, ON,
             reopen=looked.append)
    assert len(looked) == 1


def test_nothing_is_written_to_a_board_that_left_the_bus():
    """Test that the claim is not given up on a board that is no longer there."""
    board = Board()
    refused = written_while_away(board)
    text = run_with(board, ON, after(board.unplug), ON, ON, reopen=returning(board))
    assert 'Waiting for it to return' in text and 'given up' not in text
    assert 'could not give the claim up' not in text
    assert not [request for request in refused if request[0] == hl.CMD_CLAIM]


def test_unanswered_renewals_are_counted_afresh_on_a_returning_board(monkeypatch):
    """Test that one unanswered renewal after a return does not end the wait again."""
    monkeypatch.setattr(bridge, 'RECONNECT_SECONDS', 0.0)
    board = Board()
    silence = after(lambda: board.drop.add(hl.CMD_CLAIM), wait=0.05)
    text = run_with(board, ON, silence, waiting(), waiting(), after(board.drop.clear), silence,
                    after(board.drop.clear, wait=0.05), reopen=returning(board), timeout_ms=SHORT_MS)
    assert text.count('board stopped answering. Waiting for it to return') == 1
    assert text.count('board: ') == 2


def test_a_refresh_left_pending_is_dropped_when_the_board_returns(monkeypatch):
    """Test that the pages are read once on a returning board, by its start-up."""
    monkeypatch.setattr(bridge, 'RECONNECT_SECONDS', 0.0)
    monkeypatch.setattr(session_module, 'REFRESH_RETRY_SECONDS', 0.0)
    board = Board()
    marks = []

    def change_and_reject():
        board.rewire(lights=[('Up', 5)])
        board.reject[(hl.CMD_GET_PAGE, hl.PAGE_SUMMARY)] = hl.STATUS_UNSUPPORTED

    def back():
        board.reject.clear()
        board.replug()
        marks.append(len(board.requests))

    run_with(board, ON, after(change_and_reject), after(board.unplug), after(back), ON, ON,
             reopen=returning(board))
    summaries = [request for request in board.requests[marks[0]:]
                 if request[0] == hl.CMD_GET_PAGE and request[3] == hl.PAGE_SUMMARY]
    assert len(summaries) == 1


def test_an_unanswered_configure_at_startup_gives_the_claim_up():
    """Test that the board is not left claimed when the settings go unanswered."""
    board = Board(drop={hl.CMD_CONFIGURE})
    with pytest.raises(hl.HostLightingTimeoutError):
        run_with(board, ON)
    assert board.holder_token == 0
    assert [action for action, _, _ in claims(board)] == [hl.CLAIM_TAKE, hl.CLAIM_GIVE_UP]


def test_an_unanswered_configure_on_a_returning_board_gives_the_claim_up(monkeypatch):
    """Test that the next attempt does not find the bridge's own earlier claim in its way."""
    monkeypatch.setattr(bridge, 'RECONNECT_SECONDS', 0.0)
    board = Board()

    def back_without_settings():
        board.replug()
        board.drop.add(hl.CMD_CONFIGURE)

    text = run_with(board, ON, after(board.unplug, DIM), after(back_without_settings, DIM),
                    after(board.drop.clear, DIM), DIM, reopen=returning(board))
    assert "'hlp-spice2x' holds" not in text
    assert board.frames == [{0: RED}, {0: DIM_RED}]


def test_the_settings_are_sent_again_after_an_unanswered_configure():
    """Test that a CONFIGURE lost after a lapse is repeated at the next renewal."""
    board = Board()

    def lapse_and_drop():
        board.let_go()
        board.drop.add(hl.CMD_CONFIGURE)

    run_with(board, ON, after(lapse_and_drop, wait=0.05), after(board.drop.clear, wait=0.05), ON,
             timeout_ms=SHORT_MS, overlay=True)
    assert len(board.sent(hl.CMD_CONFIGURE)) == 3 and board.sent(hl.CMD_CONFIGURE)[-1][3] == 1


def test_a_claim_another_application_took_is_not_reported_as_given_up():
    """Test that the closing line is printed only when the board shows no holder."""
    board = Board(events=HELLO_EVENTS - {0})
    text = run_with(board, ON, after(board.take_over))
    assert 'given up' not in text and board.holder_token == OTHER_TOKEN


def test_a_change_to_a_page_the_bridge_does_not_read_restages_nothing():
    """Test that a moved state token alone, as an animation hotkey causes, sends no frame and no message.

    Only the Animations page's change counter moves.
    """
    board = Board()
    before = []

    def change():
        before.append(len(board.sent(hl.CMD_GET_PAGE)))
        board.animations = [[bytes([1, 2, 1, 2]), bytes([6, 6, 6])]]

    text = run_with(board, ON, after(change), ON)
    assert 'configuration changed' not in text and board.frames == [{0: RED}]
    assert [request[3] for request in board.sent(hl.CMD_GET_PAGE)[before[0]:]] == [hl.PAGE_SUMMARY]


def test_a_bus_that_cannot_be_enumerated_is_tried_again(monkeypatch):
    """Test that a failure to look for the board is not reported as a lost spice2x connection."""
    monkeypatch.setattr(bridge, 'RECONNECT_SECONDS', 0.0)
    board = Board()
    failures = [OSError('enumeration failed')]

    def reopen(board_id):
        if failures:
            raise failures.pop()
        return board.open()

    text = run_with(board, ON, after(board.unplug, DIM), DIM, after(board.replug, DIM), DIM, reopen=reopen)
    assert 'spice2x' not in text and text.count('board: ') == 2


def test_an_interrupt_while_a_board_returns_closes_it(monkeypatch):
    """Test that Ctrl-C during the start-up on a returning board leaves no device open."""
    monkeypatch.setattr(bridge, 'RECONNECT_SECONDS', 0.0)
    board = Board()

    def interrupt(fake, request):
        raise KeyboardInterrupt

    def back():
        board.replug()
        board.hooks.append(interrupt)

    text = run_with(board, ON, after(board.unplug, DIM), after(back, DIM), DIM, reopen=returning(board))
    assert 'frames in' in text and board.closed


def test_a_claim_that_came_back_under_the_same_generation_is_set_up_again():
    """Test that changes of holder seen only in passing still bring the settings and the frame back.

    The generation has seven values. Six changes of holder and the bridge's own
    CLAIM bring it back to the one the bridge held.
    """
    board = Board()

    def six_changes():
        for _ in range(3):
            board.take_over()
            board.let_go()

    run_with(board, ON, after(six_changes), ON, overlay=True)
    assert {request[2] >> 4 for request in board.sent(hl.CMD_STAGE)} == {2}
    assert len(board.sent(hl.CMD_CONFIGURE)) == 2
    assert board.frames == [{0: RED}, {0: RED}]


def test_a_board_without_events_that_left_is_seen_before_a_frame_is_written(monkeypatch):
    """Test that the read before a frame finds the board gone, so no report is written to it."""
    monkeypatch.setattr(bridge, 'RECONNECT_SECONDS', 0.0)
    board = Board(events=HELLO_EVENTS - {0})
    refused = written_while_away(board)
    text = run_with(board, ON, after(board.unplug, DIM), DIM, reopen=lambda board_id: None)
    assert 'board disconnected (the board is no longer connected (reading failed with read error))' in text
    assert refused == [] and '0 frames unacknowledged' in text
