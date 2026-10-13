"""Test the transport with replies, other applications' reports, and a board going away.

A board is a USB device, so it can vanish mid-run. A reply arriving late is
normal under load and the run carries on. The device failing ends the run.

A board's interface is also shared. Every application with it open receives
every reply and event, so a reply has to be told from another application's,
and an event must not be lost while a reply is waited for. Also finding the
right board when several are attached.

SPDX-FileCopyrightText: (C) 2026 Jacob Simpson
SPDX-License-Identifier: GPL-3.0-or-later
"""
import struct

import pytest

from hlp_spice2x import bridge, hlp

from .fake_board import OTHER_TOKEN, FakeBoard, attach

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
    return hlp.HostLightingDevice.from_hid(FakeHid(**kwargs))


def reply_to(request, status=0, payload=b'') -> bytes:
    """Build a reply carrying a request's command and sequence."""
    return (bytes([request[0] | hlp.REPLY_FLAG, request[1], status]) + payload).ljust(64, b'\x00')


def test_send_reports_a_disconnect_not_a_generic_error():
    """Test that a failed write is identified as the board going away."""
    device = make_device(write_error=OSError('device disconnected'))
    with pytest.raises(hlp.HostLightingDisconnected):
        device.send(hlp.CMD_CLEAR)


def test_request_reports_a_disconnect():
    """Test that a failed write during a request is a disconnect, not a timeout."""
    device = make_device(write_error=OSError('device disconnected'))
    with pytest.raises(hlp.HostLightingDisconnected):
        device.request(hlp.CMD_HELLO, timeout=0.05)


def test_a_failed_read_is_a_disconnect_too():
    """Test that the board vanishing between the write and the reply is not a timeout."""
    device = make_device(read_error=OSError('device disconnected'))
    with pytest.raises(hlp.HostLightingDisconnected):
        device.take_events()


def test_missing_reply_is_a_timeout_not_a_disconnect():
    """Test that silence from a present board is a timeout."""
    device = make_device()
    with pytest.raises(hlp.HostLightingTimeout):
        device.request(hlp.CMD_HELLO, timeout=0.05)


def test_both_are_host_lighting_errors():
    """Test that callers wanting either can still catch the base class."""
    assert issubclass(hlp.HostLightingTimeout, hlp.HostLightingError)
    assert issubclass(hlp.HostLightingDisconnected, hlp.HostLightingError)


def test_the_first_sequence_number_is_not_fixed():
    """Test that devices start their sequence numbers apart.

    Two applications sharing a board would otherwise send the same numbers from
    the start, and each would see the other's replies carrying its own.
    """
    starts = {make_device().sequence for _ in range(40)}
    assert len(starts) > 1


def test_the_sequence_wraps_without_ever_sending_zero():
    """Test that the sequence byte runs 1 to 255 and round again."""
    device = make_device()
    device.sequence = 254
    assert [device.send(hlp.CMD_CLEAR, flags=hlp.FLAG_NO_REPLY) for _ in range(3)] == [255, 1, 2]


def test_writes_lead_with_report_id_zero():
    """Test that a report goes to hidapi as the report ID then the 64 bytes."""
    device = make_device()
    device.send(hlp.CMD_CLEAR, flags=hlp.FLAG_NO_REPLY)
    assert len(device.device.writes[0]) == 65 and device.device.writes[0][0] == 0


def test_a_reply_asked_not_to_come_is_not_waited_for():
    """Test that request refuses NO_REPLY, which would otherwise always time out."""
    with pytest.raises(ValueError):
        make_device().request(hlp.CMD_CLEAR, flags=hlp.FLAG_NO_REPLY)


def test_every_request_carries_the_claim_generation_once_held():
    """Test that the generation rides in flag bits 4 to 6, and is 0 until a claim is held."""
    board = FakeBoard()
    device = board.open()
    device.hello()
    assert board.requests[-1][2] == 0
    holder = device.claim(hlp.CLAIM_TAKE, 0xA1, b'test')
    assert holder['holder_token'] == 0xA1 and device.generation == holder['claim_generation'] == 2
    device.hello()
    device.send(hlp.CMD_CLEAR, flags=hlp.FLAG_NO_REPLY)
    assert [request[2] for request in board.requests[-2:]] == [0x20, 0x22]


