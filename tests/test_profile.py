# hlp-spice2x (spice2x to GP2040-CE Host Lighting bridge)
# Filename: test_profile.py
# Folder: tests
# https://github.com/djGLiTCH/hlp-spice2x
# SPDX-FileCopyrightText: (C) 2026 Jacob Simpson (DJ GLiTCH)
# SPDX-License-Identifier: GPL-3.0-or-later
# See LICENSES/GPL-3.0-or-later.txt at the repository root.

"""Test profile loading and frame resolution."""
import json
import pathlib

import pytest
from gp2040ce_hostlighting.protocol import ADDRESSING_ACTION, ADDRESSING_KIND

from hlp_spice2x import hlp
from hlp_spice2x import profile as profile_module

from .boards import FIRMWARE_ACTIONS


def write(tmp_path, lights):
    """Write a profile file and return its path."""
    path = tmp_path / 'profile.json'
    path.write_text(json.dumps({'lights': lights}), encoding='utf-8')
    return str(path)


def test_button_entry_resolves_to_a_target(tmp_path):
    """Test that a control name becomes the action STAGE reaches it by (addressing 6)."""
    path = write(tmp_path, {'P1 Button 1': {'button': 'B1', 'colour': 'FF0000'}})
    profile, _ = profile_module.load(path)
    assert profile['P1 Button 1'] == (('button', (6, 5)), (0xFF, 0, 0))


def test_special_controls_resolve(tmp_path):
    """Test that the case strip and player LEDs resolve to their kinds (addressing 7)."""
    path = write(tmp_path, {'Case': {'button': 'CASE'}, 'P1': {'button': 'PLED1'}})
    profile, _ = profile_module.load(path)
    assert profile['Case'][0] == ('button', (7, 1))
    assert profile['P1'][0] == ('button', (7, 3))


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
    """Test that an unfilled skeleton line is ignored."""
    path = write(tmp_path, {'A': {'button': 'B1'}, 'B': {'button': '', 'colour': 'FFFFFF'}})
    profile, warnings = profile_module.load(path)
    assert set(profile) == {'A'}
    assert not warnings


def test_missing_keys_warn(tmp_path):
    """Test that an entry with neither key warns."""
    path = write(tmp_path, {'A': {'button': 'B1'}, 'Typo': {'buton': 'B2'}})
    profile, warnings = profile_module.load(path)
    assert set(profile) == {'A'}
    assert any('Typo' in w for w in warnings)


def test_a_name_the_bridge_has_no_value_for_is_kept_for_the_boards_page_3(tmp_path):
    """Test that any other control name is kept as written, in upper case, to be resolved on a board."""
    path = write(tmp_path, {'A': {'button': ' button_press_e13 '}, 'B': {'button': 'Turbo', 'index': 1}})
    profile, _ = profile_module.load(path)
    assert profile['A'][0] == ('button', (hlp.FIRMWARE, 'BUTTON_PRESS_E13'))
    assert profile['B'][0] == ('light', (ADDRESSING_KIND, 2), 1)


@pytest.mark.parametrize('control', ['B' * 41, 'B\u00e91', 'B\t1'])
def test_a_control_name_no_board_can_give_is_rejected(tmp_path, control):
    """Test that a name longer than page 3's 40 bytes, or not printable ASCII, fails loudly."""
    path = write(tmp_path, {'A': {'button': control}})
    with pytest.raises(profile_module.ProfileError, match='printable ASCII of up to 40 bytes'):
        profile_module.load(path)


def test_a_short_name_is_looked_up_only_among_its_own_kind_of_value():
    """Test that an action named CASE leaves the short name CASE on the kind, and a firmware name takes the action."""
    index = hlp.name_index({'GpioAction': {99: 'CASE'}, 'LightType': {1: 'Case'}})
    assert hlp.resolve_control(hlp.CONTROL_TARGETS['CASE'], index) == (ADDRESSING_KIND, 1)
    assert hlp.resolve_control((hlp.FIRMWARE, 'CASE'), index) == (ADDRESSING_ACTION, 99)
    assert hlp.resolve_control((hlp.FIRMWARE, 'PLAYER2LIGHT'), index) is None


