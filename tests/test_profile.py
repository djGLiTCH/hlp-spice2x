"""Test profile loading and frame resolution.

SPDX-FileCopyrightText: © 2026 Jacob Simpson
SPDX-License-Identifier: GPL-3.0-or-later
"""
import json
import pathlib

import pytest

from hlp_spice2x import profile as profile_module


def write(tmp_path, lights):
    """Write a profile file and return its path."""
    path = tmp_path / 'profile.json'
    path.write_text(json.dumps({'lights': lights}), encoding='utf-8')
    return str(path)


def test_button_entry_resolves_to_a_target(tmp_path):
    """Test that a control name becomes a protocol button ID."""
    path = write(tmp_path, {'P1 Button 1': {'button': 'B1', 'colour': 'FF0000'}})
    profile, _ = profile_module.load(path)
    assert profile['P1 Button 1'] == (('button', 4), (0xFF, 0, 0))


def test_special_controls_resolve(tmp_path):
    """Test that the case strip and player LEDs resolve to their special IDs."""
    path = write(tmp_path, {'Case': {'button': 'CASE'}, 'P1': {'button': 'PLED1'}})
    profile, _ = profile_module.load(path)
    assert profile['Case'][0] == ('button', 29)
    assert profile['P1'][0] == ('button', 24)


def test_range_entry_resolves(tmp_path):
    """Test that a raw range entry is kept as start and count."""
    path = write(tmp_path, {'Neon': {'range': [16, 30], 'colour': '00FF00'}})
    profile, _ = profile_module.load(path)
    assert profile['Neon'] == (('range', 16, 30), (0, 0xFF, 0))


def test_american_spelling_accepted(tmp_path):
    """Test that 'color' works as well as 'colour'."""
    path = write(tmp_path, {'A': {'button': 'B1', 'color': '0000FF'}})
    profile, _ = profile_module.load(path)
    assert profile['A'][1] == (0, 0, 0xFF)


def test_empty_button_is_skipped(tmp_path):
    """Test that an unfilled skeleton line is ignored rather than failing."""
    path = write(tmp_path, {'A': {'button': 'B1'}, 'B': {'button': '', 'colour': 'FFFFFF'}})
    profile, warnings = profile_module.load(path)
    assert set(profile) == {'A'}
    assert not warnings


def test_missing_keys_warn(tmp_path):
    """Test that an entry with neither key warns rather than silently vanishing."""
    path = write(tmp_path, {'A': {'button': 'B1'}, 'Typo': {'buton': 'B2'}})
    profile, warnings = profile_module.load(path)
    assert set(profile) == {'A'}
    assert any('Typo' in w for w in warnings)


def test_unknown_control_is_rejected(tmp_path):
    """Test that a bad control name fails loudly."""
    path = write(tmp_path, {'A': {'button': 'NOPE'}})
    with pytest.raises(profile_module.ProfileError, match='NOPE'):
        profile_module.load(path)


def test_range_past_the_buffer_is_rejected(tmp_path):
    """Test that a range running past the firmware's LED buffer fails at load."""
    path = write(tmp_path, {'A': {'range': [95, 20]}})
    with pytest.raises(profile_module.ProfileError, match='outside'):
        profile_module.load(path)


def test_bad_colour_is_rejected(tmp_path):
    """Test that a non-hex colour fails at load."""
    path = write(tmp_path, {'A': {'button': 'B1', 'colour': 'purple'}})
    with pytest.raises(profile_module.ProfileError, match='RRGGBB'):
        profile_module.load(path)


def test_profile_with_nothing_mapped_is_rejected(tmp_path):
    """Test that a wholly unfilled skeleton is an error, not an empty run."""
    path = write(tmp_path, {'A': {'button': '', 'colour': 'FFFFFF'}})
    with pytest.raises(profile_module.ProfileError, match='no mapped lights'):
        profile_module.load(path)


def test_skeleton_refuses_to_overwrite(tmp_path):
    """Test that writing a skeleton will not clobber an existing file."""
    path = tmp_path / 'existing.json'
    path.write_text('{}', encoding='utf-8')
    with pytest.raises(profile_module.ProfileError, match='already exists'):
        profile_module.write_skeleton(str(path), ['A'])