def test_the_generation_is_not_taken_from_another_holder():
    """Test that a CLAIM reply showing another holder leaves this application at generation 0."""
    board = FakeBoard()
    board.take_over()
    device = board.open()
    holder = device.claim(hlp.CLAIM_TAKE, 0xA1, b'test')
    assert holder['holder_token'] == OTHER_TOKEN and holder['claim_generation'] == 2
    assert device.generation == 0


def test_giving_the_claim_up_returns_to_generation_zero():
    """Test that an application that gave its claim up sends 0 again."""
    board = FakeBoard()
    device = board.open()
    device.claim(hlp.CLAIM_TAKE, 0xA1, b'test')
    assert device.claim(hlp.CLAIM_GIVE_UP, 0xA1)['holder_token'] == 0
    assert device.generation == 0


def test_the_claim_request_is_laid_out_as_the_protocol_says():
    """Test the CLAIM bytes: action, token, the name NUL-padded to 16, shared claim flags."""
    board = FakeBoard()
    board.open().claim(hlp.CLAIM_TAKE_OVER, 0x11223344, b'hlp-spice2x')
    request = board.sent(hlp.CMD_CLAIM)[0]
    assert request[3:8] == bytes.fromhex('01 44 33 22 11')
    assert request[8:24] == b'hlp-spice2x'.ljust(16, b'\x00')
    assert request[24] == 0 and not any(request[25:])


def test_configure_is_laid_out_as_the_protocol_says_and_returns_what_was_applied():
    """Test the CONFIGURE bytes, and that the board's clamped timeout is what comes back."""
    board = FakeBoard()
    applied = board.open().configure(overlay=True, keepalive_ms=50, brightness=False)
    assert board.sent(hlp.CMD_CONFIGURE)[0][3:7] == bytes.fromhex('01 32 00 00')
    assert applied == {'overlay': True, 'keepalive_ms': 100, 'brightness_applied': False}


def test_a_page_request_is_laid_out_as_the_protocol_says():
    """Test the GET_PAGE bytes: page, 16-bit start, expected token, field mask."""
    board = FakeBoard()
    device = board.open()
    device.get_page(hlp.PAGE_LIGHTS, 5, hlp.LIGHT_GROUPS, board.token)
    assert board.sent(hlp.CMD_GET_PAGE)[0][3:12] == struct.pack(
        '<BHIH', 11, 5, board.token, 0x07)


def test_an_event_arriving_before_a_reply_is_kept():
    """Test that an event read while waiting for a reply is handed over afterwards."""
    board = FakeBoard()
    device = board.open()
    device.subscribe(1)
    board.take_over()
    assert device.hello()['major'] == 2
    events = device.take_events()
    assert len(events) == 1
    assert hlp.decode_state_changed(events[0])[0]['holder_token'] == OTHER_TOKEN
    assert device.take_events() == []


def test_another_applications_earlier_reply_is_not_taken_for_this_ones():
    """Test that the reports already waiting are read before a request is sent.

    Replies wait in each application's buffer until it reads them. One left by
    another application's request, carrying the command and sequence this
    request is about to use, would otherwise be taken as its reply.
    """
    board = FakeBoard()
    device = board.open()
    stray = bytearray(64)
    stray[0], stray[1], stray[2] = hlp.CMD_HELLO | hlp.REPLY_FLAG, device.sequence % 255 + 1, 0
    stray[3:7] = b'XXXX'
    board.outbox.append(bytes(stray))
    assert device.hello()['major'] == 2