def test_resolving_against_page_3_takes_each_value_from_the_board():
    """Test resolve(): short and firmware names by page 3's value, ranges as written, the rest left out."""
    profile = {'a': (('button', hlp.CONTROL_TARGETS['B1']), (1, 2, 3)),
               'b': (('light', (hlp.FIRMWARE, 'PLAYER1LIGHT'), 0), (1, 2, 3)),
               'c': (('range', 16, 4), (1, 2, 3)),
               'd': (('button', hlp.CONTROL_TARGETS['B2']), (1, 2, 3))}
    names = {'GpioAction': {7: 'BUTTON_PRESS_B1'}, 'LightType': {3: 'Player1Light'}}
    resolved, missing = profile_module.resolve(profile, names)
    assert resolved == {'a': (('button', (ADDRESSING_ACTION, 7)), (1, 2, 3)),
                        'b': (('light', (ADDRESSING_KIND, 3), 0), (1, 2, 3)),
                        'c': (('range', 16, 4), (1, 2, 3))}
    assert missing == [hlp.CONTROL_TARGETS['B2']]


def test_range_past_the_wire_is_rejected(tmp_path):
    """Test that a range past the 16-bit LED index fails at load.

    How many LEDs a board addresses is its own to say, so that bound is applied
    at the handshake and not here.
    """
    assert profile_module.load(write(tmp_path, {'A': {'range': [95, 20]}}))[0]
    path = write(tmp_path, {'A': {'range': [65530, 20]}})
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
    assert frame[('button', (6, 5))] == (127, 0, 0)


def test_two_lights_on_one_control_add(tmp_path):
    """Test that duplicate targets combine additively and clamp."""
    path = write(tmp_path, {'A': {'button': 'B1', 'colour': 'FF0000'},
                            'B': {'button': 'B1', 'colour': '0000FF'}})
    profile, _ = profile_module.load(path)
    frame = profile_module.resolve_frame({'A': 1.0, 'B': 0.5}, profile)
    assert frame[('button', (6, 5))] == (0xFF, 0, 127)


def test_lights_absent_from_the_profile_are_ignored(tmp_path):
    """Test that unmapped game lights do not appear in the frame."""
    path = write(tmp_path, {'A': {'button': 'B1'}})
    profile, _ = profile_module.load(path)
    frame = profile_module.resolve_frame({'A': 1.0, 'Unmapped': 1.0}, profile)
    assert list(frame) == [('button', (6, 5))]


def test_off_lights_stage_black(tmp_path):
    """Test that a mapped light reported off is staged black, not omitted."""
    path = write(tmp_path, {'A': {'button': 'B1', 'colour': 'FF0000'}})
    profile, _ = profile_module.load(path)
    frame = profile_module.resolve_frame({'A': 0.0}, profile)
    assert frame[('button', (6, 5))] == (0, 0, 0)


def test_format_frame_names_controls(tmp_path):
    """Test that dry-run output names controls."""
    path = write(tmp_path, {'A': {'button': 'S2', 'colour': 'FFAA00'},
                            'B': {'range': [16, 4], 'colour': '112233'}})
    profile, _ = profile_module.load(path)
    text = profile_module.format_frame(profile_module.resolve_frame({'A': 1.0, 'B': 1.0}, profile))
    assert 'S2=#FFAA00' in text
    assert 'range@16+4=#112233' in text