def test_state_scales_the_colour(tmp_path):
    """Test that a half-lit light produces a half-intensity colour."""
    path = write(tmp_path, {'A': {'button': 'B1', 'colour': 'FF0000'}})
    profile, _ = profile_module.load(path)
    frame = profile_module.resolve_frame({'A': 0.5}, profile)
    assert frame[('button', 4)] == (127, 0, 0)


def test_two_lights_on_one_control_add(tmp_path):
    """Test that duplicate targets combine additively and clamp."""
    path = write(tmp_path, {'A': {'button': 'B1', 'colour': 'FF0000'},
                            'B': {'button': 'B1', 'colour': '0000FF'}})
    profile, _ = profile_module.load(path)
    frame = profile_module.resolve_frame({'A': 1.0, 'B': 0.5}, profile)
    assert frame[('button', 4)] == (0xFF, 0, 127)


def test_lights_absent_from_the_profile_are_ignored(tmp_path):
    """Test that unmapped game lights do not appear in the frame."""
    path = write(tmp_path, {'A': {'button': 'B1'}})
    profile, _ = profile_module.load(path)
    frame = profile_module.resolve_frame({'A': 1.0, 'Unmapped': 1.0}, profile)
    assert list(frame) == [('button', 4)]


def test_off_lights_stage_black(tmp_path):
    """Test that a mapped light reported off is staged black, not omitted."""
    path = write(tmp_path, {'A': {'button': 'B1', 'colour': 'FF0000'}})
    profile, _ = profile_module.load(path)
    frame = profile_module.resolve_frame({'A': 0.0}, profile)
    assert frame[('button', 4)] == (0, 0, 0)


def test_format_frame_names_controls(tmp_path):
    """Test that dry-run output names controls rather than numbering them."""
    path = write(tmp_path, {'A': {'button': 'S2', 'colour': 'FFAA00'},
                            'B': {'range': [16, 4], 'colour': '112233'}})
    profile, _ = profile_module.load(path)
    text = profile_module.format_frame(profile_module.resolve_frame({'A': 1.0, 'B': 1.0}, profile))
    assert 'S2=#FFAA00' in text
    assert 'range@16+4=#112233' in text


def test_non_object_entry_is_rejected(tmp_path):
    """Test that a light mapped to something other than an object fails clearly."""
    path = write(tmp_path, {'A': "B1"})
    with pytest.raises(profile_module.ProfileError, match='expected an object'):
        profile_module.load(path)


def test_malformed_range_is_rejected(tmp_path):
    """Test that a range that is not a two-item list fails clearly."""
    path = write(tmp_path, {'A': {'range': ['x', 'y']}})
    with pytest.raises(profile_module.ProfileError, match=r'\[start, count\]'):
        profile_module.load(path)


def test_skeleton_lists_every_light(tmp_path):
    """Test that the generated skeleton has one unfilled entry per light."""
    path = str(tmp_path / 'skeleton.json')
    profile_module.write_skeleton(path, ['Zeta', 'Alpha'])
    written = json.loads(pathlib.Path(path).read_text(encoding='utf-8'))
    assert list(written['lights']) == ['Alpha', 'Zeta']
    assert all(e == {'button': '', 'colour': 'FFFFFF'} for e in written['lights'].values())


def test_empty_frame_is_described(tmp_path):
    """Test that a frame with nothing lit renders as readable text."""
    assert profile_module.format_frame({}) == '(all lights off)'


def test_an_index_picks_one_light_of_a_control(tmp_path):
    """Test that an indexed entry resolves to an unresolved per-light target.

    Which ordinal it names is a question only a board can answer, so load keeps
    the control and the index and leaves the resolving to the handshake.
    """
    path = write(tmp_path, {'Second Up': {'button': 'UP', 'index': 1, 'colour': 'FF0000'}})
    profile, _ = profile_module.load(path)
    assert profile['Second Up'] == (('light', 0, 1), (0xFF, 0, 0))


