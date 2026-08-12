"""Test the command line surface.

These exercise the wiring between arguments and behaviour, which is otherwise
only covered by running the tool by hand.

SPDX-FileCopyrightText: © 2026 Jacob Simpson
SPDX-License-Identifier: GPL-3.0-or-later
"""
import json

import pytest

from hlp_spice2x.__main__ import main

from .stub_server import StubServer


@pytest.fixture
def server():
    """Serve a stub for one test."""
    stub = StubServer()
    yield stub
    stub.stop()


def profile_file(tmp_path, lights=None):
    """Write a usable profile and return its path."""
    path = tmp_path / 'p.json'
    path.write_text(json.dumps({'lights': lights or {
        'P1 Button 1': {'button': 'B1', 'colour': 'FF0000'}}}), encoding='utf-8')
    return str(path)


def test_zero_fps_is_rejected(tmp_path):
    """Test that --fps 0 is refused rather than dividing by zero."""
    with pytest.raises(SystemExit) as caught:
        main(['--profile', profile_file(tmp_path), '--fps', '0'])
    assert caught.value.code == 2


@pytest.mark.parametrize('timeout', ['50', '70000'])
def test_out_of_range_timeout_is_rejected(tmp_path, timeout):
    """Test that a timeout outside the 16-bit field's usable range is refused."""
    with pytest.raises(SystemExit) as caught:
        main(['--profile', profile_file(tmp_path), '--timeout', timeout])
    assert caught.value.code == 2


def test_profile_is_required():
    """Test that running the bridge without a profile is refused."""
    with pytest.raises(SystemExit) as caught:
        main([])
    assert caught.value.code == 2


def test_list_lights(server, capsys):
    """Test that --list-lights prints each light and its state."""
    assert main(['--port', str(server.port), '--list-lights']) == 0
    out = capsys.readouterr().out
    assert '5 lights:' in out
    assert 'P1 Button 1' in out


def test_write_profile_creates_a_skeleton(server, tmp_path, capsys):
    """Test that --write-profile lists every light with an empty control."""
    path = tmp_path / 'new.json'
    assert main(['--port', str(server.port), '--write-profile', str(path)]) == 0
    written = json.loads(path.read_text(encoding='utf-8'))
    assert set(written['lights']) == {'P1 Button 1', 'P1 Button 2', 'P1 Button 3',
                                      'Neon Left', 'Unmapped Spot'}
    assert all(entry['button'] == '' for entry in written['lights'].values())
    assert 'wrote' in capsys.readouterr().out


def test_write_profile_refuses_to_overwrite(server, tmp_path, capsys):
    """Test that an existing file is not clobbered."""
    path = tmp_path / 'new.json'
    path.write_text('{}', encoding='utf-8')
    assert main(['--port', str(server.port), '--write-profile', str(path)]) == 1
    assert 'already exists' in capsys.readouterr().err


def test_unreachable_spice2x_names_spice2x(tmp_path, capsys):
    """Test that failing to connect blames spice2x, with the port in the message."""
    assert main(['--port', '9', '--profile', profile_file(tmp_path)]) == 1
    err = capsys.readouterr().err
    assert 'could not reach spice2x' in err and ':9' in err


def test_a_bad_profile_is_reported_before_connecting(tmp_path, capsys):
    """Test that a profile error wins over an unreachable server.

    The profile is the thing the user can fix, so it should be what they are
    told about even when the port is also wrong.
    """
    path = profile_file(tmp_path, {'X': {'button': 'NOPE'}})
    assert main(['--port', '9', '--profile', path]) == 1
    err = capsys.readouterr().err
    assert "unknown control 'NOPE'" in err
    assert 'could not reach' not in err


def test_dry_run_prints_frames_and_needs_no_board(server, tmp_path, capsys):
    """Test that --dry-run resolves frames without opening a HID device."""
    path = profile_file(tmp_path)
    stub = server

    # let the loop run briefly, then drop the connection to end it
    import threading
    import time
    threading.Thread(target=lambda: (time.sleep(0.4), stub.stop()), daemon=True).start()
    assert main(['--port', str(stub.port), '--profile', path, '--dry-run', '--fps', '20']) == 0
    out = capsys.readouterr().out
    assert 'frame:' in out and 'B1=#FF0000' in out