def test_each_control_name_carries_the_value_the_firmware_names_it_by():
    """Test the bridge's values against the names HLP v2.0's proto/enums.proto gives them."""
    kinds = {1: 'Case', 2: 'Turbo', 3: 'Player1Light', 4: 'Player2Light', 5: 'Player3Light', 6: 'Player4Light'}
    for name, control in hlp.CONTROL_TARGETS.items():
        expected = FIRMWARE_ACTIONS if control[0] == ADDRESSING_ACTION else kinds
        assert hlp.FIRMWARE_NAMES[control] == expected[control[1]], name
    assert set(hlp.FIRMWARE_NAMES) == set(hlp.CONTROL_TARGETS.values())


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

    Which light it names is a question only a board can answer, so load keeps
    the control and the index and leaves the resolving to the handshake.
    """
    path = write(tmp_path, {'Second Up': {'button': 'UP', 'index': 1, 'colour': 'FF0000'}})
    profile, _ = profile_module.load(path)
    assert profile['Second Up'] == (('light', (6, 1), 1), (0xFF, 0, 0))


def test_index_zero_is_a_light_and_not_an_absent_key(tmp_path):
    """Test that the first light of a control is not mistaken for no index at all.

    Index 0 is both valid and falsy, so a test of the value would turn it back
    into a bare control name.
    """
    path = write(tmp_path, {'First Up': {'button': 'UP', 'index': 0}})
    profile, _ = profile_module.load(path)
    assert profile['First Up'][0] == ('light', (6, 1), 0)


@pytest.mark.parametrize('entry', [{'index': 1}, {'button': '', 'index': 1}])
def test_an_index_with_nothing_to_index_into_is_an_error(tmp_path, entry):
    """Test that an index without a control is reported.

    Both shapes are falsy as an unfilled skeleton line is, and must not be
    skipped as one.
    """
    with pytest.raises(profile_module.ProfileError, match='needs a'):
        profile_module.load(write(tmp_path, {'A': entry}))


@pytest.mark.parametrize('index', [-1, 'first', None])
def test_an_index_that_is_not_a_light_number_is_rejected(tmp_path, index):
    """Test that a negative or non-numeric index fails at load."""
    with pytest.raises(profile_module.ProfileError, match='index'):
        profile_module.load(write(tmp_path, {'A': {'button': 'UP', 'index': index}}))


def test_the_extended_controls_are_accepted(tmp_path):
    """Test that A3, A4 and E1-E12 resolve to their actions."""
    path = write(tmp_path, {'a': {'button': 'A3'}, 'b': {'button': 'E1'},
                            'c': {'button': 'E12'}})
    profile, _ = profile_module.load(path)
    assert profile['a'][0] == ('button', (6, 41))
    assert profile['b'][0] == ('button', (6, 43))
    assert profile['c'][0] == ('button', (6, 54))


def test_a_per_light_target_renders_symbolically_without_a_board():
    """Test that a dry run shows the entry as written, since it has no ordinal.

    A dry run has no board and so no Lights page, which is the only thing that
    could turn an index into an ordinal. Showing the control and the index is
    what the profile said, and it is checkable by eye.
    """
    assert profile_module.format_frame({('light', (6, 1), 1): (255, 0, 0)}) == 'Up[1]=#FF0000'


def test_an_eight_digit_colour_asks_for_the_white_channel(tmp_path):
    """Test that RRGGBBWW parses as four components."""
    path = write(tmp_path, {'A': {'button': 'B1', 'colour': 'FF000080'}})
    profile, _ = profile_module.load(path)
    assert profile['A'][1] == (0xFF, 0, 0, 0x80)


@pytest.mark.parametrize('colour', ['0xFF0000', 'FFAA', '  FF0000  ', 'FF_00_00',
                                    'FF00000', 'GGHHII', 12345])
def test_a_colour_that_is_not_six_or_eight_hex_digits_is_refused(tmp_path, colour):
    """Test that the length of a colour is checked.

    int() accepts a 0x prefix, underscores, surrounding whitespace and any
    number of digits, so it cannot tell six digits from eight.
    """
    with pytest.raises(profile_module.ProfileError, match='hex digits'):
        profile_module.load(write(tmp_path, {'A': {'button': 'B1', 'colour': colour}}))


def test_two_lights_on_one_target_combine_across_different_widths():
    """Test that only one entry asking for white does not truncate the other.

    Zipping a three-component colour against a four-component one would drop
    the white.
    """
    profile = {'a': (('button', (6, 5)), (10, 20, 30)), 'b': (('button', (6, 5)), (1, 2, 3, 40))}
    frame = profile_module.resolve_frame({'a': 1.0, 'b': 1.0}, profile)
    assert frame[('button', (6, 5))] == (11, 22, 33, 40)


def test_a_white_component_is_shown_in_a_dry_run():
    """Test that dry-run output renders all four components."""
    assert profile_module.format_frame({('button', (6, 5)): (255, 0, 0, 128)}) == 'B1=#FF000080'


@pytest.mark.parametrize('empty', [[], 0, None, ''])
def test_a_range_key_with_nothing_in_it_is_reported(tmp_path, empty):
    """Test that a falsy range is refused.

    Every one of these is falsy as an unfilled skeleton line is, and must not
    be skipped as one.
    """
    with pytest.raises(profile_module.ProfileError, match=r'\[start, count\]'):
        profile_module.load(write(tmp_path, {'A': {'range': empty}}))


def test_load_is_annotated_with_what_it_returns():
    """Test that the annotation agrees with the docstring and the call site."""
    assert profile_module.load.__annotations__['return'] is tuple


def test_every_profile_name_maps_to_its_action_or_kind():
    """Test the control names against GP2040-CE's GpioAction and LightType values."""
    targets = hlp.CONTROL_TARGETS
    assert [targets[name][1] for name in ('UP', 'DOWN', 'LEFT', 'RIGHT')] == [1, 2, 3, 4]
    assert [targets[name][1] for name in ('B1', 'B4', 'L1', 'R1', 'L2', 'R2')] == [5, 8, 9, 10, 11, 12]
    assert [targets[name][1] for name in ('S1', 'S2', 'A1', 'A2', 'L3', 'R3')] == [13, 14, 15, 16, 17, 18]
    assert [targets[name][1] for name in ('A3', 'A4', 'E1', 'E12')] == [41, 42, 43, 54]
    assert all(targets[name][0] == ADDRESSING_ACTION for name in ('UP', 'R3', 'A3', 'E12'))
    assert [targets[name] for name in ('CASE', 'TURBO', 'PLED1', 'PLED4')] == [
        (ADDRESSING_KIND, 1), (ADDRESSING_KIND, 2), (ADDRESSING_KIND, 3), (ADDRESSING_KIND, 6)]
    assert len(targets) == 38


