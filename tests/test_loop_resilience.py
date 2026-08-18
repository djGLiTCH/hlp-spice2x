"""Test that a bad reply to the loop's housekeeping does not end a working run.

The streaming loop shrugs off a dropped reply everywhere it can: a late COMMIT,
an unanswered keepalive, a probe that never came back, the clear after those
probes. The round trips that keep the board's LED map up to date have to behave
the same way. They fire every five seconds for as long as the game runs, so
being the one fatal round trip would make them the most likely thing to end an
otherwise healthy session.

What must still end the run is the board going away, which is not a bad reply
but the absence of a board.

SPDX-FileCopyrightText: © 2026 Jacob Simpson
SPDX-License-Identifier: GPL-3.0-or-later
"""
import pytest

from hlp_spice2x import bridge, hlp

from .fake_board import M_ULTRA_LIGHTS, FakeBoard

RED = (255, 0, 0)
PROFILE = {'P1 Up': (('button', 0), RED)}


class Nudging:
    """A connection that changes the board partway through, then ends the run."""

    def __init__(self, board, polls=3, on_first=None):
        """Hold the board to nudge and what to do to it on the first poll."""
        self.board = board
        self.polls = 0
        self.limit = polls
        self.on_first = on_first

    def lights_read(self):
        """Nudge the board once, answer a few polls, then interrupt."""
        self.polls += 1
        if self.polls == 1 and self.on_first is not None:
            self.on_first(self.board)
        if self.polls > self.limit:
            raise KeyboardInterrupt
        return {'P1 Up': 1.0}


def run_with(board, on_first=None):
    """Run the bridge against a fake board and return everything it reported."""
    lines = []
    bridge.run(Nudging(board, on_first=on_first), PROFILE, device=board.open(),
               report=lines.append)
    return '\n'.join(lines)


def test_a_dropped_led_map_poll_does_not_end_the_run():
    """Test that an unanswered capability read is retried rather than fatal.

    This poll happens every five seconds for the life of the run, so making it
    the one round trip that cannot fail would make it the likeliest way for a
    healthy session to stop.
    """
    board = FakeBoard(version=(1, 3), lights=M_ULTRA_LIGHTS)
    text = run_with(board, lambda b: b.drop.add(hlp.CMD_GET_CAPS))
    assert 'could not re-read' in text
    assert 'retrying in 5s' in text
    assert 'frames in' in text  # the run reached its own summary


def test_a_rejected_keepalive_does_not_end_the_run():
    """Test that a board answering the keepalive non-OK is tolerated.

    The keepalive exists to hold the takeover open. A board that refuses one is
    worth carrying on past, the same as one that simply does not answer.
    """
    board = FakeBoard(version=(1, 3), lights=M_ULTRA_LIGHTS)
    lines = []
    # rejected only once the handshake is past, and with a short keepalive so the
    # loop reaches one inside the poll budget
    bridge.run(Nudging(board, polls=40, on_first=lambda b: b.reject.add(hlp.CMD_PING)),
               PROFILE, device=board.open(), timeout_ms=200, report=lines.append)
    assert board.reject == {hlp.CMD_PING}
    assert 'frames in' in '\n'.join(lines)


def test_a_light_table_that_will_not_settle_does_not_end_the_run():
    """Test that a table churning under its own re-read is reported, not fatal.

    A walk that keeps straddling a change raises rather than returning a spliced
    table, and the churn happens during exactly the reconfiguration that moved
    the fingerprint in the first place.
    """
    board = FakeBoard(version=(1, 3), lights=M_ULTRA_LIGHTS)

    def churn(target):
        """Move the fingerprint on every single read."""
        original = target._lights_page

        def unsettled(start):
            target.fingerprint += 1
            return original(start)
        target._lights_page = unsettled
        target.fingerprint += 1

    text = run_with(board, churn)
    assert 'could not re-read' in text
    assert 'frames in' in text


def test_the_fingerprint_is_not_advanced_by_a_re_read_that_failed():
    """Test that a half-finished refresh does not hide the change it failed on.

    The refresh replaces page 1 before it walks the light table, so recording
    the new fingerprint first would leave it standing against a stale table.
    Nothing would ever notice, because the next poll compares against the value
    the failed attempt wrote.
    """
    board = FakeBoard(version=(1, 3), lights=M_ULTRA_LIGHTS)
    device = board.open()
    caps = hlp.negotiate(device)
    before = caps.fingerprint

    board.fingerprint += 1
    board.drop.add(hlp.CMD_GET_CAPS)
    with pytest.raises(hlp.HostLightingError):
        caps.refresh(device)

    board.drop.clear()
    caps.refresh(device)
    assert caps.fingerprint != before
    assert len(caps.lights) == len(M_ULTRA_LIGHTS)


def test_a_board_going_away_still_ends_the_run():
    """Test that widening the housekeeping catch did not swallow a disconnect.

    A disconnect is not a bad reply, it is the absence of a board, and carrying
    on would leave a quiet game pinging a dead one forever.
    """
    board = FakeBoard(version=(1, 3), lights=M_ULTRA_LIGHTS)

    def unplug(target):
        """Fail every write the way an unplugged device does."""
        def gone(data):
            raise OSError('device disconnected')
        target.write = gone

    text = run_with(board, unplug)
    assert 'board disconnected' in text
    assert 'spice2x' not in text


def test_a_truncated_probe_reply_is_no_more_evidence_than_a_missing_one():
    """Test that a short reply to a target probe does not crash the handshake.

    The only length contract on a reply is that it is long enough to match the
    request, so reading the skipped count out of one is reading past the end of
    a report that a flaky link could deliver.
    """
    board = FakeBoard(version=(1, 0), lights=[(0, 0)])
    device = board.open()
    caps = hlp.negotiate(device)

    original = board._stage_buttons
    board._stage_buttons = lambda payload: bytearray(original(payload)[:4])
    unmapped, unreachable = bridge.validate_targets(device, PROFILE, caps)
    assert unmapped == []
    assert unreachable == []


def test_a_failed_re_read_after_a_skip_is_reported_not_raised():
    """Test that the reactive path survives a board that will not answer it."""
    board = FakeBoard(version=(1, 3), lights=M_ULTRA_LIGHTS)
    device = board.open()
    caps = hlp.negotiate(device)
    board.drop.add(hlp.CMD_GET_CAPS)

    lines = []
    assert bridge.react_to_skips(device, caps, PROFILE, [99], lines.append) is False
    assert 're-reading its light table failed' in lines[0]
