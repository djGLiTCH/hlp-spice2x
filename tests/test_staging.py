"""Test how a frame reaches the board's lights, across protocol versions.

The interesting behaviour is what a bare control name does on a board that
gives that control more than one light. Naming the control is still the primary
path, because that is what lets one profile work across layouts, but on a board
that publishes a light table the extra lights have to be staged explicitly or
they never light.

SPDX-FileCopyrightText: (C) 2026 Jacob Simpson
SPDX-License-Identifier: GPL-3.0-or-later
"""
import pytest

from hlp_spice2x import bridge, hlp, profile as profile_module

from .fake_board import M_ULTRA_LIGHTS, FakeBoard, OneShotConnection

RED = (255, 0, 0)


def connected(**kwargs):
    """Open a fake board, negotiate, and return the device, capabilities and board."""
    board = FakeBoard(**kwargs)
    device = board.open()
    caps = hlp.negotiate(device)
    board.requests.clear()
    return device, caps, board


def stage(frame, profile=None, **kwargs):
    """Stage one frame against a fake board and return the board it was staged on."""
    device, caps, board = connected(**kwargs)
    bridge.send_frame(device, frame, staging=bridge.staging_for(profile or {}, caps))
    return board


def lights(board):
    """Collect the (ordinal, colour) of every SET_LIGHT entry the board was sent."""
    return [(payload[1 + n * 4], tuple(payload[2 + n * 4:5 + n * 4]))
            for payload in board.staged(hlp.CMD_SET_LIGHT)
            for n in range(payload[0])]


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
        bridge.send_frame(device, {('sparkle', 3): RED})


def test_a_per_light_target_that_was_never_resolved_is_refused():
    """Test that an unresolved per-light target raises rather than staging nothing.

    Its ordinal is only knowable against a board, so one that reached staging
    without having been resolved is a wiring mistake, and a silent one at that.
    """
    device, _, _ = connected(version=(1, 3))
    with pytest.raises(ValueError, match='never resolved'):
        bridge.send_frame(device, {('light', 0, 1): RED}, staging={})


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
    rate, reason = bridge.resolve_stream_rate(None, hlp.HostLightingCapabilities.absent())
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


def test_the_expansion_switches_to_ordinals_once_the_board_offers_them():
    """Test that v1.2 addresses each light by its ordinal instead of by raw index.

    An ordinal names the whole record in one entry and is immune to page 2's
    best-effort LED bindings, so it is the better address wherever it exists.
    """
    board = stage({('button', 0): RED}, version=(1, 2), lights=M_ULTRA_LIGHTS)
    assert named(board) == [0]
    assert [ordinal for ordinal, _ in lights(board)] == [3, 12]
    assert ranges(board) == []


def test_a_per_light_target_colours_that_light_alone():
    """Test that an indexed entry stages one ordinal and does not name the control."""
    profile = {'Second Up': (('light', 0, 1), RED)}
    board = stage({('light', 0, 1): RED}, profile=profile, version=(1, 2),
                  lights=M_ULTRA_LIGHTS)
    assert named(board) == []
    assert lights(board) == [(12, RED)]


def test_an_index_counts_the_lights_the_table_gives_that_control():
    """Test that index 0 and index 1 pick the first and second of Up's two lights."""
    for index, ordinal in ((0, 3), (1, 12)):
        profile = {'Up': (('light', 0, index), RED)}
        board = stage({('light', 0, index): RED}, profile=profile, version=(1, 2),
                      lights=M_ULTRA_LIGHTS)
        assert lights(board) == [(ordinal, RED)]


def test_an_explicit_light_beats_the_expansion_of_its_own_control():
    """Test that naming one light wins over a control name that also covers it."""
    profile = {'Up': (('button', 0), RED), 'Second Up': (('light', 0, 1), (0, 0, 255))}
    board = stage({('button', 0): RED, ('light', 0, 1): (0, 0, 255)}, profile=profile,
                  version=(1, 2), lights=M_ULTRA_LIGHTS)
    assert lights(board) == [(3, RED), (12, RED), (12, (0, 0, 255))]