# Names as DDR and SDVX report them. FX-R ends in R for right and BT-B is the
# B button, after a hyphen, and neither has the other two channels.
GAME_LIGHTS = ['GOLD P1 Card Unit B', 'GOLD P1 Card Unit G', 'GOLD P1 Card Unit R',
               'HD P1 Speaker W B', 'HD P1 Speaker W G', 'HD P1 Speaker W R',
               'GOLD Top Panel Avg B', 'GOLD Top Panel Avg G', 'GOLD Top Panel Avg R',
               'P1 Foot Right', 'Spot Red', 'Neon', 'FX-L', 'FX-R', 'BT-A', 'BT-B']


@pytest.mark.parametrize('channel, colour', [('R', (0xFF, 0, 0)), ('g', (0, 0xFF, 0)),
                                             (' B ', (0, 0, 0xFF)), ('W', (0xFF, 0xFF, 0xFF))])
def test_a_channel_entry_takes_its_primary_colour(tmp_path, channel, colour):
    """Test that a channel is read as its colour, W as a six-digit white so that it needs no white chain."""
    profile, _ = profile_module.load(write(tmp_path, {'Woofer': {'button': 'E1', 'channel': channel}}))
    assert profile['Woofer'] == (('button', (6, 43)), colour)


@pytest.mark.parametrize('entry, match', [
    ({'button': 'E1', 'channel': 'R', 'colour': 'FF0000'}, 'not both'),
    ({'button': 'E1', 'channel': 'R', 'color': 'FF0000'}, 'not both'),
    ({'button': 'E1', 'channel': 'red'}, 'R, G, B or W'),
    ({'button': 'E1', 'channel': 1}, 'R, G, B or W'),
])
def test_a_channel_that_cannot_be_read_is_refused(tmp_path, entry, match):
    """Test that a channel with a colour as well, or one that is not R, G, B or W, fails at load."""
    with pytest.raises(profile_module.ProfileError, match=match):
        profile_module.load(write(tmp_path, {'Woofer R': entry}))


