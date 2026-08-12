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
