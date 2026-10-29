# hlp-spice2x (spice2x to GP2040-CE Host Lighting bridge)
# Filename: test_device_errors.py
# Folder: tests
# https://github.com/djGLiTCH/hlp-spice2x
# SPDX-FileCopyrightText: (C) 2026 Jacob Simpson (DJ GLiTCH)
# SPDX-License-Identifier: GPL-3.0-or-later
# See LICENSES/GPL-3.0-or-later.txt at the repository root.

"""Test finding a board, and a frame or a run when the board goes away.

The protocol's transport is gp2040ce-hostlighting's, tested there. These test
the bridge's own part: its messages for finding a board when none, or more
than one, can be chosen, its listing of the boards, and what a frame or a run
does when the board is lost.
"""
import struct

import pytest
from gp2040ce_hostlighting import protocol as hl
from gp2040ce_hostlighting.testing import HELLO_PAGES, attach

from hlp_spice2x import bridge, hlp

from .boards import Board, on_clock

B1 = hlp.CONTROL_TARGETS['B1']


class FakeHid:
    """Stands in for the hidapi device object inside HostLightingDevice."""

    def __init__(self, write_error=None, read_error=None):
        """Script the device with an error to raise on write or on read."""
        self.write_error = write_error
        self.read_error = read_error
        self.writes = []

    def write(self, data):
        """Record the write, or fail the way an unplugged device does."""
        if self.write_error is not None:
            raise self.write_error
        self.writes.append(bytes(data))
        return len(data)

    def read(self, size, timeout_ms=0):
        """Return nothing at all, or fail the way an unplugged device does."""
        if self.read_error is not None:
            raise self.read_error
        return []

    def close(self):
        """Match the hidapi interface."""


def make_device(**kwargs):
    """Build a HostLightingDevice around a FakeHid without opening real hardware."""
    return hl.HostLightingDevice(handle=FakeHid(**kwargs))


def test_an_identity_string_that_did_not_fit_is_asked_for_on_its_own():
    """Test that the firmware version is still read when the label fills the reply."""
    board = Board(label='L' * 48, firmware='v0.7.12-154-gc3499c2')
    device = board.open()
    assert hlp.label_and_firmware(device) == ('L' * 48, 'v0.7.12-154-gc3499c2')
    assert [struct.unpack_from('<H', request, 10)[0] for request in board.sent(hl.CMD_GET_PAGE)] == [
        0x09, 0x08]


def test_no_board_points_at_the_firmware_builds(monkeypatch):
    """Test that the not-found error says where to get firmware.

    Host Lighting is not in an official GP2040-CE release, and a user on stock
    firmware has no add-on setting to check.
    """
    attach(monkeypatch)
    with pytest.raises(hl.HostLightingError) as caught:
        hlp.open_device()
    message = str(caught.value)
    assert 'add-on enabled' in message
    assert 'releases/tag/HLP_v2.0-rc11' in message
    assert 'another protocol major' not in message


def test_a_board_on_protocol_1_is_named_rather_than_missed(monkeypatch):
    """Test that a 1.x board, which this version cannot find by usage, is still explained."""
    attach(monkeypatch, other_majors=[hl.USAGE_PROTOCOL_1])
    with pytest.raises(hl.HostLightingError) as caught:
        hlp.open_device()
    message = str(caught.value)
    assert '1 Host Lighting interface(s) found speaking another protocol major' in message
    assert 'hlp-spice2x v0.3.0' in message


def test_an_interface_that_answers_as_no_board_is_not_taken_for_no_interface(monkeypatch):
    """Test that an interface found on the usage page that does not answer as a board is said to be found."""
    attach(monkeypatch, Board(magic=b'NOPE'))
    with pytest.raises(hl.HostLightingError) as caught:
        hlp.open_device()
    message = str(caught.value)
    assert message.startswith('1 Host Lighting interface(s) found, but none opened and answered')
    assert 'add-on enabled' not in message


def test_the_note_on_another_major_names_v0_3_0_only_for_1_x():
    """Test that the note on another major points at hlp-spice2x v0.3.0 for 1.x boards only."""
    assert 'v0.3.0' in hlp.other_majors_note([hl.USAGE_PROTOCOL_1])
    assert 'v0.3.0' not in hlp.other_majors_note([0x0003])
    assert hlp.other_majors_note([]) == ''


def test_send_frame_reports_an_unacknowledged_frame_after_sending_it_twice(monkeypatch):
    """Test that a frame whose acknowledgement never comes is sent once more, then given up on.

    A late reply must not end the run, so this reports it unacknowledged and does not raise.
    """
    board = on_clock(monkeypatch, Board(drop={hl.CMD_STAGE}))
    staging = {('button', B1): (('entry',) + B1, False)}
    assert not bridge.send_frame(board.open(), {('button', B1): (255, 0, 0)}, staging).acknowledged
    stages = board.sent(hl.CMD_STAGE)
    assert len(stages) == 2 and stages[0][3:] == stages[1][3:]


