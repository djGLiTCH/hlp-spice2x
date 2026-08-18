"""Test how the bridge reacts to the board misbehaving or going away.

A board is a USB device, so it can vanish mid-run. The distinction that
matters is between a reply arriving late, which is normal under load and worth
carrying on through, and the device failing, which is not.

SPDX-FileCopyrightText: © 2026 Jacob Simpson
SPDX-License-Identifier: GPL-3.0-or-later
"""
import pytest

from hlp_spice2x import bridge, hlp


class FakeHid:
    """Stands in for the hidapi device object inside HostLightingDevice."""

    def __init__(self, write_error=None, reply=None):
        """Script the device's behaviour: an error to raise, or a reply to return."""
        self.write_error = write_error
        self.reply = reply
        self.writes = 0

    def write(self, data):
        """Record the write, or fail the way an unplugged device does."""
        if self.write_error is not None:
            raise self.write_error
        self.writes += 1
        return len(data)

    def read(self, size):
        """Return the scripted reply, or nothing at all."""
        return list(self.reply) if self.reply else []

    def close(self):
        """Match the hidapi interface."""


def make_device(**kwargs):
    """Build a HostLightingDevice around a FakeHid without opening real hardware."""
    device = hlp.HostLightingDevice.__new__(hlp.HostLightingDevice)
    device.device = FakeHid(**kwargs)
    device.sequence = 0
    return device


def test_send_reports_a_disconnect_not_a_generic_error():
    """Test that a failed write is identified as the board going away."""
    device = make_device(write_error=OSError('device disconnected'))
    with pytest.raises(hlp.HostLightingDisconnected):
        device.send(hlp.CMD_CLEAR)


def test_request_reports_a_disconnect():
    """Test that a failed write during a request is a disconnect, not a timeout."""
    device = make_device(write_error=OSError('device disconnected'))
    with pytest.raises(hlp.HostLightingDisconnected):
        device.request(hlp.CMD_PING, timeout=0.05)


def test_missing_reply_is_a_timeout_not_a_disconnect():
    """Test that silence from a present board is a timeout."""
    device = make_device()
    with pytest.raises(hlp.HostLightingTimeout):
        device.request(hlp.CMD_PING, timeout=0.05)


def test_both_are_host_lighting_errors():
    """Test that callers wanting either can still catch the base class."""
    assert issubclass(hlp.HostLightingTimeout, hlp.HostLightingError)
    assert issubclass(hlp.HostLightingDisconnected, hlp.HostLightingError)


def test_no_board_points_at_the_firmware_builds(monkeypatch):
    """Test that the not-found error says where to get firmware.

    Host Lighting is not in an official GP2040-CE release, so asking whether
    the add-on is enabled is not enough on its own: a user on stock firmware
    has no such setting to check. The link is what gets them unstuck.
    """
    monkeypatch.setattr(hlp, 'find_devices', lambda: [])
    with pytest.raises(hlp.HostLightingError) as caught:
        hlp.open_device()
    message = str(caught.value)
    assert 'add-on enabled' in message
    assert 'releases/tag/HLP_v1.3' in message


def test_send_frame_tolerates_a_late_commit():
    """Test that an unacknowledged COMMIT reports False rather than raising.

    Replies are best-effort while streaming, so a late one must not end the run.
    """
    device = make_device()
    assert bridge.send_frame(device, {('button', 4): (255, 0, 0)}) is False
    assert device.device.writes >= 2  # the staging report, then the commit


def test_send_frame_propagates_a_disconnect():
    """Test that a vanished board is not mistaken for a late reply."""
    device = make_device(write_error=OSError('device disconnected'))
    with pytest.raises(hlp.HostLightingDisconnected):
        bridge.send_frame(device, {('button', 4): (255, 0, 0)})


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
    profile = {'A': (('button', 4), (255, 0, 0))}
    lines = []
    bridge.run(DeadConnection(), profile, device=device, fps=100, report=lines.append)
    text = ' '.join(lines)
    assert 'board disconnected' in text
    assert 'spice2x' not in text