@pytest.mark.parametrize('entry, match', [
    ({'button': '', 'channel': 'bogus'}, 'R, G, B or W'),
    ({'button': '', 'channel': 'R', 'colour': 'FF0000'}, 'not both'),
])
def test_a_channel_is_read_before_its_button_is_filled_in(tmp_path, entry, match):
    """Test that an unmapped light's channel is checked, as an unmapped fixture's channels are."""
    with pytest.raises(profile_module.ProfileError, match=match):
        profile_module.load(write(tmp_path, {'Woofer R': entry, 'b': {'button': 'B1'}}))


def test_an_unmapped_lights_colour_is_not_read_as_in_1_0(tmp_path):
    """Test that a bad colour on an unmapped light is still passed over, so a 1.0 profile loads as it did."""
    profile, _ = profile_module.load(write(tmp_path, {'a': {'button': '', 'colour': 'purple'},
                                                      'b': {'button': 'B1'}}))
    assert set(profile) == {'b'}


def test_a_fixture_maps_each_of_its_lights_to_one_target(tmp_path):
    """Test that a fixture becomes one entry per channel, keyed by the game's light name."""
    path = write(tmp_path, {'Top Panel': {'button': 'S2', 'channels': {
        'R': 'Top Panel R', 'G': 'Top Panel G', 'B': 'Top Panel B'}}})
    profile, _ = profile_module.load(path)
    assert profile == {'Top Panel R': (('button', (6, 14)), (0xFF, 0, 0)),
                       'Top Panel G': (('button', (6, 14)), (0, 0xFF, 0)),
                       'Top Panel B': (('button', (6, 14)), (0, 0, 0xFF))}


def test_a_fixture_shows_the_colour_the_game_mixes(tmp_path):
    """Test that a fixture's channels add on its target to the game's colour."""
    path = write(tmp_path, {'Woofer': {'button': 'B1', 'channels': {'R': 'Woofer R', 'G': 'Woofer G',
                                                                    'B': 'Woofer B'}}})
    profile, _ = profile_module.load(path)
    frame = profile_module.resolve_frame({'Woofer R': 1.0, 'Woofer G': 0.5, 'Woofer B': 0.0}, profile)
    assert frame == {('button', (6, 5)): (0xFF, 127, 0)}


def test_a_fixture_takes_an_index_or_a_range(tmp_path):
    """Test that a fixture is shown wherever a light entry could be."""
    channels = {'R': 'a R', 'G': 'a G', 'B': 'a B'}
    profile, _ = profile_module.load(write(tmp_path, {
        'a': {'button': 'UP', 'index': 1, 'channels': channels},
        'b': {'range': [16, 4], 'channels': {'R': 'b R', 'G': 'b G', 'B': 'b B'}}}))
    assert profile['a G'] == (('light', (6, 1), 1), (0, 0xFF, 0))
    assert profile['b B'] == (('range', 16, 4), (0, 0, 0xFF))


@pytest.mark.parametrize('extra', [{'colour': 'FF0000'}, {'color': 'FF0000'}, {'channel': 'R'}])
def test_a_fixture_with_a_colour_of_its_own_is_refused(tmp_path, extra):
    """Test that a fixture's colours come only from its channels."""
    entry = {'button': 'B1', 'channels': {'R': 'a R', 'G': 'a G', 'B': 'a B'}, **extra}
    with pytest.raises(profile_module.ProfileError, match="takes its colours from 'channels'"):
        profile_module.load(write(tmp_path, {'a': entry}))


