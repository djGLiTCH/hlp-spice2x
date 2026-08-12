"""Minimal Spice API client.

spice2x exposes the running game's state over a TCP service speaking
NUL-terminated JSON, enabled with `spice.exe -api PORT`. With `-apipass PASS`
the traffic is RC4-encrypted.

Two details of the wire format are easy to get wrong, and both are taken from
spice2x's reference C++ client:

* one RC4 keystream is allocated per connection and shared by both
  directions, so it advances in wire order - a request's bytes, then the bytes
  of its reply. Re-keying per message desynchronises after the first exchange.
* the NUL terminator is part of the plaintext and is encrypted with it, so
  framing has to be found after decryption. Ciphertext contains NULs.

SPDX-FileCopyrightText: © 2026 Jacob Simpson
SPDX-License-Identifier: GPL-3.0-or-later
"""
import json
import socket


class RC4:
    """An RC4 keystream, held for the life of a connection."""

    def __init__(self, key: bytes):
        """Key the cipher from the API password."""
        state = list(range(256))
        j = 0
        for i in range(256):
            j = (j + state[i] + key[i % len(key)]) & 0xFF
            state[i], state[j] = state[j], state[i]
        self.state, self.i, self.j = state, 0, 0

    def crypt(self, data: bytes) -> bytes:
        """Encrypt or decrypt, advancing the shared keystream."""
        state, i, j = self.state, self.i, self.j
        out = bytearray()
        for byte in data:
            i = (i + 1) & 0xFF
            j = (j + state[i]) & 0xFF
            state[i], state[j] = state[j], state[i]
            out.append(byte ^ state[(state[i] + state[j]) & 0xFF])
        self.i, self.j = i, j
        return bytes(out)


class SpiceApiError(RuntimeError):
    """The Spice API could not be reached, or answered with an error."""


class SpiceConnection:
    """A Spice API client: NUL-terminated JSON over TCP, RC4 when passworded."""

    def __init__(self, host: str, port: int, password: str = ''):  # nosec B107
        """Connect to the spice2x API, keying the cipher if a password is set.

        The empty default means "no password", which is spice2x's own default
        when -apipass is not given; it is not a credential.
        """
        self.cipher = RC4(password.encode('utf-8')) if password else None
        self.buffer = bytearray()  # decrypted plaintext awaiting a terminator
        self.next_id = 1
        try:
            self.sock = socket.create_connection((host, port), timeout=5.0)
        except OSError as error:
            raise SpiceApiError(f"could not reach spice2x on {host}:{port} ({error}) - "
                                f"is the game running with -api {port}?") from None
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def close(self) -> None:
        """Close the API socket."""
        self.sock.close()

    def call(self, module: str, function: str, params=None):
        """Send one request and return its `data` array.

        :param module: API module name, for example 'lights'
        :param function: function within that module
        :param params: list of parameters, or None
        :return: the reply's data array
        """
        request = {'id': self.next_id, 'module': module, 'function': function, 'params': params or []}
        self.next_id += 1
        payload = json.dumps(request).encode('utf-8') + b'\x00'
        self.sock.sendall(self.cipher.crypt(payload) if self.cipher else payload)

        while b'\x00' not in self.buffer:
            chunk = self.sock.recv(4096)
            # a closed socket is the game exiting; the bridge treats that as a
            # normal ending, so it stays a ConnectionError rather than becoming
            # a SpiceApiError
            if not chunk:
                raise ConnectionError("spice2x closed the connection")
            self.buffer += self.cipher.crypt(chunk) if self.cipher else chunk
        raw, _, rest = bytes(self.buffer).partition(b'\x00')
        self.buffer = bytearray(rest)
        reply = json.loads(raw.decode('utf-8'))
        if reply.get('errors'):
            raise SpiceApiError(f"spice2x error on {module}.{function}: {reply['errors']}")
        return reply.get('data', [])

    def lights_read(self) -> dict:
        """Read every light the game is driving as {name: state 0.0-1.0}.

        The API returns one entry per light. The reference clients read them as
        [name, state, active] arrays; objects are accepted too, in case a
        future version changes shape.

        :return: mapping of light name to its state
        """
        states = {}
        for entry in self.call('lights', 'read'):
            if isinstance(entry, dict):
                name, state = entry.get('name'), entry.get('state', 0.0)
            elif isinstance(entry, (list, tuple)) and len(entry) >= 2:
                name, state = entry[0], entry[1]
            else:
                continue
            if name is not None:
                states[str(name)] = float(state)
        return states
