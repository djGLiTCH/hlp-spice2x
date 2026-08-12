"""A stand-in Spice API server, so the bridge can be exercised without a game.

Speaks the same wire format as spice2x: NUL-terminated JSON over TCP, with one
RC4 keystream per connection when a password is set.

SPDX-FileCopyrightText: © 2026 Jacob Simpson
SPDX-License-Identifier: GPL-3.0-or-later
"""
import json
import socket
import threading
import time

from hlp_spice2x.spiceapi import RC4

DEFAULT_LIGHTS = [
    ["P1 Button 1", 1.0, True],
    ["P1 Button 2", 0.0, True],
    ["P1 Button 3", 0.5, True],
    ["Neon Left", 0.25, True],
    ["Unmapped Spot", 1.0, True],
]


def _copy(entry):
    """Copy one light entry, preserving whether it is array-shaped or object-shaped."""
    return dict(entry) if isinstance(entry, dict) else list(entry)


class StubServer:
    """Serves one client on a loopback port until stopped."""

    def __init__(self, lights=None, password='', mutate=None, errors=None, hang_up=False):
        """Start listening on an ephemeral port.

        :param lights: list of [name, state, active] entries to report
        :param password: API password, enabling RC4 if non-empty
        :param mutate: optional callable(lights, elapsed) returning the list to send
        :param errors: if set, reply with this errors array instead of data
        :param hang_up: close the connection without replying, as a crashed game would
        """
        self.lights = [_copy(entry) for entry in (lights or DEFAULT_LIGHTS)]
        self.password = password.encode('utf-8') if password else b''
        self.mutate = mutate
        self.errors = errors
        self.hang_up = hang_up
        self.requests = 0
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(('127.0.0.1', 0))
        self._sock.listen(1)
        self.port = self._sock.getsockname()[1]
        self._started = time.monotonic()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        """Accept clients one after another until stopped."""
        self._sock.settimeout(0.5)
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self._handle(conn)

    def _handle(self, conn):
        """Serve one client. Each connection gets its own keystream, as spice2x does."""
        cipher = RC4(self.password) if self.password else None
        buffer = bytearray()
        started = self._started
        conn.settimeout(0.5)
        try:
            while not self._stop.is_set():
                try:
                    chunk = conn.recv(65536)
                except socket.timeout:
                    continue
                if not chunk:
                    return
                buffer += cipher.crypt(chunk) if cipher else chunk
                while b'\x00' in buffer:
                    raw, _, rest = bytes(buffer).partition(b'\x00')
                    buffer = bytearray(rest)
                    request = json.loads(raw.decode())
                    self.requests += 1
                    if self.hang_up:
                        return
                    lights = [_copy(entry) for entry in self.lights]
                    if self.mutate:
                        lights = self.mutate(lights, time.monotonic() - started)
                    body = json.dumps({'id': request['id'], 'errors': self.errors or [],
                                       'data': lights}).encode() + b'\x00'
                    conn.sendall(cipher.crypt(body) if cipher else body)
        except (ConnectionError, OSError):
            pass
        finally:
            conn.close()

    def stop(self):
        """Stop serving and close the listening socket."""
        self._stop.set()
        self._sock.close()
        self._thread.join(timeout=2)
