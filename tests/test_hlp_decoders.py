"""Test the protocol's framing, decoders and staging encoders as pure functions.

The fixtures are the worked examples of the protocol reference
(docs/host-lighting.md, captured from a Haute42 COSMOX), written out by hand.
They are built by hand, since decoding a reply the fake board generated would
test the fake as much as the decoder.

SPDX-FileCopyrightText: (C) 2026 Jacob Simpson
SPDX-License-Identifier: GPL-3.0-or-later
"""
import pytest

from hlp_spice2x import hlp

BOARD_ID = '433031343539302E'
TOKEN = 'cd ab 34 12'


def report(*parts, token=None) -> bytes:
    """Build a 64-byte report from hex strings, zero-filled, with an optional state token."""
    data = bytearray(b''.join(bytes.fromhex(part) for part in parts).ljust(64, b'\x00'))
    if token:
        data[60:64] = bytes.fromhex(token)
    return bytes(data)


def name(text: str) -> str:
    """Hex of a 16-byte NUL-padded application name."""
    return text.encode().ljust(16, b'\x00').hex()


HELLO = report('81 41 00', '47 50 48 4c 02 00', BOARD_ID, '10 7f',
               '3e 00 0f 00 03 00 0f 00 00 00 00 00 00 00 03 00', '07 1f 00 00',
               'f3 00 07 00 01 00', '00 00')
SUMMARY = report('82 42 00', '08 00 00 01 00', '01 24 03 00', '10 00 64 00 28 00', '00 63 0a 0a',
                 '11 00 09 00', '01 00 15 00 10 00 05 00', '10 00 04 00', '1b 1f',
                 '00 00 00 00 00 00 00 00', token=TOKEN)
CLAIM_REPLY = report('83 45 00', '00 b2 00 00 00', 'a1 00 00 00', name('Alpha'), '01 02')
REFUSED = report('90 46 04', 'b2 00 00 00', name('Beta'), '00 03')
STATE_CHANGED = report('80 00 00', '01 00 00 01 00', '01 23 07 00', '00 00 01 02', '00 00 0a 01 0a',
                       'b2 00 00 00', name('Beta'), '00 03', '00 d0 07 01', token=TOKEN)
LIGHT_KINDS = report('82 47 00', '0b 00 00 10 00', '10 02 02 00',
                     '00 00 00 00 00 00 00 01 00 00 00 00 00 00 00 00',
                     '00 00 00 00 00 00 00 00 00 01 00 01 00 00 00 01', token=TOKEN)
STALE = report('82 48 03', '09 00 00', token=TOKEN)


def test_a_request_carries_its_flags_at_byte_two():
    """Test the 2.0 framing: command, sequence, flags, then the payload from [3]."""
    request = hlp.build_request(hlp.CMD_STAGE, 0x44, bytes.fromhex('0602'), flags=0x21)
    assert len(request) == 64
    assert request[:5] == bytes.fromhex('10 44 21 06 02')
    assert not any(request[5:])


def test_a_payload_that_does_not_fit_is_refused():
    """Test that a payload over 61 bytes is an error."""
    assert hlp.build_request(1, 1, bytes(61))
    with pytest.raises(ValueError):
        hlp.build_request(1, 1, bytes(62))


def test_a_reply_is_matched_by_command_and_sequence():
    """Test that a reply needs both the command echo and the sequence."""
    assert hlp.match_reply(HELLO, hlp.CMD_HELLO, 0x41)
    assert not hlp.match_reply(HELLO, hlp.CMD_HELLO, 0x42)
    assert not hlp.match_reply(HELLO, hlp.CMD_GET_PAGE, 0x41)
    assert not hlp.match_reply(HELLO[:2], hlp.CMD_HELLO, 0x41)


def test_an_event_is_told_from_a_reply():
    """Test that [0] = 0x80 marks an event, since there is no command 0x00."""
    assert hlp.is_event(STATE_CHANGED)
    assert not hlp.is_event(HELLO)


