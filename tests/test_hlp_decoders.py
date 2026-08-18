"""Test the capability decoders and the per-light staging commands.

These are the parts of the protocol the bridge reads rather than writes, and
they are all pure functions over a 64-byte report, so they can be tested
against hand-built replies without a board or a transport.

The cases worth pinning are the ones where a field means something other than
what its value looks like: a zero that means "not reported", a flag that is an
assertion rather than a negation, and a stride that comes off the wire so an
older decoder cannot be misaligned by a newer board.

SPDX-FileCopyrightText: © 2026 Jacob Simpson
SPDX-License-Identifier: GPL-3.0-or-later
"""
import pytest

from hlp_spice2x import hlp


def caps_reply() -> bytearray:
    """Build an empty GET_CAPS reply with its command echo in place."""
    reply = bytearray(hlp.REPORT_SIZE)
    reply[0] = hlp.CMD_GET_CAPS | hlp.RESPONSE_FLAG
    return reply


def state_page(input_mode=0, profile=0, brightness=0, player=0, fingerprint=0,
               animation=0, features=0, framework=0, namespace=0, render_hz=0) -> bytes:
    """Build a GET_CAPS page 1 reply."""
    reply = caps_reply()
    reply[3], reply[4], reply[5], reply[6] = input_mode, profile, brightness, player
    reply[7:11] = fingerprint.to_bytes(4, 'little')
    reply[11] = animation
    reply[12:16] = features.to_bytes(4, 'little')
    reply[16], reply[17], reply[18] = framework, namespace, render_hz
    return bytes(reply)


def led_map_page(leds_per_button=1, colour_format=0, layout=0, led_extent=46,
                 brightness_maximum=255, buttons=None, players=None, turbo=hlp.UNMAPPED,
                 case=None, fingerprint=0) -> bytes:
    """Build a GET_CAPS page 2 reply, defaulting every slot to unmapped."""
    reply = caps_reply()
    reply[3], reply[4], reply[5] = leds_per_button, colour_format, layout
    reply[6], reply[7] = led_extent, brightness_maximum
    for index in range(len(hlp.BUTTON_NAMES)):
        first, count = (buttons or {}).get(index, (hlp.UNMAPPED, 0))
        reply[8 + index * 2], reply[9 + index * 2] = first, count
    for slot in range(4):
        reply[44 + slot] = (players or {}).get(slot + 1, hlp.UNMAPPED)
    reply[48] = turbo
    reply[49], reply[50] = case or (hlp.UNMAPPED, 0)
    reply[51:55] = fingerprint.to_bytes(4, 'little')
    return bytes(reply)


def light_record(first_led=0, led_count=1, kind=0, button_id=0, gpio_pin=hlp.UNMAPPED,
                 gpio_action=0, player_index=hlp.UNMAPPED, case_group=hlp.UNMAPPED,
                 x=0, y=0, flags=hlp.LIGHT_FLAG_PER_LIGHT, width=12) -> bytes:
    """Build one page 5 record, padded out to a wider stride on request."""
    record = (bytes([first_led, led_count, kind, button_id, gpio_pin])
              + gpio_action.to_bytes(2, 'little', signed=True)
              + bytes([player_index, case_group, x, y, flags]))
    return record + bytes(width - len(record))


def light_page(records, total=None, start=0, fingerprint=0, stride=12) -> bytes:
    """Build a GET_CAPS page 5 reply carrying the given records."""
    reply = caps_reply()
    reply[3] = len(records) if total is None else total
    reply[4], reply[5], reply[6] = start, len(records), stride
    for n, record in enumerate(records):
        reply[7 + n * stride:7 + n * stride + len(record)] = record
    reply[60:64] = fingerprint.to_bytes(4, 'little')
    return bytes(reply)


def stage_reply(command=hlp.CMD_SET_LIGHT, applied=0, skipped=0, mask=0) -> bytes:
    """Build a reply to a per-light staging command."""
    reply = bytearray(hlp.REPORT_SIZE)
    reply[0] = command | hlp.RESPONSE_FLAG
    reply[3], reply[4] = applied, skipped
    reply[5], reply[6] = mask & 0xFF, (mask >> 8) & 0xFF
    return bytes(reply)


