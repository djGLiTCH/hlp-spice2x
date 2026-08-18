"""Test how a frame reaches the board's lights, across protocol versions.

The interesting behaviour is what a bare control name does on a board that
gives that control more than one light. Naming the control is still the primary
path, because that is what lets one profile work across layouts, but on a board
that publishes a light table the extra lights have to be staged explicitly or
they never light.

SPDX-FileCopyrightText: © 2026 Jacob Simpson
SPDX-License-Identifier: GPL-3.0-or-later
"""
import pytest

from hlp_spice2x import bridge, hlp

from .fake_board import M_ULTRA_LIGHTS, FakeBoard, OneShotConnection

RED = (255, 0, 0)


def connected(**kwargs):
    """Open a fake board, negotiate, and return the device, capabilities and board."""
    board = FakeBoard(**kwargs)
    device = board.open()
    caps = hlp.negotiate(device)
    board.requests.clear()
    return device, caps, board


def stage(frame, **kwargs):
    """Stage one frame against a fake board and return the board and its plan."""
    device, caps, board = connected(**kwargs)
    bridge.send_frame(device, frame, plan=caps.staging_plan())
    return board


def ranges(board):
    """Collect the (start, count) of every SET_RANGE the board was sent, in order."""
    return [(payload[0], payload[1]) for payload in board.staged(hlp.CMD_SET_RANGE)]


def named(board):
    """Collect the control IDs named in every SET_BUTTONS the board was sent."""
    return [payload[1 + n * 4] for payload in board.staged(hlp.CMD_SET_BUTTONS)
            for n in range(payload[0])]


def test_a_control_the_board_gives_several_lights_has_all_of_them_staged():
    """Test that both of the reference board's Up lights are coloured, not just one."""
    board = stage({('button', 0): RED}, version=(1, 1), lights=M_ULTRA_LIGHTS)
    assert named(board) == [0]
    assert ranges(board) == [(3, 1), (12, 1)]


def test_the_expansion_follows_the_control_pass_and_never_precedes_it():
    """Test the one ordering that makes per-light staging survive the frame.

    Naming a control colours every light of it on one of the two render
    pipelines, so a SET_BUTTONS emitted after the expansion would re-colour the
    pixels the expansion had just set and undo it inside the same frame.
    """
    board = stage({('button', 0): RED}, version=(1, 1), lights=M_ULTRA_LIGHTS)
    staged = [command for command, _ in board.requests
              if command in (hlp.CMD_SET_BUTTONS, hlp.CMD_SET_RANGE)]
    assert staged == [hlp.CMD_SET_BUTTONS, hlp.CMD_SET_RANGE, hlp.CMD_SET_RANGE]


def test_a_board_with_no_light_table_stages_by_name_alone():
    """Test that v1.0 leaves the breadth of a control name to the firmware.

    Page 5 does not exist there, so which lights a control owns is not knowable
    and there is nothing to expand. One entry, and the board decides what it
    reaches.
    """
    board = stage({('button', 0): RED}, version=(1, 0), lights=M_ULTRA_LIGHTS)
    assert named(board) == [0]
    assert ranges(board) == []


def test_a_synthesised_table_cannot_show_a_duplicate_so_nothing_is_expanded():
    """Test that a table rebuilt from per-control config is not read as an inventory.

    Those rows are per-control, so such a board reports one row for Up however
    many Up buttons it has. Counting them would be answering a question the
    table cannot answer.
    """
    board = stage({('button', 0): RED}, version=(1, 3), lights=M_ULTRA_LIGHTS,
                  synthesised=True)
    assert named(board) == [0]
    assert ranges(board) == []


def test_a_whole_strip_control_is_never_expanded():
    """Test that the case strip stays one entry rather than becoming thirty.

    The reference board puts thirty lights on the case. Expanding it would cost
    extra reports in every frame, on exactly the boards with the most lights,
    to reach lights that one SET_BUTTONS entry already covers.
    """
    board = stage({('button', 29): RED}, version=(1, 3), lights=M_ULTRA_LIGHTS)
    assert named(board) == [29]
    assert ranges(board) == []


def test_an_extended_control_is_staged_by_index_before_it_can_be_staged_by_name():
    """Test that v1.1 reaches E1 through the light table rather than by naming it.

    v1.1 names the extended controls on page 5 but does not stage them by name,
    so a SET_BUTTONS entry for one is simply counted as skipped. The light table
    is the only route to them until v1.2.
    """
    board = stage({('button', 30): RED}, version=(1, 1), lights=[(0, 0), (30, 1)])
    assert named(board) == []
    assert ranges(board) == [(1, 1)]


def test_an_extended_control_is_staged_by_name_once_it_can_be():
    """Test that v1.2 goes back to naming the control, with no raw indexes involved."""
    board = stage({('button', 30): RED}, version=(1, 2), lights=[(0, 0), (30, 1)])
    assert named(board) == [30]
    assert ranges(board) == []


def test_a_raw_range_has_the_last_word_over_a_control_name():
    """Test that addressing a pixel explicitly beats reaching it through a control.

    Both land in one staging buffer where the last write wins, so a profile that
    says exactly which LED it means has to be emitted after anything implied by
    a control name.
    """
    board = stage({('button', 0): RED, ('range', 3, 1): (0, 0, 255)},
                  version=(1, 1), lights=M_ULTRA_LIGHTS)
    assert ranges(board)[-1] == (3, 1)
    assert board.staged(hlp.CMD_SET_RANGE)[-1][2:5] == bytes((0, 0, 255))


