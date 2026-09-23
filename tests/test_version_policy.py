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

from .fake_board import M_ULTRA_CONTROLS, M_ULTRA_LIGHTS, FakeBoard, OneShotConnection

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


@pytest.mark.parametrize('minor, light_table, per_light, outcome_mask, control_table', [
    (0, False, False, False, False),
    (1, True, False, False, False),
    (2, True, True, False, False),
    (3, True, True, True, False),
    (4, True, True, True, True),
])
def test_each_version_offers_exactly_what_it_added(minor, light_table, per_light, outcome_mask,
                                                   control_table):
    """Test that each protocol version buys precisely the capabilities it introduced."""
    caps = negotiated(version=(1, minor))
    assert caps.light_table is light_table
    assert caps.per_light is per_light
    assert caps.outcome_mask is outcome_mask
    assert caps.control_table is control_table


@pytest.mark.parametrize('reported_minor', [5, 9, 255])
def test_a_newer_minor_is_driven_as_the_highest_known(reported_minor):
    """Test that a board newer than this client is clamped rather than refused.

    A minor version only ever adds, so a v1.5 board keeps every v1.4 promise.
    Refusing it would strand the bridge on firmware that is strictly better at
    everything it is being asked to do.
    """
    caps = negotiated(version=(1, reported_minor))
    assert caps.minor == hlp.MAX_SUPPORTED_MINOR
    assert caps.reported == (1, reported_minor)
    assert caps.forced is False


def test_a_newer_minor_behaves_exactly_like_the_highest_known():
    """Test that a clamped board takes the same paths as the version it clamps to."""
    newer, known = negotiated(version=(1, 5)), negotiated(version=(1, 4))
    for capability in ('light_table', 'per_light', 'outcome_mask', 'host_white', 'control_table'):
        assert getattr(newer, capability) == getattr(known, capability)
    assert newer.controls == known.controls


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


def control_table_requests(board) -> list:
    """Every GET_CAPS request the board was sent for page 6."""
    return [payload for payload in board.staged(hlp.CMD_GET_CAPS)
            if payload[0] == hlp.CAPS_PAGE_CONTROLS]


def m_ultra(version=(1, 4), **kwargs):
    """Describe the reference board, lights and pins both."""
    return FakeBoard(version=version, lights=M_ULTRA_LIGHTS, controls=M_ULTRA_CONTROLS, **kwargs)


def test_a_v1_4_board_negotiates_its_control_table():
    """Test that page 6 is read at connect and indexed by button ID."""
    caps = hlp.negotiate(m_ultra().open())
    assert caps.control_table is True
    assert len(caps.controls) == 21
    assert [record['gpio_pin'] for record in caps.pins[0]] == [2, 27]      # Up
    assert [record['gpio_pin'] for record in caps.pins[14]] == [18, 26]    # L3
    assert hlp.BUTTON_NONE not in caps.pins                                # turbo's pin


def test_bit_2_from_older_firmware_is_not_a_control_table():
    """Test that the control table needs v1.4 as well as the bit, which is reserved before it."""
    state = {'features': hlp.FEATURE_CONTROL_TABLE}
    assert hlp.HostLightingCapabilities(major=1, minor=3, state=state).control_table is False
    assert hlp.HostLightingCapabilities(major=1, minor=4, state=state).control_table is True


def test_page_6_is_not_read_when_the_feature_bit_is_clear():
    """Test that a cleared control-table bit is taken at its word."""
    board = m_ultra(control_table_feature=False)
    caps = hlp.negotiate(board.open())
    assert caps.control_table is False
    assert caps.controls == []
    assert control_table_requests(board) == []


@pytest.mark.parametrize('minor', [0, 1, 2, 3])
def test_page_6_is_never_requested_before_v1_4(minor):
    """Test that firmware older than the page is never asked for it."""
    board = FakeBoard(version=(1, minor))
    hlp.negotiate(board.open())
    assert control_table_requests(board) == []


def test_a_v1_4_board_refusing_page_6_has_an_empty_table():
    """Test that INVALID_ARG on page 6 reads as no table rather than as an error."""
    board = m_ultra()
    board._controls_page = lambda start: board._reply(status=2)
    caps = hlp.negotiate(board.open())
    assert caps.controls == []
    assert len(caps.lights) == 46


def test_a_page_6_read_that_fails_does_not_fail_the_connect():
    """Test that an unanswered page 6 leaves the board driven as it was at v1.3."""
    board = m_ultra()
    board._controls_page = lambda start: None      # never answered
    caps = hlp.negotiate(board.open())
    assert caps.controls == []
    assert len(caps.lights) == 46
    assert 'control table' not in caps.summary()


def test_a_board_going_away_during_page_6_is_still_reported():
    """Test that the advisory read does not swallow a vanished board."""
    board = m_ultra()
    device = board.open()
    caps = hlp.negotiate(device)
    original = board.write

    def unplugged(data):
        if data[1] == hlp.CMD_GET_CAPS and data[3] == hlp.CAPS_PAGE_CONTROLS:
            raise OSError('device disconnected')
        return original(data)

    board.write = unplugged
    with pytest.raises(hlp.HostLightingDisconnected):
        hlp.read_control_table(device, caps)


def test_refresh_re_reads_the_control_table():
    """Test that a fingerprint change re-reads page 6 along with the pages it certifies."""
    board = m_ultra()
    device = board.open()
    caps = hlp.negotiate(device)
    board.controls = M_ULTRA_CONTROLS[:12]
    caps.refresh(device)
    assert len(caps.controls) == 12


def test_a_failed_page_6_re_read_does_not_hold_up_the_light_table():
    """Test that refresh still lands the new light table when page 6 cannot be read."""
    board = m_ultra()
    device = board.open()
    caps = hlp.negotiate(device)
    board.lights = board.lights[:16]

    def stalled(start):
        reply = board._reply()
        reply[3], reply[6] = 21, 6      # claims 21 records, returns none
        return reply

    board._controls_page = stalled
    caps.refresh(device)
    assert len(caps.lights) == 16
    assert caps.controls == []


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
    assert hlp.HostLightingCapabilities.absent().led_extent == hlp.MAX_LEDS


def test_capabilities_without_a_board_answer_no_to_everything():
    """Test that a dry run has a defined absent form rather than needing None checks."""
    caps = hlp.HostLightingCapabilities.absent()
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


def test_the_startup_line_names_the_control_table_from_v1_4():
    """Test that the control table is named where it was read, and not before v1.4."""
    assert hlp.negotiate(m_ultra().open()).summary() == (
        "board speaks HLP v1.4 - light table, control table, per-light staging, "
        "outcome mask, renders at 40 Hz (white channel: no)")
    assert 'control table' not in hlp.negotiate(m_ultra(version=(1, 3)).open()).summary()


def test_the_startup_line_says_when_a_newer_board_is_being_clamped():
    """Test that a clamped board says so, rather than silently claiming to be v1.4."""
    assert 'driven as v1.4' in negotiated(version=(1, 7)).summary()
    assert 'driven as' not in negotiated(version=(1, 4)).summary()


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