class ScriptedDevice(hlp.HostLightingDevice):
    """A device that answers from a scripted list instead of from a board.

    Deliberately does not run the real __init__, which would open a HID handle.
    Replies are taken in order, so a test's script is also its expectation of
    how many round trips the code under test makes.
    """

    def __init__(self, replies):
        """Queue the replies this device will hand back, in order."""
        self.replies = list(replies)
        self.requests = []

    def request_ok(self, command, payload=b'', timeout=0.5):
        """Record the request and answer it from the script."""
        self.requests.append((command, bytes(payload)))
        if not self.replies:
            raise AssertionError(f"unscripted request: command 0x{command:02X}")
        return self.replies.pop(0)


def test_page_1_decodes_the_fields_protocol_v1_1_appended():
    """Test that the v1.1 tail of page 1 is decoded, not just the fingerprint."""
    state = hlp.decode_state(state_page(fingerprint=0xDEADBEEF, features=hlp.FEATURE_LIGHT_TABLE,
                                        framework=2, namespace=1, render_hz=40))
    assert state['fingerprint'] == 0xDEADBEEF
    assert state['features'] & hlp.FEATURE_LIGHT_TABLE
    assert state['framework'] == 2
    assert state['render_hz'] == 40


def test_a_board_that_states_no_render_rate_reads_as_unstated():
    """Test that a zero render rate decodes as None rather than as 0 Hz.

    Zero is what pre-v1.1 firmware leaves in the zero-filled reply, and what a
    v1.1 board reports when it does not know its own rate. Neither is a rate.
    """
    assert hlp.decode_state(state_page(render_hz=0))['render_hz'] is None


def test_page_1_from_older_firmware_reads_as_nothing_reported():
    """Test that a board predating the v1.1 fields reports no capabilities."""
    state = hlp.decode_state(state_page(fingerprint=7))
    assert state['fingerprint'] == 7
    assert state['features'] == 0
    assert state['render_hz'] is None


def test_no_animation_selected_is_not_animation_255():
    """Test that the 0xFF animation sentinel decodes as nothing selected."""
    assert hlp.decode_state(state_page(animation=hlp.UNMAPPED))['animation_index'] is None


def test_a_truncated_page_is_reported_not_indexed_past():
    """Test that a short reply raises rather than dying on an index.

    A truncated read would otherwise surface as an IndexError from inside a
    decoder, which no caller in the bridge is positioned to catch.
    """
    for decoder, page in ((hlp.decode_state, 'page 1'), (hlp.decode_led_map, 'page 2'),
                          (hlp.decode_lights, 'page 5')):
        with pytest.raises(hlp.HostLightingError, match=page):
            decoder(bytes(8))


def test_page_2_omits_controls_the_board_has_no_led_for():
    """Test that unmapped controls drop out of the button map."""
    decoded = hlp.decode_led_map(led_map_page(buttons={0: (3, 1), 4: (7, 2)}))
    assert decoded['buttons'] == {0: (3, 1), 4: (7, 2)}


def test_page_2_keeps_player_leds_keyed_by_player_not_by_position():
    """Test that an absent player 1 does not slide player 2 into its place."""
    decoded = hlp.decode_led_map(led_map_page(players={2: 40, 4: 42}))
    assert decoded['players'] == {2: 40, 4: 42}


def test_a_case_range_of_no_lights_is_no_case_range():
    """Test that a case entry with a zero count decodes as absent."""
    assert hlp.decode_led_map(led_map_page(case=(30, 0)))['case'] is None
    assert hlp.decode_led_map(led_map_page(case=(30, 16)))['case'] == (30, 16)


def test_the_board_reports_its_own_led_extent():
    """Test that page 2 carries the extent that bounds a raw range."""
    assert hlp.decode_led_map(led_map_page(led_extent=128))['led_extent'] == 128


def test_only_the_w_formats_have_a_white_channel():
    """Test that a white component only has somewhere to go on GRBW and RGBW."""
    assert not hlp.has_white_channel(0)  # GRB
    assert not hlp.has_white_channel(1)  # RGB
    assert hlp.has_white_channel(2)      # GRBW
    assert hlp.has_white_channel(3)      # RGBW


def test_page_5_numbers_its_records_from_the_ordinal_the_page_starts_at():
    """Test that ordinals continue across a paged walk rather than restarting."""
    page = hlp.decode_lights(light_page([light_record(), light_record()], total=6, start=4))
    assert [record['ordinal'] for record in page['records']] == [4, 5]


