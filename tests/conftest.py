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
    """Accepts connections, answers the wire v6 hello, then answers each
    decoded Call via ``respond``."""

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
            # The first frame decides the era, mirroring ``ikigai.serve``: a
            # hello is answered with ours (so clients under test exercise the
            # v6 wire path, not their legacy-reconnect fallback); a frame
            # without the magic is a <= v5 client's first Call, served as one.
            try:
                first = wire.read_frame(f)
            except (EOFError, OSError):
                return
            if wire.decode_hello(first) is not None:
                try:
                    wire.write_frame(f, wire.encode_hello(wire.Hello(wire.PROTOCOL_VERSION)))
                except OSError:
                    return
            elif not self._serve_one_frame(f, first):
                return
            while True:
                try:
                    frame = wire.read_frame(f)
                except (EOFError, OSError):
                    return
                if not self._serve_one_frame(f, frame):
                    return

    def _serve_one_frame(self, f, frame: bytes) -> bool:
        """Answer one Call frame; ``False`` ends the connection."""
        reply = self._respond(wire.decode_call(frame))
        if reply is None:
            # Scripted hang: hold the connection open, say nothing —
            # the client's read deadline is what's under test.
            time.sleep(10)
            return False
        try:
            wire.write_frame(f, wire.encode_reply(reply))
        except OSError:
            return False
        return True

    def close(self):
        self._listener.close()


class ExamplesPeer:
    """The examples' endpoint set served in-process on a temp socket, with
    ``IKIGAI_SOCKET`` pointing at it — what every example app reads at
    startup."""

    def __init__(self, path: Path, endpoints):
        from ikigai.serve import Server

        self.path = path
        self.server = Server(endpoints, path)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.thread.join(timeout=5)


@pytest.fixture
def examples_peer(socket_dir, monkeypatch):
    from examples.endpoints import ENDPOINTS

    peer = ExamplesPeer(socket_dir / "examples.sock", ENDPOINTS)
    monkeypatch.setenv("IKIGAI_SOCKET", str(peer.path))
    yield peer
    peer.stop()


@pytest.fixture
def partial_peer(socket_dir, monkeypatch):
    """A space serving ONLY urn:py:hello — /upper then hits an unresolved
    target, which is how the smoke tests exercise the 502 mapping."""
    from examples.endpoints import hello

    peer = ExamplesPeer(socket_dir / "partial.sock", [hello])
    monkeypatch.setenv("IKIGAI_SOCKET", str(peer.path))
    yield peer
    peer.stop()


class DyingPeer:
    """Answers the wire v6 hello, then hangs up on the first real Call.

    The example apps connect fine at startup, but their first resolution
    gets EOF -> ``ConnectionLost`` -> the 503 mapping under test. (Simply
    shutting an ``ikigai.serve`` Server down does not sever connections it
    has already accepted, so this stub plays the dying peer instead.)"""

    def __init__(self, path: Path):
        self.path = path
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._listener.bind(str(path))
        self._listener.listen()
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self._listener.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket):
        with conn, conn.makefile("rwb") as f:
            try:
                if wire.decode_hello(wire.read_frame(f)) is not None:
                    wire.write_frame(f, wire.encode_hello(wire.Hello(wire.PROTOCOL_VERSION)))
                    wire.read_frame(f)  # the first real Call...
            except (EOFError, OSError):
                pass
            # ...answered by hanging up.

    def close(self):
        self._listener.close()


@pytest.fixture
def dying_peer(socket_dir, monkeypatch):
    peer = DyingPeer(socket_dir / "dying.sock")
    monkeypatch.setenv("IKIGAI_SOCKET", str(peer.path))
    yield peer
    peer.close()


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