def test_index_zero_is_a_light_and_not_an_absent_key(tmp_path):
    """Test that the first light of a control is not mistaken for no index at all.

    Index 0 is both valid and falsy, so testing the value rather than its
    presence would quietly turn it back into a bare control name.
    """
    path = write(tmp_path, {'First Up': {'button': 'UP', 'index': 0}})
    profile, _ = profile_module.load(path)
    assert profile['First Up'][0] == ('light', 0, 0)


@pytest.mark.parametrize('entry', [{'index': 1}, {'button': '', 'index': 1}])
def test_an_index_with_nothing_to_index_into_is_an_error(tmp_path, entry):
    """Test that an index without a control is reported rather than skipped.

    Both shapes would otherwise fall into a path that treats them as an unfilled
    skeleton line, which is the wrong answer for an entry someone half filled in.
    """
    with pytest.raises(profile_module.ProfileError, match='needs a'):
        profile_module.load(write(tmp_path, {'A': entry}))


@pytest.mark.parametrize('index', [-1, 'first', None])
def test_an_index_that_is_not_a_light_number_is_rejected(tmp_path, index):
    """Test that a negative or non-numeric index fails at load rather than at connect."""
    with pytest.raises(profile_module.ProfileError, match='index'):
        profile_module.load(write(tmp_path, {'A': {'button': 'UP', 'index': index}}))


def test_the_extended_controls_are_accepted(tmp_path):
    """Test that the names only a light table can report resolve to their IDs."""
    path = write(tmp_path, {'a': {'button': 'A3'}, 'b': {'button': 'E1'},
                            'c': {'button': 'E12'}})
    profile, _ = profile_module.load(path)
    assert profile['a'][0] == ('button', 18)
    assert profile['b'][0] == ('button', 30)
    assert profile['c'][0] == ('button', 41)


def test_a_per_light_target_renders_symbolically_without_a_board():
    """Test that a dry run shows the entry as written, since it has no ordinal.

    A dry run has no board and so no light table, which is the only thing that
    could turn an index into an ordinal. Showing the control and the index is
    what the profile said, and it is checkable by eye.
    """
    assert profile_module.format_frame({('light', 0, 1): (255, 0, 0)}) == 'Up[1]=#FF0000'


def test_an_eight_digit_colour_asks_for_the_white_channel(tmp_path):
    """Test that RRGGBBWW parses as four components rather than being truncated."""
    path = write(tmp_path, {'A': {'button': 'B1', 'colour': 'FF000080'}})
    profile, _ = profile_module.load(path)
    assert profile['A'][1] == (0xFF, 0, 0, 0x80)


@pytest.mark.parametrize('colour', ['0xFF0000', 'FFAA', '  FF0000  ', 'FF_00_00',
                                    'FF00000', 'GGHHII', 12345])
def test_a_colour_that_is_not_six_or_eight_hex_digits_is_refused(tmp_path, colour):
    """Test that the length is checked rather than left to int(colour, 16).

    int() accepts a 0x prefix, underscores, surrounding whitespace and any
    number of digits, so 'FF0000FF' parsed as blue long before white existed.
    Telling six digits from eight is only safe once the length means something.
    """
    with pytest.raises(profile_module.ProfileError, match='hex digits'):
        profile_module.load(write(tmp_path, {'A': {'button': 'B1', 'colour': colour}}))


def test_two_lights_on_one_target_combine_across_different_widths():
    """Test that only one entry asking for white does not truncate the other.

    Zipping a three-component colour against a four-component one would drop the
    white silently, which is the kind of thing nobody notices until a board with
    a white chain is plugged in.
    """
    profile = {'a': (('button', 4), (10, 20, 30)), 'b': (('button', 4), (1, 2, 3, 40))}
    frame = profile_module.resolve_frame({'a': 1.0, 'b': 1.0}, profile)
    assert frame[('button', 4)] == (11, 22, 33, 40)


def test_a_white_component_is_shown_in_a_dry_run():
    """Test that dry-run output renders all four components rather than three."""
    assert profile_module.format_frame({('button', 4): (255, 0, 0, 128)}) == 'B1=#FF000080'