def test_send_frame_propagates_a_disconnect():
    """Test that a vanished board is not mistaken for a late reply."""
    device = make_device(write_error=OSError('device disconnected'))
    staging = {('button', B1): (('entry',) + B1, False)}
    with pytest.raises(hl.HostLightingDisconnectedError):
        bridge.send_frame(device, {('button', B1): (255, 0, 0)}, staging)


def test_a_vanished_board_is_not_blamed_on_spice2x():
    """Test the message a user sees when the board is unplugged mid-run.

    The bridge catches socket errors to handle the game exiting. Board failures
    must not fall into that branch, or the user goes looking at spice2x for a
    USB problem.
    """
    class DeadConnection:
        def lights_read(self):
            return {'A': 1.0}

    device = make_device(write_error=OSError('device disconnected'))
    profile = {'A': (('button', B1), (255, 0, 0))}
    lines = []
    bridge.run(DeadConnection(), profile, device=device, fps=100, report=lines.append)
    text = ' '.join(lines)
    assert 'board disconnected' in text
    assert 'spice2x' not in text


M_ULTRA = {'board_id': '433030343237362E', 'label': 'Haute42 COSMOX M Ultra'}
B16 = {'board_id': '433031343539302E', 'label': 'Haute42 COSMOX'}


def test_several_boards_are_named_by_id_and_label(monkeypatch):
    """Test that the several-boards error says which board is which, and closes each."""
    boards = [Board(**M_ULTRA), Board(**B16)]
    attach(monkeypatch, *boards)
    with pytest.raises(hl.HostLightingError) as caught:
        hlp.open_device()
    message = str(caught.value)
    assert '433030343237362E (Haute42 COSMOX M Ultra)' in message
    assert '433031343539302E (Haute42 COSMOX)' in message
    assert '--board-id' in message
    assert all(board.closed for board in boards)


def test_an_id_prefix_picks_one_of_several_boards(monkeypatch):
    """Test that --board-id opens the matching board and closes the rest."""
    boards = [Board(**M_ULTRA), Board(**B16)]
    attach(monkeypatch, *boards)
    device = hlp.open_device('4330313')
    assert device.hello().board_id == B16['board_id']
    assert boards[0].closed and not boards[1].closed


def test_an_id_prefix_matching_nothing_says_so(monkeypatch):
    """Test that a prefix no board has is an error naming the prefix, with every board closed."""
    boards = [Board(**M_ULTRA)]
    attach(monkeypatch, *boards)
    with pytest.raises(hl.HostLightingError, match="no board matches ID prefix 'FFFF'"):
        hlp.open_device('FFFF')
    assert boards[0].closed


def test_boards_are_described_without_being_taken_over(monkeypatch):
    """Test that describe_boards sends HELLO and reads page 0 only, and closes every board."""
    boards = [Board(firmware='v2.0-build', **M_ULTRA),
              Board(version=(2, 1), firmware='v2.1-build', **B16)]
    attach(monkeypatch, *boards)
    assert hlp.describe_boards() == [
        {'board_id': M_ULTRA['board_id'], 'label': M_ULTRA['label'], 'firmware': 'v2.0-build',
         'version': (2, 0)},
        {'board_id': B16['board_id'], 'label': B16['label'], 'firmware': 'v2.1-build',
         'version': (2, 1)}]
    assert all(board.closed for board in boards)
    assert all({request[0] for request in board.requests} == {hl.CMD_HELLO, hl.CMD_GET_PAGE}
               for board in boards)
    assert all(request[3] == hl.PAGE_IDENTITY for board in boards
               for request in board.sent(hl.CMD_GET_PAGE))


@pytest.mark.parametrize('stranger', [{'magic': b'XXXX'}, {'version': (3, 0)}])
def test_an_interface_that_is_not_this_protocol_is_not_described(monkeypatch, stranger):
    """Test that an interface failing the handshake, or of another major, is left out and closed."""
    boards = [Board(**stranger), Board(**B16)]
    attach(monkeypatch, *boards)
    assert [board['board_id'] for board in hlp.describe_boards()] == [B16['board_id']]
    assert all(board.closed for board in boards)


@pytest.mark.parametrize('unreadable', [{'pages': HELLO_PAGES - {hl.PAGE_IDENTITY}},
                                        {'reject': {hl.CMD_GET_PAGE: hl.STATUS_UNSUPPORTED}}])
def test_a_board_whose_identity_cannot_be_read_is_still_listed(monkeypatch, unreadable):
    """Test that describe_boards lists a board it cannot name, as the bridge drives one."""
    attach(monkeypatch, Board(**unreadable, **M_ULTRA))
    assert hlp.describe_boards() == [{'board_id': M_ULTRA['board_id'], 'label': '?', 'firmware': '?',
                                      'version': (2, 0)}]