@pytest.mark.parametrize('channels, match', [
    ({}, "must map R, G, B or W"),
    (['a R', 'a G'], "must map R, G, B or W"),
    ({'X': 'a X'}, 'R, G, B or W'),
    ({'R': 'a R', 'r': 'a r'}, 'given twice'),
    ({'R': ''}, 'must name a light'),
    ({'R': '  '}, 'must name a light'),
    ({'R': 7}, 'must name a light'),
    ({'R': 'a', 'G': 'a'}, 'named by two channels'),
])
def test_a_fixture_that_cannot_be_read_is_refused(tmp_path, channels, match):
    """Test that a malformed fixture fails at load, even before its button is filled in."""
    with pytest.raises(profile_module.ProfileError, match=match):
        profile_module.load(write(tmp_path, {'a': {'button': '', 'channels': channels},
                                             'b': {'button': 'B1'}}))


def test_an_unfilled_fixture_is_skipped(tmp_path):
    """Test that a fixture with an empty button maps nothing, as an unfilled light does."""
    profile, warnings = profile_module.load(write(tmp_path, {
        'a': {'button': '', 'channels': {'R': 'a R', 'G': 'a G', 'B': 'a B'}},
        'a R': {'button': 'B2', 'channel': 'R'}}))
    assert set(profile) == {'a R'}
    assert not warnings


@pytest.mark.parametrize('lights', [
    {'a': {'button': 'B1', 'channels': {'R': 'a R', 'G': 'a G', 'B': 'a B'}},
     'a R': {'button': 'B2', 'channel': 'R'}},
    {'a R': {'button': 'B2', 'channel': 'R'},
     'a': {'button': 'B1', 'channels': {'R': 'a R', 'G': 'a G', 'B': 'a B'}}},
    {'a': {'button': 'B1', 'channels': {'R': 'a R', 'G': 'a G', 'B': 'a B'}},
     'b': {'button': 'B2', 'channels': {'R': 'a R', 'G': 'b G', 'B': 'b B'}}},
])
def test_a_light_mapped_by_two_entries_is_refused(tmp_path, lights):
    """Test that a fixture's light given a second mapping fails, whichever comes first."""
    with pytest.raises(profile_module.ProfileError, match="'a R' is mapped by two entries"):
        profile_module.load(write(tmp_path, lights))


@pytest.mark.parametrize('text, key', [
    ('{"lights": {"A": {"button": "B1"}, "A": {"button": "B2"}}}', 'A'),
    ('{"lights": {"A": {"button": "B1", "button": "B2"}}}', 'button'),
    ('{"lights": {"F": {"button": "B2", "channels": {"R": "x", "G": "y", "B": "z", "R": "w"}}}}', 'R'),
])
def test_a_key_given_twice_in_one_object_warns_and_the_last_is_used(tmp_path, text, key):
    """Test that a repeated key, which JSON drops without a word, is named in a warning.

    The last one is used, as in 1.0, so a profile that loaded before loads alike.
    """
    path = tmp_path / 'profile.json'
    path.write_text(text, encoding='utf-8')
    profile, warnings = profile_module.load(str(path))
    assert warnings == [f"'{key}' is given twice in one object of the profile, and only the last is used"]
    target = profile.get('A', profile.get('w'))[0]
    assert target == ('button', (6, 6))


def test_a_profile_without_a_repeated_key_gives_no_warning(tmp_path):
    """Test that keys repeated only across different objects are not mistaken for a repeat."""
    profile, warnings = profile_module.load(write(tmp_path, {'A': {'button': 'B1'}, 'B': {'button': 'B2'}}))
    assert set(profile) == {'A', 'B'} and not warnings


def test_a_fixture_without_button_or_range_warns_by_its_name(tmp_path):
    """Test that the missing-keys warning names the fixture, not each of its lights."""
    profile, warnings = profile_module.load(write(tmp_path, {
        'a': {'buton': 'B1', 'channels': {'R': 'a R', 'G': 'a G', 'B': 'a B'}}, 'b': {'button': 'B1'}}))
    assert set(profile) == {'b'}
    assert warnings == ["fixture 'a' has no 'button' or 'range' key, ignoring"]