def test_a_per_light_target_on_firmware_too_old_is_refused_by_version():
    """Test that v1.1 refuses an indexed entry and names the firmware needed.

    A silent difference in which light lights is worse than a refusal that says
    what would fix it.
    """
    _, caps, _ = connected(version=(1, 1), lights=M_ULTRA_LIGHTS)
    with pytest.raises(hlp.HostLightingIncompatible, match='v1.2 or newer') as caught:
        bridge.staging_for({'Up': (('light', 0, 1), RED)}, caps)
    assert 'v1.1' in str(caught.value)


def test_a_per_light_target_past_the_end_of_a_control_is_refused():
    """Test that an index the board cannot offer says how many lights it does have."""
    _, caps, _ = connected(version=(1, 2), lights=M_ULTRA_LIGHTS)
    with pytest.raises(profile_module.ProfileError, match='out of range') as caught:
        bridge.staging_for({'Up': (('light', 0, 5), RED)}, caps)
    assert '2 lights' in str(caught.value)


def test_a_synthesised_table_is_named_as_the_reason_rather_than_the_firmware():
    """Test that a board whose table cannot show duplicates says so.

    Reporting this as an out-of-range index would send the user chasing a
    firmware upgrade that changes nothing, on a board that visibly has two Up
    buttons.
    """
    _, caps, _ = connected(version=(1, 3), lights=M_ULTRA_LIGHTS, synthesised=True)
    with pytest.raises(profile_module.ProfileError, match='synthesised') as caught:
        bridge.staging_for({'Up': (('light', 0, 1), RED)}, caps)
    assert 'board limitation' in str(caught.value)


def white_lights(board):
    """Collect the (ordinal, colour) of every SET_LIGHT_RGBW entry sent."""
    return [(payload[1 + n * 5], tuple(payload[2 + n * 5:6 + n * 5]))
            for payload in board.staged(hlp.CMD_SET_LIGHT_RGBW)
            for n in range(payload[0])]


def test_the_outcome_mask_names_the_ordinal_the_board_skipped():
    """Test that a stale ordinal is reported rather than silently doing nothing."""
    device, _, _ = connected(version=(1, 3), lights=M_ULTRA_LIGHTS)
    result = bridge.send_frame(device, {('button', 0): RED},
                               staging={('button', 0): (False, [('light', 99)])}, verify=True)
    assert result.skipped == [99]
    assert result.acknowledged is True


def test_a_frame_result_still_reads_as_a_plain_yes_or_no():
    """Test that widening the return kept the only thing callers used it for."""
    device, _, _ = connected(version=(1, 3))
    assert bridge.send_frame(device, {('button', 0): RED})


def test_the_mask_is_not_read_on_the_firmware_that_zero_fills_it():
    """Test that v1.2 is never asked, because its answer would be a lie.

    A v1.2 board zero-fills the outcome bytes, so reading them says every entry
    was skipped, including the ones that lit. The gate is the whole reason the
    capability exists separately from per-light staging.
    """
    _, caps, _ = connected(version=(1, 2), lights=M_ULTRA_LIGHTS)
    assert caps.per_light is True
    assert caps.outcome_mask is False

    device, _, _ = connected(version=(1, 2), lights=M_ULTRA_LIGHTS)
    result = bridge.send_frame(device, {('button', 0): RED},
                               staging={('button', 0): (False, [('light', 3)])})
    assert result.skipped == []


def test_reading_the_mask_on_a_v1_2_board_would_report_everything_skipped():
    """Test the trap itself, so the gate above cannot be removed without a failure."""
    device, _, _ = connected(version=(1, 2), lights=M_ULTRA_LIGHTS)
    applied, skipped, stale = device.set_lights([(3, 255, 0, 0)], outcomes=True)
    assert (applied, skipped) == (1, 0)
    assert stale == [3]


def test_a_skip_after_the_map_moved_re_reads_the_light_table():
    """Test that a stale ordinal is treated as staleness when the map really moved."""
    device, caps, board = connected(version=(1, 3), lights=[(0, 0), (0, 1)])
    board.lights = [(0, 0, 1)]
    board.fingerprint = 77
    lines = []
    assert bridge.react_to_skips(device, caps, {}, [1], lines.append) is True
    assert 're-read' in lines[0]
    assert caps.fingerprint == 77


def test_a_skip_with_no_map_change_says_the_profile_is_asking_for_too_much():
    """Test that an unchanged fingerprint stops the re-reading and blames the right thing.

    Re-reading a table that has not moved would return the same ordinals and
    the same skip, every frame, forever.
    """
    device, caps, _ = connected(version=(1, 3), lights=[(0, 0)])
    lines = []
    assert bridge.react_to_skips(device, caps, {}, [9], lines.append) is False
    assert 'does not have' in lines[0]


