"""Test what the bridge agrees to at connect, across protocol versions.

The protocol only ever adds within a major version, so a board newer than this
client is still a board this client can drive correctly, and refusing it would
be a bug rather than caution. A board with a different major version is the
opposite case: nothing it says can be trusted to mean what it used to.

Everything here goes through the real transport against a fake board, so the
handshake being tested is the one that runs against hardware.

SPDX-FileCopyrightText: (C) 2026 Jacob Simpson
SPDX-License-Identifier: GPL-3.0-or-later
"""
import pytest

from hlp_spice2x import bridge, hlp
from hlp_spice2x.__main__ import build_parser

from .fake_board import FakeBoard, OneShotConnection

RED_PROFILE = {'P1': (('button', 4), (255, 0, 0))}


def negotiated(**kwargs):
    """Negotiate against a fake board and return the capabilities."""
    return hlp.negotiate(FakeBoard(**kwargs).open())


def run_against(board, profile=None, **kwargs):
    """Run the bridge against a fake board and return everything it reported."""
    lines = []
    bridge.run(OneShotConnection(), RED_PROFILE if profile is None else profile,
               device=board.open(), report=lines.append, **kwargs)
    return '\n'.join(lines)


@pytest.mark.parametrize('minor, light_table, per_light, outcome_mask', [
    (0, False, False, False),
    (1, True, False, False),
    (2, True, True, False),
    (3, True, True, True),
])
def test_each_version_offers_exactly_what_it_added(minor, light_table, per_light, outcome_mask):
    """Test that each protocol version buys precisely the capabilities it introduced."""
    caps = negotiated(version=(1, minor))
    assert caps.light_table is light_table
    assert caps.per_light is per_light
    assert caps.outcome_mask is outcome_mask


@pytest.mark.parametrize('reported_minor', [4, 9, 255])
def test_a_newer_minor_is_driven_as_the_highest_known(reported_minor):
    """Test that a board newer than this client is clamped rather than refused.

    A minor version only ever adds, so a v1.4 board keeps every v1.3 promise.
    Refusing it would strand the bridge on firmware that is strictly better at
    everything it is being asked to do.
    """
    caps = negotiated(version=(1, reported_minor))
    assert caps.minor == hlp.MAX_SUPPORTED_MINOR
    assert caps.reported == (1, reported_minor)
    assert caps.forced is False


def test_a_newer_minor_behaves_exactly_like_the_highest_known():
    """Test that a clamped board takes the same paths as the version it clamps to."""
    newer, known = negotiated(version=(1, 4)), negotiated(version=(1, 3))
    for capability in ('light_table', 'per_light', 'outcome_mask', 'host_white'):
        assert getattr(newer, capability) == getattr(known, capability)


def test_a_different_major_is_refused_and_says_both_versions():
    """Test that a major version bump stops the run, naming what was found and what is supported.

    A major version is the one thing allowed to change what existing commands
    mean, so every assumption in this bridge is void against one.
    """
    with pytest.raises(hlp.HostLightingIncompatible) as caught:
        negotiated(version=(2, 0))
    message = str(caught.value)
    assert 'v2.0' in message
    assert f"v{hlp.SUPPORTED_MAJOR}.{hlp.MAX_SUPPORTED_MINOR}" in message
    assert '--force' in message


def test_major_zero_is_still_refused():
    """Test that a board below the first release is refused, as it always was."""
    with pytest.raises(hlp.HostLightingIncompatible):
        negotiated(version=(0, 9))


def test_force_overrides_a_refused_major():
    """Test that --force runs anyway, and says so on the capabilities it hands back.

    The minor version is then read exactly as it is for a supported major: taken
    at face value and clamped. Nothing better is available, since a major bump
    may have changed what its own minor means, so the rule that applies
    everywhere else applies here too rather than a second rule being invented
    for the one case nobody can reason about.
    """
    caps = hlp.negotiate(FakeBoard(version=(2, 0)).open(), force=True)
    assert caps.forced is True
    assert caps.reported == (2, 0)
    assert caps.minor == 0

    newer = hlp.negotiate(FakeBoard(version=(2, 7)).open(), force=True)
    assert newer.minor == hlp.MAX_SUPPORTED_MINOR


def test_something_that_is_not_a_host_lighting_interface_is_reported_as_such():
    """Test that a wrong magic fails the handshake rather than being read as a version."""
    with pytest.raises(hlp.HostLightingError, match='Host Lighting'):
        negotiated(magic=b'XXXX')


def test_the_light_table_is_not_gated_on_the_version_alone():
    """Test that a board can be new enough for page 5 and still report it empty.

    The light registry is populated on the render core during LED setup, so a
    host that enumerates early can legitimately see the feature bit clear. That
    is a board that is not ready, not a board that is too old, and reading the
    version alone would have the bridge trust a page that returns nothing.
    """
    caps = negotiated(version=(1, 3), light_table_feature=False)
    assert caps.per_light is True
    assert caps.light_table is False


def test_an_empty_light_table_still_answers_rather_than_failing():
    """Test that a board with the feature bit clear returns no records, not an error."""
    device = FakeBoard(version=(1, 3), light_table_feature=False).open()
    records, _ = device.read_lights()
    assert records == []