def test_a_record_without_the_per_light_flag_is_synthesised():
    """Test that the per-light flag is read as an assertion, not a negation.

    A synthesised table is rebuilt from per-control configuration, so it can
    never show a control owning more than one light. That is the caveat a
    caller needs, so it is the absence of the flag that gets reported.
    """
    page = hlp.decode_lights(light_page([light_record(flags=hlp.LIGHT_FLAG_PER_LIGHT),
                                         light_record(flags=0)]))
    assert [record['synthesised'] for record in page['records']] == [False, True]


def test_a_position_is_only_read_when_the_board_vouches_for_it():
    """Test that grid coordinates without their flag are not reported as real."""
    page = hlp.decode_lights(light_page([light_record(x=3, y=4, flags=hlp.LIGHT_FLAG_PER_LIGHT),
                                         light_record(x=3, y=4, flags=hlp.LIGHT_FLAG_POSITION)]))
    assert [record['position'] for record in page['records']] == [None, (3, 4)]


def test_page_5_takes_its_stride_from_the_wire():
    """Test that a wider record than this decoder knows does not misalign it.

    The stride is on the wire precisely so a board that grows the record can
    still be read by an older host, which is the whole compatibility contract.
    """
    records = [light_record(first_led=9, button_id=4, width=14),
               light_record(first_led=11, button_id=5, width=14)]
    page = hlp.decode_lights(light_page(records, stride=14))
    assert page['stride'] == 14
    assert [record['first_led'] for record in page['records']] == [9, 11]
    assert [record['button_id'] for record in page['records']] == [4, 5]


def test_a_stride_narrower_than_the_record_is_refused():
    """Test that a too-narrow stride is an error rather than a short read."""
    with pytest.raises(hlp.HostLightingError, match='stride'):
        hlp.decode_lights(light_page([light_record()], stride=8))


def test_read_lights_walks_every_page():
    """Test that a table spanning several reads comes back whole and in order."""
    device = ScriptedDevice([
        light_page([light_record(button_id=n) for n in range(4)], total=6, start=0, fingerprint=5),
        light_page([light_record(button_id=n) for n in (4, 5)], total=6, start=4, fingerprint=5),
    ])
    records, fingerprint = device.read_lights()
    assert [record['button_id'] for record in records] == [0, 1, 2, 3, 4, 5]
    assert fingerprint == 5
    assert [payload[1] for _, payload in device.requests] == [0, 4]


def test_read_lights_refuses_a_walk_that_stops_short():
    """Test that a board that stops answering mid-table is an error, not a truncated table."""
    device = ScriptedDevice([
        light_page([light_record()], total=6, start=0),
        light_page([], total=6, start=1),
    ])
    with pytest.raises(hlp.HostLightingError, match='stalled'):
        device.read_lights()


def test_read_lights_discards_a_walk_the_table_moved_under():
    """Test that a table changing mid-walk restarts the walk.

    Splicing records from either side of the change would produce a table that
    looks self-consistent and addresses the wrong lights, which no later error
    can catch: a stale ordinal is still a perfectly valid ordinal.
    """
    device = ScriptedDevice([
        light_page([light_record(button_id=0)], total=2, start=0, fingerprint=1),
        light_page([light_record(button_id=9)], total=2, start=1, fingerprint=2),
        light_page([light_record(button_id=7)], total=2, start=0, fingerprint=3),
        light_page([light_record(button_id=8)], total=2, start=1, fingerprint=3),
    ])
    records, fingerprint = device.read_lights()
    assert [record['button_id'] for record in records] == [7, 8]
    assert fingerprint == 3


def test_read_lights_gives_up_if_the_table_never_settles():
    """Test that an endlessly changing table is reported rather than retried forever."""
    device = ScriptedDevice([
        light_page([light_record()], total=2, start=0, fingerprint=1),
        light_page([light_record()], total=2, start=1, fingerprint=2),
    ])
    with pytest.raises(hlp.HostLightingError, match='kept changing'):
        device.read_lights(retries=0)


def test_set_lights_chunks_at_the_report_capacity():
    """Test that more entries than one report holds are sent as further reports."""
    device = ScriptedDevice([stage_reply(applied=15), stage_reply(applied=3)])
    entries = [(ordinal, 255, 0, 0) for ordinal in range(18)]
    applied, skipped, stale = device.set_lights(entries)
    assert applied == 18
    assert skipped == 0
    assert [payload[0] for _, payload in device.requests] == [hlp.SET_LIGHT_MAX, 3]