def test_a_colour_with_no_white_is_staged_exactly_as_before():
    """Test that adding white support changed nothing for profiles without it."""
    board = stage({('button', 4): RED}, version=(1, 3), colour_format=2,
                  lights=M_ULTRA_LIGHTS)
    assert named(board) == [4]
    assert white_lights(board) == []


def test_white_reaches_a_white_chain_on_firmware_that_honours_it():
    """Test that v1.3 plus a white chain routes the colour through SET_LIGHT_RGBW.

    SET_BUTTONS has no RGBW form, so a control asking for white has to be
    reached one light at a time instead of by name.
    """
    profile = {'Up': (('button', 0), (255, 255, 255, 0))}
    board = stage({('button', 0): (255, 255, 255, 0)}, profile=profile, version=(1, 3),
                  colour_format=2, lights=M_ULTRA_LIGHTS)
    assert named(board) == []
    assert white_lights(board) == [(3, (0, 0, 0, 255)), (12, (0, 0, 0, 255))]


def test_white_is_dropped_where_the_chain_has_no_emitter_for_it():
    """Test that a GRB board gets plain RGB, since it has nothing to send white to."""
    profile = {'Up': (('button', 0), (255, 255, 255, 255))}
    board = stage({('button', 0): (255, 255, 255, 255)}, profile=profile, version=(1, 3),
                  colour_format=0, lights=M_ULTRA_LIGHTS)
    assert named(board) == [0]
    assert white_lights(board) == []
    _, caps, _ = connected(version=(1, 3), colour_format=0, lights=M_ULTRA_LIGHTS)
    assert 'no white emitter' in bridge.white_warnings(profile, caps)[0]


def test_white_is_dropped_on_firmware_that_ignores_a_host_supplied_one():
    """Test that a white chain on v1.2 still gets RGB, which is what renders right there.

    Older firmware maps achromatic colours onto the white emitter itself, so
    sending the textbook subtractive white would render dark.
    """
    profile = {'Up': (('button', 0), (255, 255, 255, 255))}
    board = stage({('button', 0): (255, 255, 255, 255)}, profile=profile, version=(1, 2),
                  colour_format=2, lights=M_ULTRA_LIGHTS)
    assert named(board) == [0]
    assert white_lights(board) == []
    _, caps, _ = connected(version=(1, 2), colour_format=2, lights=M_ULTRA_LIGHTS)
    assert 'v1.3 honours it' in bridge.white_warnings(profile, caps)[0]


def test_a_per_light_target_carries_white_to_its_own_ordinal():
    """Test that an indexed entry asking for white stages one RGBW light."""
    profile = {'Second Up': (('light', 0, 1), (255, 0, 0, 128))}
    board = stage({('light', 0, 1): (255, 0, 0, 128)}, profile=profile, version=(1, 3),
                  colour_format=3, lights=M_ULTRA_LIGHTS)
    assert white_lights(board) == [(12, (255, 0, 0, 128))]


def test_a_raw_range_carries_white_through_the_rgbw_range_command():
    """Test that a range entry asking for white uses SET_RANGE_RGBW."""
    profile = {'Neon': (('range', 16, 2), (255, 255, 255, 0))}
    board = stage({('range', 16, 2): (255, 255, 255, 0)}, profile=profile, version=(1, 3),
                  colour_format=2, lights=M_ULTRA_LIGHTS)
    payloads = board.staged(hlp.CMD_SET_RANGE_RGBW)
    assert len(payloads) == 1
    assert payloads[0][:2] == bytes([16, 2])
    assert payloads[0][2:6] == bytes((0, 0, 0, 255))


def test_white_on_a_control_with_no_light_table_entry_is_reported():
    """Test that a control the table cannot break down is named, not silently dropped."""
    profile = {'B2': (('button', 5), (255, 255, 255, 0))}
    _, caps, _ = connected(version=(1, 3), colour_format=2, lights=[(0, 0)])
    assert 'B2' in bridge.white_warnings(profile, caps)[0]