def test_page_five_does_not_exist_before_it_was_added():
    """Test that a v1.0 board refuses the light table the way unknown pages are refused."""
    device = FakeBoard(version=(1, 0)).open()
    with pytest.raises(hlp.HostLightingRejected):
        device.read_lights()


@pytest.mark.parametrize('colour_format, chain, host', [
    (0, False, False),   # GRB
    (1, False, False),   # RGB
    (2, True, True),     # GRBW
    (3, True, True),     # RGBW
])
def test_the_white_channel_comes_from_the_chain(colour_format, chain, host):
    """Test that whether white can be driven is read off the board's colour format."""
    caps = negotiated(version=(1, 3), colour_format=colour_format)
    assert caps.white_channel is chain
    assert caps.host_white is host


def test_a_white_chain_on_older_firmware_is_not_a_white_channel_a_host_can_drive():
    """Test that host-supplied white needs the firmware as well as the emitter.

    Before v1.3 the firmware maps achromatic colours onto the white emitter
    itself and ignores a host-supplied W, so a host that sends the textbook
    subtractive white gets darkness.
    """
    caps = negotiated(version=(1, 2), colour_format=2)
    assert caps.white_channel is True
    assert caps.host_white is False


def test_a_board_that_states_no_render_rate_reads_as_unstated():
    """Test that a board reporting zero render rate is not read as zero Hz."""
    assert negotiated(version=(1, 3), render_hz=0).render_hz is None
    assert negotiated(version=(1, 0)).render_hz is None


def test_the_board_reported_extent_is_preferred_to_the_buffer_ceiling():
    """Test that the board's own LED extent is used where it gives one."""
    assert negotiated(version=(1, 3), led_extent=128).led_extent == 128
    assert hlp.Capabilities.absent().led_extent == hlp.MAX_LEDS


def test_capabilities_without_a_board_answer_no_to_everything():
    """Test that a dry run has a defined absent form rather than needing None checks."""
    caps = hlp.Capabilities.absent()
    assert caps.connected is False
    assert caps.light_table is False
    assert caps.per_light is False
    assert caps.outcome_mask is False
    assert caps.host_white is False
    assert caps.render_hz is None
    assert caps.fingerprint is None


def test_the_startup_line_names_the_version_and_what_it_buys():
    """Test that the negotiated version and the paths it opens are visible at startup."""
    newest = negotiated(version=(1, 3)).summary()
    assert 'v1.3' in newest
    assert 'light table' in newest
    assert 'per-light staging' in newest
    assert 'outcome mask' in newest

    oldest = negotiated(version=(1, 0)).summary()
    assert 'v1.0' in oldest
    assert 'no light table' in oldest
    assert 'no per-light staging' in oldest
    assert 'outcome mask' not in oldest


def test_the_startup_line_says_when_a_newer_board_is_being_clamped():
    """Test that a clamped board says so, rather than silently claiming to be v1.3."""
    assert 'driven as v1.3' in negotiated(version=(1, 7)).summary()
    assert 'driven as' not in negotiated(version=(1, 3)).summary()


def test_the_startup_line_reports_the_white_channel():
    """Test that a chain with no white emitter is stated rather than left to be guessed."""
    assert '(white channel: no)' in negotiated(version=(1, 3), colour_format=0).summary()
    assert '(white channel: yes)' in negotiated(version=(1, 3), colour_format=2).summary()


def test_the_run_reports_the_negotiated_capabilities():
    """Test that the startup line reaches the run's own output, not just the object."""
    text = run_against(FakeBoard(version=(1, 3)))
    assert 'board speaks HLP v1.3' in text


def test_the_startup_line_never_blames_the_game_side():
    """Test that startup output stays clear of the word the disconnect test rules out.

    A vanished board must not be reported as a lost game connection, and that is
    asserted by looking for one word in everything the run printed.
    """
    assert 'spice2x' not in run_against(FakeBoard(version=(1, 3)))


def test_a_board_speaking_a_newer_major_stops_the_run():
    """Test that the refusal happens at connect, before any frame is staged."""
    board = FakeBoard(version=(2, 0))
    with pytest.raises(hlp.HostLightingIncompatible):
        run_against(board)
    assert board.staged(hlp.CMD_SET_BUTTONS) == []
    assert board.staged(hlp.CMD_COMMIT) == []


def test_force_runs_a_newer_major_behind_a_loud_warning():
    """Test that the escape hatch works and cannot be missed in the output."""
    text = run_against(FakeBoard(version=(2, 0)), force=True)
    assert 'WARNING' in text
    assert 'v2.0' in text
    assert 'does not support' in text
    assert 'is reliable' in text


def test_fps_is_unset_unless_it_is_asked_for():
    """Test that the poll rate carries no answer until something resolves one.

    A concrete default bound at argument-parsing time cannot be told apart from
    a user asking for that same number, which leaves the board no way to have a
    say in its own stream rate.
    """
    assert build_parser().parse_args([]).fps is None
    assert build_parser().parse_args(['--fps', '30']).fps == 30.0