def test_hello_decodes_the_identity_and_every_bitmap():
    """Test HELLO against the reference's worked example."""
    hello = hlp.decode_hello(HELLO)
    assert (hello['major'], hello['minor']) == (2, 0)
    assert hello['board_id'] == BOARD_ID
    assert hello['queue_limit'] == 16
    assert hello['flags'] == {0, 1, 2, 3, 4, 5, 6}
    assert hello['commands'] == {0x01, 0x02, 0x03, 0x04, 0x05, 0x10, 0x11, 0x12, 0x13, 0x20, 0x21,
                                 0x30, 0x31, 0x32, 0x33, 0x70, 0x71}
    assert hello['pages'] == {0, 1, 2, 8, 9, 10, 11, 12}
    assert hello['addressing'] == {0, 1, 4, 5, 6, 7}
    assert hello['pixel_formats'] == {0, 1, 2}
    assert hello['events'] == {0}
    assert hello['subscribed'] == set()


def test_hello_without_the_magic_is_not_host_lighting():
    """Test that a reply lacking GPHL is refused."""
    with pytest.raises(hlp.HostLightingError, match='did not answer'):
        hlp.decode_hello(HELLO[:3] + b'XXXX' + HELLO[7:])


def test_a_truncated_hello_is_reported_not_indexed_past():
    """Test that a short reply raises the module's own error."""
    with pytest.raises(hlp.HostLightingError, match='HELLO reply is 20 bytes'):
        hlp.decode_hello(HELLO[:20])


def test_this_firmware_lacks_nothing_the_bridge_needs():
    """Test the capability check against the worked HELLO, for every addressing mode used."""
    assert hlp.lacking(hlp.decode_hello(HELLO), {0, 4, 6, 7}) == []


def test_each_missing_capability_is_named():
    """Test that what a board does not list is named by kind and number."""
    hello = hlp.decode_hello(HELLO)
    hello['commands'].discard(hlp.CMD_CLAIM)
    hello['flags'].discard(1)
    hello['pages'].discard(hlp.PAGE_LIGHTS)
    hello['addressing'].discard(hlp.ADDRESSING_ACTION)
    assert hlp.lacking(hello, {hlp.ADDRESSING_ACTION}) == [
        'command 0x03', 'request flag bit 1', 'page 11', 'addressing mode 6']


def test_the_summary_decodes_from_its_two_groups():
    """Test page 8 against the reference's worked example."""
    header = hlp.decode_page_header(SUMMARY)
    assert (header['page'], header['total'], header['count']) == (8, 1, 1)
    assert (header['stride'], header['groups']) == (36, 0x03)
    assert header['token'] == 0x1234ABCD
    summary = hlp.decode_records(SUMMARY)[0]
    assert (summary['led_extent'], summary['led_limit'], summary['render_hz']) == (16, 100, 40)
    assert summary['colour_format'] == 0
    assert (summary['controls'], summary['lit_controls'], summary['unlit_controls']) == (21, 16, 5)
    assert summary['lights'] == 16
    assert summary['page_changes'] == {page: 0 for page in range(8, 16)}


def test_the_change_counters_are_keyed_by_page():
    """Test that the Changes group's eight bytes land on pages 8 to 15 in order."""
    changed = bytearray(SUMMARY)
    changed[40:48] = bytes([1, 2, 3, 4, 5, 6, 7, 8])
    changes = hlp.decode_records(bytes(changed))[0]['page_changes']
    assert changes[hlp.PAGE_CONTROLS] == 3 and changes[hlp.PAGE_LIGHTS] == 4


def test_only_the_groups_a_reply_echoes_are_decoded():
    """Test a masked page 11 read: one group of two bytes, sixteen records."""
    records = hlp.decode_records(LIGHT_KINDS)
    assert [record['ordinal'] for record in records] == list(range(16))
    assert all(record['kind'] == 0 for record in records)
    assert [record['ordinal'] for record in records if record['duplicated']] == [3, 12, 13, 15]
    assert 'first_led' not in records[0] and 'owner_action' not in records[0]