def test_a_page_reply_for_another_page_is_another_applications():
    """Test that a GET_PAGE reply is told apart by the page it echoes."""
    board = FakeBoard()
    device = board.open()

    def answer_for_page_10_first(fake, request):
        if request[0] == hlp.CMD_GET_PAGE:
            other = bytearray(64)
            other[0], other[1], other[3] = hlp.CMD_GET_PAGE | hlp.REPLY_FLAG, request[1], 10
            fake.outbox.append(bytes(other))

    board.hooks.append(answer_for_page_10_first)
    record, token = device.read_record(hlp.PAGE_SUMMARY, hlp.SUMMARY_GROUPS)
    assert record['lights'] == 16 and token == board.token


def test_a_claim_reply_for_another_token_is_another_applications():
    """Test that a CLAIM reply is told apart by the action and token it echoes."""
    board = FakeBoard()
    device = board.open()

    def answer_for_another_token_first(fake, request):
        if request[0] == hlp.CMD_CLAIM:
            fake.outbox.append(reply_to(request, payload=bytes([0]) + struct.pack('<I', 0x77) * 2))

    board.hooks.append(answer_for_another_token_first)
    assert device.claim(hlp.CLAIM_TAKE, 0xA1, b'test')['holder_token'] == 0xA1
    assert device.generation == 2


def test_a_reply_to_an_earlier_send_is_kept_for_its_own_wait():
    """Test that a reply read while another is waited for is not thrown away."""
    board = FakeBoard()
    device = board.open()
    earlier = device.send(hlp.CMD_HELLO)
    device.request_ok(hlp.CMD_SUBSCRIBE, bytes(2))
    assert hlp.decode_hello(device.wait_reply(hlp.CMD_HELLO, earlier, timeout=0.05))['major'] == 2


def test_a_table_walk_under_a_token_that_moved_is_abandoned():
    """Test that a walk the configuration changed under returns nothing."""
    board = FakeBoard(lights=[('B1', n) for n in range(12)])
    device = board.open()
    token = board.token

    def change_after_the_first_read(fake, request):
        if request[0] == hlp.CMD_GET_PAGE and struct.unpack_from('<H', request, 4)[0]:
            fake.hooks.clear()
            fake.reconfigure(lights=[('B2', n) for n in range(12)])

    board.hooks.append(change_after_the_first_read)
    assert device.read_table(hlp.PAGE_LIGHTS, hlp.LIGHT_GROUPS, token) is None
    assert len(device.read_table(hlp.PAGE_LIGHTS, hlp.LIGHT_GROUPS, board.token)) == 12


def test_a_table_that_stops_short_is_reported():
    """Test that a page promising more records than it returns does not loop forever."""
    board = FakeBoard()
    device = board.open()

    def empty_from_the_second_read(fake, request):
        if request[0] == hlp.CMD_GET_PAGE and struct.unpack_from('<H', request, 4)[0]:
            reply = bytearray(64)
            reply[0], reply[1], reply[3] = hlp.CMD_GET_PAGE | hlp.REPLY_FLAG, request[1], request[3]
            reply[4:8] = request[4:6] + struct.pack('<H', 16)
            reply[10:12] = request[10:12]
            fake.outbox.append(bytes(reply))
            fake.drop.add(hlp.CMD_GET_PAGE)

    board.hooks.append(empty_from_the_second_read)
    with pytest.raises(hlp.HostLightingError, match='stalled at 5 of 16'):
        device.read_table(hlp.PAGE_LIGHTS, hlp.LIGHT_GROUPS, board.token)


def test_an_identity_string_that_did_not_fit_is_asked_for_on_its_own():
    """Test that the firmware version is still read when the label fills the reply."""
    board = FakeBoard(label='L' * 48, firmware='v0.7.12-154-gc3499c2')
    device = board.open()
    assert device.read_identity() == ('L' * 48, 'v0.7.12-154-gc3499c2')
    assert [struct.unpack_from('<H', request, 10)[0] for request in board.sent(hlp.CMD_GET_PAGE)] == [
        0x09, 0x08]