def test_a_target_kind_no_pass_recognises_is_refused():
    """Test that an unknown target raises rather than lighting nothing quietly.

    Staging is fire-and-forget, so a target that no pass picks up produces a
    frame that stages nothing and reports nothing, which is the hardest possible
    failure to notice.
    """
    device, _, _ = connected(version=(1, 3))
    with pytest.raises(ValueError, match='unknown target kind'):
        bridge.send_frame(device, {('light', 3): RED})


def test_target_discovery_reads_the_table_instead_of_writing_to_the_board():
    """Test that a board with a light table is asked nothing at all.

    Probing means writing a black frame to every mapped control and reading back
    what stuck. The table already says which controls have lights, so on any
    board that publishes one the probe is pure cost.
    """
    device, caps, board = connected(version=(1, 1), lights=M_ULTRA_LIGHTS)
    unmapped, unreachable = bridge.validate_targets(
        device, {'a': (('button', 0), RED), 'b': (('button', 29), RED)}, caps)
    assert unmapped == []
    assert unreachable == []
    assert board.requests == []


def test_a_control_the_table_does_not_name_is_reported():
    """Test that a control with no light on this board is still caught without probing."""
    device, caps, _ = connected(version=(1, 1), lights=[(0, 0)])
    unmapped, _ = bridge.validate_targets(device, {'a': (('button', 5), RED)}, caps)
    assert unmapped == ['B2']


def test_a_board_with_no_light_table_is_still_probed():
    """Test that v1.0 keeps the write-probe, which is the only thing it can do."""
    device, caps, board = connected(version=(1, 0), lights=[(0, 0)])
    unmapped, _ = bridge.validate_targets(
        device, {'a': (('button', 0), RED), 'b': (('button', 5), RED)}, caps)
    assert unmapped == ['B2']
    assert hlp.CMD_SET_BUTTONS in [command for command, _ in board.requests]


def test_an_unreachable_control_is_not_reported_as_missing_hardware():
    """Test that firmware too old to reach a control says so, rather than blaming the board.

    Reporting "no LED on this board" for a control the board plainly has sends
    the user hunting a hardware fault that does not exist.
    """
    device, caps, _ = connected(version=(1, 0), lights=[(0, 0), (30, 1)])
    unmapped, unreachable = bridge.validate_targets(device, {'a': (('button', 30), RED)}, caps)
    assert unmapped == []
    assert unreachable == ['E1']


def test_a_probe_that_goes_unanswered_does_not_end_the_run():
    """Test that tidying up after the probes tolerates a late reply.

    The clear afterwards is housekeeping. The streaming loop below shrugs off a
    late reply by design, so letting one here end the run before streaming even
    starts would be perverse.
    """
    device, caps, board = connected(version=(1, 0), lights=[(0, 0)])
    board.pending.clear()
    board.write = lambda data: len(data)   # answers nothing from here on
    unmapped, _ = bridge.validate_targets(device, {'a': (('button', 0), RED)}, caps)
    assert unmapped == []


@pytest.mark.parametrize('render_hz, expected, why', [
    (40, 40.0, "matching the board's render rate"),
    (100, 60.0, 'capped'),
    (0, 60.0, 'did not state'),
])
def test_the_stream_rate_follows_the_board_up_to_the_cap(render_hz, expected, why):
    """Test that a slower board is matched exactly and a faster one is capped.

    Matching below the cap stops the bridge sending frames the board will never
    render. Capping above it keeps headroom for a slow poll or a retry, which is
    worth more than a rate nobody can see.
    """
    _, caps, _ = connected(version=(1, 3), render_hz=render_hz)
    rate, reason = bridge.resolve_stream_rate(None, caps)
    assert rate == expected
    assert why in reason


def test_an_explicit_rate_overrides_the_board():
    """Test that --fps still wins, in either direction."""
    _, caps, _ = connected(version=(1, 3), render_hz=40)
    assert bridge.resolve_stream_rate(100.0, caps) == (100.0, 'asked for with --fps')


def test_a_dry_run_has_a_rate_without_having_a_board():
    """Test that the absent capabilities resolve a rate rather than needing a special case."""
    rate, reason = bridge.resolve_stream_rate(None, hlp.Capabilities.absent())
    assert rate == bridge.DEFAULT_FPS
    assert 'no board' in reason


def test_a_changed_light_table_is_re_read_rather_than_only_reported():
    """Test that a fingerprint change replaces the cached table and the plan with it.

    A stale ordinal is still a perfectly valid ordinal, so a table that moved
    underneath the cache points at the wrong lights while every reply still says
    the frame applied.
    """
    device, caps, board = connected(version=(1, 3), lights=[(0, 0), (0, 1)])
    assert len(caps.staging_plan()[0][1]) == 2

    board.lights = [(0, 0, 1)]
    board.fingerprint = 99
    caps.refresh(device)
    assert caps.staging_plan() == {}
    assert caps.fingerprint == 99


def test_a_run_says_what_rate_it_settled_on():
    """Test that the chosen stream rate reaches the run's output, not just the helper."""
    lines = []
    bridge.run(OneShotConnection(), {'P1': (('button', 4), RED)},
               device=FakeBoard(version=(1, 3), render_hz=40).open(), report=lines.append)
    reported = '|'.join(lines)
    assert "streaming at 40 fps (matching the board's render rate)" in reported