def test_a_light_record_decodes_its_led_range_kind_and_owner():
    """Test page 11 groups 0 to 2: LEDs, kind and flags, owner pin and signed action."""
    reply = report('82 01 00', '0b 05 00 2e 00', '02 09 07 00',
                   '03 00 01 00', '00 01', '02 01 00',
                   '10 00 1e 00', '01 00', 'ff 00 80', token=TOKEN)
    button, case = hlp.decode_records(reply)
    assert button == {'ordinal': 5, 'first_led': 3, 'led_count': 1, 'kind': 0, 'duplicated': True,
                      'owner_pin': 2, 'owner_action': 1}
    assert case == {'ordinal': 6, 'first_led': 16, 'led_count': 30, 'kind': 1, 'duplicated': False,
                    'owner_pin': None, 'owner_action': None}


def test_a_control_record_decodes_its_key_action_and_flags():
    """Test page 10 group 0: key, signed action, and the duplicated and lit flags."""
    reply = report('82 01 00', '0a 00 00 15 00', '03 04 01 00',
                   '02 01 00 03', '10 0d 00 00', 'c3 ff ff 02', token=TOKEN)
    up, select, expander = hlp.decode_records(reply)
    assert up == {'ordinal': 0, 'key': 2, 'action': 1, 'duplicated': True, 'lit': True}
    assert select == {'ordinal': 1, 'key': 16, 'action': 13, 'duplicated': False, 'lit': False}
    assert (expander['key'], expander['action'], expander['lit']) == (0xC3, -1, True)


def test_records_are_stepped_by_the_stride_on_the_wire():
    """Test that a group a later board adds after the known ones does not misalign the read.

    Each record here carries group 0 (four bytes) and a three-byte group this
    client does not know, at bit 4.
    """
    reply = report('82 01 00', '0a 00 00 02 00', '02 07 11 00',
                   '02 01 00 02', 'aa bb cc', '03 02 00 02', 'dd ee ff', token=TOKEN)
    records = hlp.decode_records(reply)
    assert [(record['key'], record['action']) for record in records] == [(2, 1), (3, 2)]


def test_a_stride_too_narrow_for_its_groups_is_refused():
    """Test that a record narrower than the groups it claims is refused."""
    reply = report('82 01 00', '0a 00 00 02 00', '02 03 01 00', token=TOKEN)
    with pytest.raises(hlp.HostLightingError, match='stride is 3'):
        hlp.decode_records(reply)


def test_more_records_than_a_reply_holds_is_refused():
    """Test that a count running past the 48-byte record area is refused."""
    reply = report('82 01 00', '0a 00 00 30 00', '0d 04 01 00', token=TOKEN)
    with pytest.raises(hlp.HostLightingError, match='more than a reply holds'):
        hlp.decode_records(reply)


def test_a_stale_token_reply_has_no_records_and_the_current_token():
    """Test the status 3 reply: page and start echoed, nothing else but the token."""
    header = hlp.decode_page_header(STALE)
    assert header['status'] == hlp.STATUS_STALE
    assert (header['page'], header['start'], header['count'], header['groups']) == (9, 0, 0, 0)
    assert header['token'] == 0x1234ABCD


def test_state_changed_carries_page_one_and_the_token():
    """Test the event against the reference's worked example."""
    state, token = hlp.decode_state_changed(STATE_CHANGED)
    assert token == 0x1234ABCD
    assert state['takeover_live'] is False
    assert (state['profile'], state['player'], state['takeover_number']) == (1, 2, 1)
    assert (state['holder_token'], state['holder_name']) == (0xB2, 'Beta')
    assert (state['claim_exclusive'], state['claim_generation']) == (False, 3)
    assert (state['overlay'], state['keepalive_ms'], state['brightness_applied']) == (False, 2000, True)


def test_another_event_is_not_taken_for_state_changed():
    """Test that an event number this client does not know is refused, not decoded."""
    other = bytearray(STATE_CHANGED)
    other[1] = 1
    with pytest.raises(hlp.HostLightingError, match='not STATE_CHANGED'):
        hlp.decode_state_changed(bytes(other))


def test_the_claim_reply_shows_the_holder_and_generation():
    """Test the Controller group as the CLAIM reply carries it at [8..29]."""
    holder = hlp.decode_controller(CLAIM_REPLY[8:30])
    assert holder == {'holder_token': 0xA1, 'holder_name': 'Alpha', 'claim_exclusive': True,
                      'claim_generation': 2}