def test_the_outcome_mask_is_left_alone_unless_the_caller_asks():
    """Test that the v1.3 mask is not read by default.

    Boards before v1.3 zero-fill those bytes, so reading them ungated reports
    every entry as skipped - including the ones that visibly lit.
    """
    device = ScriptedDevice([stage_reply(applied=2, mask=0)])
    _, _, stale = device.set_lights([(0, 1, 2, 3), (1, 4, 5, 6)])
    assert stale is None


def test_the_outcome_mask_names_the_ordinals_the_board_skipped():
    """Test that a cleared mask bit is reported as its ordinal, not its bit index."""
    device = ScriptedDevice([stage_reply(applied=2, skipped=1, mask=0b101)])
    _, skipped, stale = device.set_lights([(7, 0, 0, 0), (8, 0, 0, 0), (9, 0, 0, 0)],
                                          outcomes=True)
    assert skipped == 1
    assert stale == [8]


def test_skipped_ordinals_are_named_across_report_boundaries():
    """Test that the second report's mask maps onto the second report's entries."""
    device = ScriptedDevice([stage_reply(applied=15, mask=0x7FFF),
                             stage_reply(applied=2, skipped=1, mask=0b110)])
    entries = [(ordinal, 0, 0, 0) for ordinal in range(100, 118)]
    _, _, stale = device.set_lights(entries, outcomes=True)
    assert stale == [115]


def test_set_lights_rgbw_sends_five_byte_entries():
    """Test that the white component reaches the wire on the RGBW variant."""
    device = ScriptedDevice([stage_reply(command=hlp.CMD_SET_LIGHT_RGBW, applied=1)])
    device.set_lights_rgbw([(3, 10, 20, 30, 40)])
    command, payload = device.requests[0]
    assert command == hlp.CMD_SET_LIGHT_RGBW
    assert payload[:6] == bytes([1, 3, 10, 20, 30, 40])


def test_an_entry_of_the_wrong_width_is_refused_before_anything_is_sent():
    """Test that a mis-shaped entry raises instead of staging garbage.

    The count byte tells the firmware how many fixed-width entries follow, so a
    short entry shifts every entry after it. Staging is not acknowledged
    per-entry, so nothing on the wire would report it.
    """
    device = ScriptedDevice([])
    with pytest.raises(ValueError, match='entries'):
        device.set_lights([(3, 10, 20, 30, 40)])
    with pytest.raises(ValueError, match='entries'):
        device.set_lights_rgbw([(3, 10, 20, 30)])
    assert device.requests == []


def test_only_the_paged_pages_carry_a_start_entry():
    """Test that the paged request form is used for pages 4 and 5 alone.

    Sending the wider payload to an unpaged page risks an INVALID_ARG from
    firmware that reads the extra byte, for no gain.
    """
    device = ScriptedDevice([caps_reply(), caps_reply()])
    device.get_caps_page(hlp.CAPS_PAGE_STATE)
    device.get_caps_page(hlp.CAPS_PAGE_LIGHTS, 8)
    assert device.requests == [(hlp.CMD_GET_CAPS, bytes([hlp.CAPS_PAGE_STATE])),
                               (hlp.CMD_GET_CAPS, bytes([hlp.CAPS_PAGE_LIGHTS, 8]))]


@pytest.mark.parametrize('colour, expected', [
    ((255, 255, 255), (0, 0, 0, 255)),        # white rides the emitter, not the three
    ((255, 255, 255, 0), (0, 0, 0, 255)),     # an explicit zero changes nothing
    ((255, 255, 255, 255), (0, 0, 0, 255)),   # and an explicit full white clamps
    ((255, 0, 0), (255, 0, 0, 0)),            # nothing achromatic to move
    ((255, 0, 0, 255), (255, 0, 0, 255)),     # red plus white, both honoured
    ((255, 0, 0, 128), (255, 0, 0, 128)),     # and at half
    ((120, 200, 90), (30, 110, 0, 90)),       # the general case
])
def test_white_takes_the_achromatic_part_of_a_colour(colour, expected):
    """Test the subtractive conversion a board expects a host to have applied.

    A white emitter exists to make white better than three colour emitters can,
    so the achromatic part of a colour belongs on it. Anything the profile asked
    for on top is added and clamped.
    """
    assert hlp.subtractive_white(colour) == expected