def test_fixtures_are_found_only_where_red_green_and_blue_are_all_present():
    """Test the grouping against a game's real names.

    FX-R, BT-B and Spot Red are not channels, and HD P1 Speaker W is a fixture whose
    W is part of its name and not a channel of its own.
    """
    assert profile_module.find_fixtures(GAME_LIGHTS) == {
        'GOLD P1 Card Unit': {'R': 'GOLD P1 Card Unit R', 'G': 'GOLD P1 Card Unit G',
                              'B': 'GOLD P1 Card Unit B'},
        'GOLD Top Panel Avg': {'R': 'GOLD Top Panel Avg R', 'G': 'GOLD Top Panel Avg G',
                               'B': 'GOLD Top Panel Avg B'},
        'HD P1 Speaker W': {'R': 'HD P1 Speaker W R', 'G': 'HD P1 Speaker W G',
                            'B': 'HD P1 Speaker W B'}}


@pytest.mark.parametrize('names, expected', [
    (['Wing R', 'Wing G'], {}),
    (['WingR', 'WingG', 'WingB', 'WingW'], {'Wing': {'R': 'WingR', 'G': 'WingG', 'B': 'WingB', 'W': 'WingW'}}),
    (['Wing_R', 'Wing_G', 'Wing_B'], {'Wing': {'R': 'Wing_R', 'G': 'Wing_G', 'B': 'Wing_B'}}),
    (['Wing', 'Wing R', 'Wing G', 'Wing B'], {'Wing RGB': {'R': 'Wing R', 'G': 'Wing G', 'B': 'Wing B'}}),
    (['Wing', 'Wing RGB', 'Wing R', 'Wing G', 'Wing B'], {}),
    (['Wing W'], {}),
])
def test_fixture_grouping_and_naming(names, expected):
    """Test the separators, a W that joins a group, and the name taken when a light already has it."""
    assert profile_module.find_fixtures(names) == expected


def test_the_skeleton_writes_a_fixture_as_one_entry(tmp_path):
    """Test that the skeleton groups a fixture's lights, and loads once its one button is filled in."""
    path = tmp_path / 'skeleton.json'
    fixtures = profile_module.write_skeleton(str(path), GAME_LIGHTS)
    written = json.loads(path.read_text(encoding='utf-8'))['lights']
    assert set(fixtures) == {'GOLD P1 Card Unit', 'GOLD Top Panel Avg', 'HD P1 Speaker W'}
    assert written['GOLD P1 Card Unit'] == {'button': '', 'channels': fixtures['GOLD P1 Card Unit']}
    assert written['FX-R'] == written['BT-B'] == {'button': '', 'colour': 'FFFFFF'}
    assert 'GOLD P1 Card Unit R' not in written
    assert list(written) == sorted(written)
    assert len(written) == len(GAME_LIGHTS) - 6

    written['GOLD P1 Card Unit']['button'] = 'B1'
    path.write_text(json.dumps({'lights': written}), encoding='utf-8')
    profile, _ = profile_module.load(str(path))
    assert profile['GOLD P1 Card Unit G'] == (('button', (6, 5)), (0, 0xFF, 0))
    assert set(profile) == {'GOLD P1 Card Unit R', 'GOLD P1 Card Unit G', 'GOLD P1 Card Unit B'}


def test_a_control_is_named_back_as_the_profile_wrote_it():
    """Test the reverse mapping, and a value with no name."""
    assert hlp.control_name(hlp.CONTROL_TARGETS['L3']) == 'L3'
    assert hlp.control_name(hlp.CONTROL_TARGETS['CASE']) == 'CASE'
    assert hlp.control_name((ADDRESSING_ACTION, 32)) == 'action 32'
    assert hlp.control_name((ADDRESSING_KIND, 9)) == 'kind 9'