def test_a_status_4_reply_names_the_holder():
    """Test that a refusal carries the holder at [3..24] into the error."""
    error = hlp.HostLightingDevice.rejected(hlp.CMD_STAGE, REFUSED)
    assert error.status == hlp.STATUS_CLAIMED
    assert error.holder['holder_token'] == 0xB2 and error.holder['claim_generation'] == 3
    assert "CLAIMED by 'Beta'" in str(error)


def test_a_status_2_reply_names_the_invalid_byte():
    """Test that an invalid argument carries the request offset at [3] into the error."""
    error = hlp.HostLightingDevice.rejected(hlp.CMD_STAGE, report('90 01 02', '06'))
    assert (error.status, error.offset, error.holder) == (hlp.STATUS_INVALID, 6, None)
    assert 'INVALID_ARG at request byte 6' in str(error)


def test_identity_strings_follow_each_other_in_bit_order():
    """Test page 0: the label and the firmware version, the groups asked for and echoed."""
    strings = b'Haute42 COSMOX\x00v0.7.12-154-gc3499c2\x00'
    reply = report('82 01 00', '00 00 00 01 00', f"01 {len(strings):02x} 09 00", strings.hex(),
                   token=TOKEN)
    assert hlp.decode_identity(reply) == {0: 'Haute42 COSMOX', 3: 'v0.7.12-154-gc3499c2'}


def test_a_full_width_identity_string_has_no_terminator():
    """Test that a 48-byte string fills its group, and is decoded leniently if cut mid-character."""
    text = ('x' * 47).encode() + b'\xc3'
    reply = report('82 01 00', '00 00 00 01 00', '01 30 01 00', text.hex(), token=TOKEN)
    assert hlp.decode_identity(reply) == {0: 'x' * 47 + '\ufffd'}


@pytest.mark.parametrize('addressing, rgb, rgbw', [
    (hlp.ADDRESSING_LED_RUN, 19, 14),
    (hlp.ADDRESSING_LIGHT, 11, 9),
    (hlp.ADDRESSING_ACTION, 11, 9),
    (hlp.ADDRESSING_KIND, 14, 11),
])
def test_capacity_per_report_matches_the_reference(addressing, rgb, rgbw):
    """Test the per-report capacities against the protocol reference's table."""
    assert hlp.stage_capacity(addressing, hlp.PIXEL_RGB) == rgb
    assert hlp.stage_capacity(addressing, hlp.PIXEL_RGBW) == rgbw


def test_stage_by_action_matches_the_worked_example():
    """Test the bytes of B1 red and E1 blue, staged by action."""
    payloads = hlp.entry_payloads(hlp.ADDRESSING_ACTION, [(5, (255, 0, 0)), (43, (0, 0, 255))])
    assert payloads == [bytes.fromhex('06 02 05 00 ff 00 00 2b 00 00 00 ff')]


def test_an_led_run_matches_the_worked_example():
    """Test the bytes of LED 0 red as a run of one, as the refused frame sends it."""
    assert hlp.run_payloads(0, 1, (255, 0, 0)) == [bytes.fromhex('00 00 00 01 ff 00 00')]


def test_a_kind_is_one_byte_and_a_light_ordinal_two():
    """Test the address widths: kind 8-bit, light ordinal 16-bit, both with RGBW pixels."""
    assert hlp.entry_payloads(hlp.ADDRESSING_KIND, [(1, (1, 2, 3, 4))], hlp.PIXEL_RGBW) == [
        bytes.fromhex('17 01 01 01 02 03 04')]
    assert hlp.entry_payloads(hlp.ADDRESSING_LIGHT, [(300, (1, 2, 3, 4))], hlp.PIXEL_RGBW) == [
        bytes.fromhex('14 01 2c 01 01 02 03 04')]


def test_entries_past_one_report_are_split_at_the_capacity():
    """Test that twelve RGB entries by action go as eleven and one, in order."""
    entries = [(action, (action, 0, 0)) for action in range(1, 13)]
    first, second = hlp.entry_payloads(hlp.ADDRESSING_ACTION, entries)
    assert (first[1], second[1]) == (11, 1)
    assert second[2:] == bytes.fromhex('0c 00 0c 00 00')
    assert len(first) <= 61


