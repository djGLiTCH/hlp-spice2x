"""Test that a bad reply to the loop's housekeeping does not end a working run.

The streaming loop shrugs off a dropped reply everywhere it can: a late COMMIT,
an unanswered keepalive, a probe that never came back, the clear after those
probes. The round trips that keep the board's LED map up to date have to behave
the same way. They fire every five seconds for as long as the game runs, so
being the one fatal round trip would make them the most likely thing to end an
otherwise healthy session.

What must still end the run is the board going away, which is not a bad reply
but the absence of a board.

SPDX-FileCopyrightText: (C) 2026 Jacob Simpson
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


def test_a_failed_refresh_does_not_stop_a_later_one_landing():
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
    assert bridge.react_to_skips(device, caps, [99], lines.append) is False
    assert 're-reading its light table failed' in lines[0]


class Clock:
    """A clock the test advances itself, so a five-second poll costs no time."""

    def __init__(self):
        """Start somewhere far from zero, as a real monotonic clock would be."""
        self.now = 1000.0

    def monotonic(self):
        """Report the current time."""
        return self.now

    def sleep(self, seconds):
        """Advance instead of waiting."""
        self.now += max(seconds, 0.0)


class Reconfiguring:
    """A connection that moves the board's LED map, failing its first re-read."""

    def __init__(self, board, clock, polls=4):
        """Hold the board to reconfigure and the clock to push past each poll."""
        self.board = board
        self.clock = clock
        self.polls = 0
        self.limit = polls

    def lights_read(self):
        """Push the clock past the next LED-map poll, moving the map once."""
        self.polls += 1
        self.clock.now += 5.0
        if self.polls == 1:
            original = self.board._lights_page

            def unanswered(start):
                self.board._lights_page = original   # only the first walk fails
                return None
            self.board._lights_page = unanswered
            self.board.fingerprint += 1
        if self.polls > self.limit:
            raise KeyboardInterrupt
        return {'P1 Up': 1.0}


def test_a_change_whose_re_read_failed_is_still_outstanding_next_poll(monkeypatch):
    """Test that a map change survives the re-read that failed on it.

    Recording the new fingerprint before the re-read has landed leaves it
    standing against a stale light table, and every later poll then compares
    against the value the failed attempt wrote and sees nothing to do. The board
    is never re-read, and the profile lights the wrong LEDs for the rest of the
    run behind a single warning.

    This has to drive the loop rather than the refresh, because the loop's own
    copy of the fingerprint is the thing under test.
    """
    board = FakeBoard(version=(1, 3), lights=M_ULTRA_LIGHTS)
    clock = Clock()
    monkeypatch.setattr(bridge, 'time', clock)
    lines = []
    bridge.run(Reconfiguring(board, clock), PROFILE, device=board.open(), report=lines.append)
    text = '\n'.join(lines)
    assert 'could not re-read' in text          # the first attempt failed
    assert 'controls re-resolved' in text       # and a later one still picked it up


def test_a_board_that_stops_fitting_the_profile_does_not_end_the_run():
    """Test that a mid-run re-resolve failure holds entries back rather than stopping.

    Refusing a profile the board cannot light belongs at startup, where it costs
    nothing and the message is the point. The same refusal partway through a game
    costs the session, and the entries that no longer fit are only a subset:
    everything else can still be lit correctly.
    """
    board = FakeBoard(version=(1, 3), lights=M_ULTRA_LIGHTS)
    device = board.open()
    caps = hlp.negotiate(device)
    profile = {'P1 Up': (('button', 0), RED), 'Third Up': (('light', 0, 5), RED)}

    lines = []
    staging = bridge.restage(profile, caps, {}, lines.append)
    assert 'out of range' in lines[0]
    assert staging[('light', 0, 5)] == (False, [])
    assert staging[('button', 0)][1]            # the rest of the profile still resolves


def test_the_same_profile_is_still_refused_at_startup():
    """Test that holding entries back mid-run did not soften the startup refusal."""
    _, caps, _ = connected(version=(1, 3), lights=M_ULTRA_LIGHTS)
    with pytest.raises(Exception, match='out of range'):
        bridge.staging_for({'Third Up': (('light', 0, 5), RED)}, caps)


def connected(**kwargs):
    """Open a fake board, negotiate, and return the device, capabilities and board."""
    board = FakeBoard(**kwargs)
    device = board.open()
    return device, hlp.negotiate(device), board