def test_no_board_points_at_the_firmware_builds(monkeypatch):
    """Test that the not-found error says where to get firmware.

    Host Lighting is not in an official GP2040-CE release, and a user on stock
    firmware has no add-on setting to check.
    """
    attach(monkeypatch)
    with pytest.raises(hlp.HostLightingError) as caught:
        hlp.open_device()
    message = str(caught.value)
    assert 'add-on enabled' in message
    assert 'releases/tag/HLP_v2.0-rc7' in message
    assert 'another protocol major' not in message


def test_a_board_on_protocol_1_is_named_rather_than_missed(monkeypatch):
    """Test that a 1.x board, which this version cannot find by usage, is still explained."""
    attach(monkeypatch, other_majors=[hlp.USAGE_PROTOCOL_1])
    with pytest.raises(hlp.HostLightingError) as caught:
        hlp.open_device()
    message = str(caught.value)
    assert '1 Host Lighting interface(s) found speaking another protocol major' in message
    assert 'hlp-spice2x v0.3.0' in message


def test_discovery_matches_the_usage_of_this_major(monkeypatch):
    """Test that only usage page 0xFF47 with usage 0x0002 is a board to drive."""
    class Hid:
        @staticmethod
        def enumerate():
            return [{'usage_page': 0xFF47, 'usage': 0x0002, 'path': b'two'},
                    {'usage_page': 0xFF47, 'usage': 0x004C, 'path': b'one'},
                    {'usage_page': 0xFF47, 'usage': 0x0003, 'path': b'three'},
                    {'usage_page': 0x0001, 'usage': 0x0002, 'path': b'pad'}]

    monkeypatch.setattr(hlp, '_import_hid', lambda: Hid)
    assert [info['path'] for info in hlp.find_devices()] == [b'two']
    assert hlp.find_other_majors() == [0x004C, 0x0003]
    assert 'v0.3.0' not in hlp.other_majors_note([0x0003])
    assert hlp.other_majors_note([]) == ''


def test_send_frame_reports_an_unacknowledged_frame_after_sending_it_twice():
    """Test that a frame whose acknowledgement never comes is sent once more, then given up on.

    A late reply must not end the run, so this reports False and does not raise.
    """
    device = make_device()
    staging = {('button', B1): (('entry',) + B1, False)}
    assert not bridge.send_frame(device, {('button', B1): (255, 0, 0)}, staging)
    assert len(device.device.writes) == 2
    assert device.device.writes[0][4:] == device.device.writes[1][4:]


def test_send_frame_propagates_a_disconnect():
    """Test that a vanished board is not mistaken for a late reply."""
    device = make_device(write_error=OSError('device disconnected'))
    staging = {('button', B1): (('entry',) + B1, False)}
    with pytest.raises(hlp.HostLightingDisconnected):
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
    boards = [FakeBoard(**M_ULTRA), FakeBoard(**B16)]
    attach(monkeypatch, *boards)
    with pytest.raises(hlp.HostLightingError) as caught:
        hlp.open_device()
    message = str(caught.value)
    assert '433030343237362E (Haute42 COSMOX M Ultra)' in message
    assert '433031343539302E (Haute42 COSMOX)' in message
    assert '--board-id' in message
    assert all(board.closed for board in boards)


def test_an_id_prefix_picks_one_of_several_boards(monkeypatch):
    """Test that --board-id opens the matching board and closes the rest."""
    boards = [FakeBoard(**M_ULTRA), FakeBoard(**B16)]
    attach(monkeypatch, *boards)
    device = hlp.open_device('4330313')
    assert device.hello()['board_id'] == B16['board_id']
    assert boards[0].closed and not boards[1].closed


def test_an_id_prefix_matching_nothing_says_so(monkeypatch):
    """Test that a prefix no board has is an error naming the prefix, with every board closed."""
    boards = [FakeBoard(**M_ULTRA)]
    attach(monkeypatch, *boards)
    with pytest.raises(hlp.HostLightingError, match="no board matches ID prefix 'FFFF'"):
        hlp.open_device('FFFF')
    assert boards[0].closed