def test_a_long_run_is_split_and_each_part_starts_where_the_last_ended():
    """Test that a 30-LED run goes as 19 and 11, and as 14, 14 and 2 with white."""
    rgb = hlp.run_payloads(16, 30, (1, 2, 3))
    assert [(payload[1] | payload[2] << 8, payload[3]) for payload in rgb] == [(16, 19), (35, 11)]
    rgbw = hlp.run_payloads(16, 30, (1, 2, 3, 4), hlp.PIXEL_RGBW)
    assert [(payload[1], payload[3]) for payload in rgbw] == [(16, 14), (30, 14), (44, 2)]
    assert rgbw[0][0] == 0x10 and rgbw[2][4:] == bytes([1, 2, 3, 4]) * 2


def test_an_led_index_above_255_travels_as_sixteen_bits():
    """Test that a run's first LED is little-endian 16-bit."""
    assert hlp.run_payloads(300, 1, (9, 9, 9))[0][:4] == bytes.fromhex('00 2c 01 01')


def test_a_pixel_of_the_wrong_width_is_refused_before_it_shifts_the_rest():
    """Test that a colour not matching its format raises."""
    with pytest.raises(ValueError, match='3-byte'):
        hlp.entry_payloads(hlp.ADDRESSING_ACTION, [(5, (1, 2, 3, 4))])
    with pytest.raises(ValueError, match='4-byte'):
        hlp.run_payloads(0, 2, (1, 2, 3), hlp.PIXEL_RGBW)


def test_every_profile_name_maps_to_its_action_or_kind():
    """Test the control names against GP2040-CE's GpioAction and LightType values."""
    targets = hlp.CONTROL_TARGETS
    assert [targets[name][1] for name in ('UP', 'DOWN', 'LEFT', 'RIGHT')] == [1, 2, 3, 4]
    assert [targets[name][1] for name in ('B1', 'B4', 'L1', 'R1', 'L2', 'R2')] == [5, 8, 9, 10, 11, 12]
    assert [targets[name][1] for name in ('S1', 'S2', 'A1', 'A2', 'L3', 'R3')] == [13, 14, 15, 16, 17, 18]
    assert [targets[name][1] for name in ('A3', 'A4', 'E1', 'E12')] == [41, 42, 43, 54]
    assert all(targets[name][0] == hlp.ADDRESSING_ACTION for name in ('UP', 'R3', 'A3', 'E12'))
    assert [targets[name] for name in ('CASE', 'TURBO', 'PLED1', 'PLED4')] == [
        (hlp.ADDRESSING_KIND, 1), (hlp.ADDRESSING_KIND, 2), (hlp.ADDRESSING_KIND, 3),
        (hlp.ADDRESSING_KIND, 6)]
    assert len(targets) == 38


def test_a_control_is_named_back_as_the_profile_wrote_it():
    """Test the reverse mapping, and a value with no name."""
    assert hlp.control_name(hlp.CONTROL_TARGETS['L3']) == 'L3'
    assert hlp.control_name(hlp.CONTROL_TARGETS['CASE']) == 'CASE'
    assert hlp.control_name((hlp.ADDRESSING_ACTION, 32)) == 'action 32'
    assert hlp.control_name((hlp.ADDRESSING_KIND, 9)) == 'kind 9'


@pytest.mark.parametrize('colour, wire', [
    ((255, 255, 255), (0, 0, 0, 255)),
    ((255, 128, 0), (255, 128, 0, 0)),
    ((200, 100, 100), (100, 0, 0, 100)),
    ((255, 0, 0, 128), (255, 0, 0, 128)),
    ((255, 255, 255, 255), (0, 0, 0, 255)),
    ((10, 10, 10, 20), (0, 0, 0, 30)),
])
def test_white_is_moved_onto_the_white_emitter(colour, wire):
    """Test the subtractive conversion, with an explicit white added and clamped."""
    assert hlp.subtractive_white(colour) == wire


@pytest.mark.parametrize('colour_format, white', [(0, False), (1, False), (2, True), (3, True)])
def test_only_the_w_formats_have_a_white_channel(colour_format, white):
    """Test that GRBW and RGBW are the chains with a white emitter."""
    assert hlp.has_white_channel(colour_format) is white
