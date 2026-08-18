"""Test the Spice API client against a stand-in server.

SPDX-FileCopyrightText: (C) 2026 Jacob Simpson
SPDX-License-Identifier: GPL-3.0-or-later
"""
import pytest

from hlp_spice2x.spiceapi import RC4, SpiceApiError, SpiceConnection

from .stub_server import StubServer


@pytest.fixture
def server():
    """Serve an unencrypted stub for one test."""
    stub = StubServer()
    yield stub
    stub.stop()


@pytest.fixture
def secure_server():
    """Serve a passworded stub for one test."""
    stub = StubServer(password='hunter2')
    yield stub
    stub.stop()


def test_rc4_is_symmetric():
    """Test that two identically keyed streams round-trip a message."""
    plain = b'{"id": 1, "module": "lights"}\x00'
    assert RC4(b'key').crypt(RC4(b'key').crypt(plain)) == plain


def test_rc4_keystream_advances():
    """Test that a single stream does not repeat itself, as a per-message key would."""
    cipher = RC4(b'key')
    first = cipher.crypt(b'AAAAAAAA')
    second = cipher.crypt(b'AAAAAAAA')
    assert first != second


def test_lights_read_returns_states(server):
    """Test that lights are returned as a name to state mapping."""
    connection = SpiceConnection('127.0.0.1', server.port)
    try:
        states = connection.lights_read()
    finally:
        connection.close()
    assert states['P1 Button 1'] == 1.0
    assert states['P1 Button 2'] == 0.0
    assert states['P1 Button 3'] == 0.5


def test_repeated_calls_stay_framed(server):
    """Test that many calls on one connection stay in sync."""
    connection = SpiceConnection('127.0.0.1', server.port)
    try:
        for _ in range(20):
            assert len(connection.lights_read()) == 5
    finally:
        connection.close()
    assert server.requests == 20


def test_encrypted_calls_stay_synchronised(secure_server):
    """Test the passworded path over several calls.

    One keystream is shared by both directions, so a client that re-keys per
    message decodes the first reply and then garbage. Several calls in a row
    are the discriminating case.
    """
    connection = SpiceConnection('127.0.0.1', secure_server.port, 'hunter2')
    try:
        for _ in range(5):
            assert connection.lights_read()['P1 Button 1'] == 1.0
    finally:
        connection.close()


def test_object_form_entries_are_accepted():
    """Test that object-shaped light entries are read as well as array-shaped ones."""
    stub = StubServer(lights=[{'name': 'Spot', 'state': 0.75}])
    try:
        connection = SpiceConnection('127.0.0.1', stub.port)
        try:
            assert connection.lights_read() == {'Spot': 0.75}
        finally:
            connection.close()
    finally:
        stub.stop()


def test_unreachable_server_raises_a_clear_error():
    """Test that a refused connection names spice2x and the port."""
    with pytest.raises(SpiceApiError, match='could not reach spice2x'):
        SpiceConnection('127.0.0.1', 9)


def test_api_errors_are_raised():
    """Test that an errors array in the reply becomes an exception."""
    stub = StubServer(errors=['no such module'])
    try:
        connection = SpiceConnection('127.0.0.1', stub.port)
        try:
            with pytest.raises(SpiceApiError, match='no such module'):
                connection.lights_read()
        finally:
            connection.close()
    finally:
        stub.stop()


def test_server_hanging_up_is_a_connection_error():
    """Test that the game vanishing mid-call surfaces as a ConnectionError.

    The bridge relies on this type to treat a closed game as a normal ending.
    """
    stub = StubServer(hang_up=True)
    try:
        connection = SpiceConnection('127.0.0.1', stub.port)
        try:
            with pytest.raises(ConnectionError):
                connection.lights_read()
        finally:
            connection.close()
    finally:
        stub.stop()