def test_boards_are_described_without_being_taken_over(monkeypatch):
    """Test that describe_boards sends HELLO and reads page 0 only, and closes every board."""
    boards = [FakeBoard(firmware='v2.0-build', **M_ULTRA),
              FakeBoard(version=(2, 1), firmware='v2.1-build', **B16)]
    attach(monkeypatch, *boards)
    assert hlp.describe_boards() == [
        {'board_id': M_ULTRA['board_id'], 'label': M_ULTRA['label'], 'firmware': 'v2.0-build',
         'version': (2, 0)},
        {'board_id': B16['board_id'], 'label': B16['label'], 'firmware': 'v2.1-build',
         'version': (2, 1)}]
    assert all(board.closed for board in boards)
    assert all({request[0] for request in board.requests} == {hlp.CMD_HELLO, hlp.CMD_GET_PAGE}
               for board in boards)
    assert all(request[3] == hlp.PAGE_IDENTITY for board in boards
               for request in board.sent(hlp.CMD_GET_PAGE))


@pytest.mark.parametrize('stranger', [{'magic': b'XXXX'}, {'version': (3, 0)}])
def test_an_interface_that_is_not_this_protocol_is_not_described(monkeypatch, stranger):
    """Test that an interface failing the handshake, or of another major, is left out and closed."""
    boards = [FakeBoard(**stranger), FakeBoard(**B16)]
    attach(monkeypatch, *boards)
    assert [board['board_id'] for board in hlp.describe_boards()] == [B16['board_id']]
    assert all(board.closed for board in boards)


def test_a_board_is_found_again_by_its_whole_id(monkeypatch):
    """Test that find_board opens the board with that ID and closes any other."""
    boards = [FakeBoard(**M_ULTRA), FakeBoard(**B16)]
    attach(monkeypatch, *boards)
    device = hlp.find_board(B16['board_id'])
    assert device.hello()['board_id'] == B16['board_id']
    assert boards[0].closed and not boards[1].closed


def test_a_board_off_the_bus_is_not_found(monkeypatch):
    """Test that find_board returns nothing for an absent board or one that cannot be opened."""
    board = FakeBoard(**M_ULTRA)
    attach(monkeypatch, board)
    assert hlp.find_board(B16['board_id']) is None and board.closed

    def cannot_open(path):
        raise OSError('open failed')

    monkeypatch.setattr(hlp, 'HostLightingDevice', cannot_open)
    assert hlp.find_board(M_ULTRA['board_id']) is None


def test_a_write_that_fails_without_raising_is_the_board_going_away():
    """Test that the negative count hidapi returns for a failed write is not taken as sent."""
    class Refusing(FakeHid):
        def write(self, data):
            """Fail as hidapi's write does."""
            return -1

    device = hlp.HostLightingDevice.from_hid(Refusing())
    with pytest.raises(hlp.HostLightingDisconnected, match='while sending 0x01'):
        device.send(hlp.CMD_HELLO)
    assert device.awaited == set()


@pytest.mark.parametrize('unreadable', [{'pages': frozenset({1, 8, 10, 11})}, {'reject': {hlp.CMD_GET_PAGE}}])
def test_a_board_whose_identity_cannot_be_read_is_still_listed(monkeypatch, unreadable):
    """Test that describe_boards lists a board it cannot name, as the bridge drives one."""
    attach(monkeypatch, FakeBoard(**unreadable, **M_ULTRA))
    assert hlp.describe_boards() == [{'board_id': M_ULTRA['board_id'], 'label': '?', 'firmware': '?',
                                      'version': (2, 0)}]


def test_a_device_is_closed_once():
    """Test that a second close does not reach the HID device."""
    class Counting(FakeHid):
        closes = 0

        def close(self):
            """Count the close."""
            self.closes += 1

    hid = Counting()
    device = hlp.HostLightingDevice.from_hid(hid)
    device.close()
    device.close()
    assert hid.closes == 1


def test_the_claim_generation_is_read_as_the_board_sends_it():
    """Test that the Controller group's last byte is the generation, unmasked."""
    assert hlp.decode_controller(bytes(21) + bytes([0x0A]))['claim_generation'] == 0x0A
