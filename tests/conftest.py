"""Shared fixtures: a minimal in-process wire server for client tests.

This stub is NOT the real ``ikigai.serve`` (that has its own tests) — it
answers each connection with scripted logic so client behavior (framing,
errors, timeouts) is tested in isolation.
"""

from __future__ import annotations

import socket
import tempfile
import threading
import time
from pathlib import Path

import pytest

from ikigai import wire


@pytest.fixture
def socket_dir():
    # UDS paths are length-limited (~104 bytes on macOS); tempfile's default
    # location is short enough, unlike deep per-session scratch directories.
    with tempfile.TemporaryDirectory(prefix="ik-py-") as d:
        yield Path(d)


class StubServer:
    """Accepts connections and answers each decoded Call via ``respond``."""

    def __init__(self, path: Path, respond):
        self.path = path
        self._respond = respond
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._listener.bind(str(path))
        self._listener.listen()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while True:
            try:
                conn, _ = self._listener.accept()
            except OSError:
                return  # listener closed
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket):
        with conn, conn.makefile("rwb") as f:
            while True:
                try:
                    call = wire.decode_call(wire.read_frame(f))
                except (EOFError, OSError):
                    return
                reply = self._respond(call)
                if reply is None:
                    # Scripted hang: hold the connection open, say nothing —
                    # the client's read deadline is what's under test.
                    time.sleep(10)
                    return
                wire.write_frame(f, wire.encode_reply(reply))

    def close(self):
        self._listener.close()


@pytest.fixture
def stub_server(socket_dir):
    servers = []

    def start(respond) -> Path:
        path = socket_dir / f"stub-{len(servers)}.sock"
        servers.append(StubServer(path, respond))
        return path

    yield start
    for server in servers:
        server.close()