def test_a_raw_range_past_the_board_is_reported_at_connect():
    """Test that a range written for a bigger board is named rather than lighting nothing.

    The load-time bound is the firmware's buffer ceiling, which is all that can
    be known without a board. It catches a typo; it does not catch a profile
    written for a 46-light board and run on a 16-light one.
    """
    _, caps, _ = connected(version=(1, 3), lights=[(n, n) for n in range(16)])
    profile = {'Far': (('range', 16, 30), RED), 'Near': (('range', 0, 4), RED),
               'Edge': (('range', 14, 4), RED)}
    assert bridge.ranges_past_the_board(profile, caps) == [(14, 4), (16, 30)]


def test_a_range_inside_the_board_is_not_reported():
    """Test that the warning stays quiet on a profile that fits."""
    _, caps, _ = connected(version=(1, 3), lights=M_ULTRA_LIGHTS)
    assert bridge.ranges_past_the_board({'Neon': (('range', 16, 30), RED)}, caps) == []


def test_a_board_that_reports_no_extent_falls_back_to_the_buffer_ceiling():
    """Test that a pre-v1.1 board or a dry run does not report every range as past the end."""
    assert bridge.ranges_past_the_board({'Neon': (('range', 16, 30), RED)},
                                        hlp.HostLightingCapabilities.absent()) == []


def test_a_board_with_no_light_table_yet_holds_per_light_entries_back():
    """Test that a clear feature bit on new-enough firmware is a race, not a verdict.

    The light registry is populated on the render core during LED setup, so a
    host that enumerates a moment early sees the bit clear on a board that would
    have answered a moment later. Refusing to start would turn that into a
    permanent judgement on a board that is merely still waking up.
    """
    _, caps, _ = connected(version=(1, 3), lights=M_ULTRA_LIGHTS, light_table_feature=False)
    profile = {'Second Up': (('light', 0, 1), RED)}
    assert bridge.deferred_lights(profile, caps) == ['Up[1]']
    assert bridge.staging_for(profile, caps)[('light', 0, 1)] == (False, [])


def test_a_held_back_entry_stages_nothing_rather_than_raising():
    """Test that a held-back target is passed over instead of tripping the guard."""
    profile = {'Second Up': (('light', 0, 1), RED)}
    board = stage({('light', 0, 1): RED}, profile=profile, version=(1, 3),
                  lights=M_ULTRA_LIGHTS, light_table_feature=False)
    assert lights(board) == []
    assert named(board) == []


def test_firmware_too_old_is_still_refused_rather_than_held_back():
    """Test that the version case keeps its hard error, which no waiting can fix."""
    _, caps, _ = connected(version=(1, 1), lights=M_ULTRA_LIGHTS)
    profile = {'Second Up': (('light', 0, 1), RED)}
    assert bridge.deferred_lights(profile, caps) == []
    with pytest.raises(hlp.HostLightingIncompatible):
        bridge.staging_for(profile, caps)


def test_a_held_back_entry_resolves_once_the_table_appears():
    """Test that the entry starts working by itself when the board catches up."""
    device, caps, board = connected(version=(1, 3), lights=M_ULTRA_LIGHTS,
                                    light_table_feature=False)
    profile = {'Second Up': (('light', 0, 1), RED)}
    assert bridge.staging_for(profile, caps)[('light', 0, 1)] == (False, [])

    board.light_table_feature = True
    board.fingerprint += 1
    caps.refresh(device)
    assert bridge.deferred_lights(profile, caps) == []
    assert bridge.staging_for(profile, caps)[('light', 0, 1)] == (False, [('light', 12)])


@pytest.mark.parametrize('white_first', [True, False])
def test_white_survives_whatever_order_the_profile_lists_its_entries_in(white_first):
    """Test that a second entry on one target cannot quietly drop the white channel.

    Two profile entries may name the same target, and they are merged into the
    wider of the two colours before staging. Deciding per entry whether white is
    in play would let whichever entry the profile happens to list last decide it,
    so swapping two lines of JSON would change what the LED emits.
    """
    plain = {'plain': (('light', 0, 0), (0, 0, 255))}
    coloured = {'white': (('light', 0, 0), (255, 0, 255, 128))}
    profile = dict(**coloured, **plain) if white_first else dict(**plain, **coloured)

    board = stage({('light', 0, 0): (255, 0, 255, 128)}, profile=profile, version=(1, 3),
                  colour_format=3, lights=M_ULTRA_LIGHTS)
    assert white_lights(board) == [(3, hlp.subtractive_white((255, 0, 255, 128)))]
    assert lights(board) == []