@pytest.mark.parametrize('version, path', [((1, 3), 'light table'), ((1, 0), 'write probe')])
def test_a_run_starts_from_a_cleared_staging_buffer(version, path):
    """Test that startup clears staging whichever way targets were discovered.

    The staging buffer outlives a session. Pixels another host left staged, or
    this one left on its own last run, are republished by every commit and never
    overwritten, because nothing in the new profile stages them. Discovering
    targets by reading the light table issues no writes at all, which is how the
    clear came to be lost along with the writes it used to tidy up after.
    """
    board = FakeBoard(version=version, lights=M_ULTRA_LIGHTS)
    lines = []
    bridge.run(Nudging(board, polls=1), PROFILE, device=board.open(), report=lines.append)
    staged = [command for command, _ in board.requests]
    commit = staged.index(hlp.CMD_COMMIT)
    assert hlp.CMD_CLEAR in staged[:commit], f"no CLEAR before the first frame ({path})"


@pytest.mark.parametrize('how', ['drop', 'reject'])
def test_a_lost_staging_receipt_does_not_end_the_run(how):
    """Test that asking the board what it staged is not riskier than not asking.

    Per-light entries are written before the reply is waited for, so they are
    staged whether or not the receipt comes back. All that is lost is knowing
    what the board made of them, and the receipt is asked for at exactly the
    moment the board is busiest reconfiguring itself.
    """
    board = FakeBoard(version=(1, 3), lights=M_ULTRA_LIGHTS)

    def misbehave(target):
        """Stop answering the per-light staging command, one way or the other."""
        getattr(target, how).add(hlp.CMD_SET_LIGHT)

    text = run_with(board, misbehave)
    assert 'frames in' in text          # the run reached its own summary


def test_the_run_reads_the_outcome_mask_and_reports_what_was_skipped():
    """Test that the reactive half of v1.3 is wired into the loop, not just present.

    The mask is read on the first frame after the ordinals are resolved, and a
    skip it reports is what triggers the light table being re-read. Both halves
    live in run(), so this has to drive run() - asserting on send_frame and
    react_to_skips directly would pass just as happily against a loop that never
    asked for the mask at all.

    The board loses a light after its table was read, so a cached ordinal that
    was valid at connect no longer is. Its fingerprint does not move, which is
    what tells the bridge the profile is asking for more than the board has.
    """
    board = FakeBoard(version=(1, 3), lights=[(0, 0), (0, 1)])

    def shrink(target):
        """Take one of Up's two lights away, leaving the fingerprint alone."""
        target.lights = [(0, 0, 1)]

    text = run_with(board, shrink)
    assert 'board skipped light(s)' in text
    assert 'does not have' in text


def test_a_lost_receipt_does_not_abandon_the_rest_of_the_batch():
    """Test that every entry still reaches the board when one receipt goes missing.

    Per-light entries are sent one report at a time, and each waits for its reply
    before the next is written, so a reply that never comes takes every report
    behind it with it. Swallowing that quietly would leave most of a large
    control unstaged and publish the frame anyway, which is worse than the dead
    session it was meant to avoid: it is silent.

    A control with thirty lights and a white colour is the case that shows it,
    because the RGBW form carries twelve entries per report.
    """
    board = FakeBoard(version=(1, 3), colour_format=3, lights=M_ULTRA_LIGHTS)
    device = board.open()
    caps = hlp.negotiate(device)
    profile = {'Neon': (('button', 29), (255, 255, 255, 255))}
    staging = bridge.staging_for(profile, caps)
    assert len(staging[('button', 29)][1]) == 30    # more than one report's worth

    answered = board._stage_lights
    lost = []

    def lose_the_first(command, payload):
        """Leave the first report unanswered, then behave normally."""
        if not lost:
            lost.append(True)
            return None
        return answered(command, payload)

    board._stage_lights = lose_the_first
    board.requests.clear()
    result = bridge.send_frame(device, {('button', 29): (255, 255, 255, 255)},
                               staging=staging, verify=True)

    staged = {payload[1 + n * 5] for payload in board.staged(hlp.CMD_SET_LIGHT_RGBW)
              for n in range(payload[0])}
    assert len(staged) == 30, f"only {len(staged)} of 30 lights reached the board"
    assert result.acknowledged is True


def test_a_truncated_receipt_is_treated_as_a_lost_one():
    """Test that a short reply takes the resend path rather than ending the run.

    A reply only has to be long enough to match the request, so one too short to
    hold the outcome mask matches and then fails on the read. Withholding a reply
    and mangling one have to end the same way.
    """
    board = FakeBoard(version=(1, 3), lights=M_ULTRA_LIGHTS)
    answered = board._stage_lights
    board._stage_lights = lambda command, payload: bytearray(answered(command, payload)[:5])

    text = run_with(board)
    assert 'frames in' in text
