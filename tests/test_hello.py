"""The version hello: codec golden bytes, mismatch errors, and the v7
posture — the hello is REQUIRED, and each failure shape gets its own honest
diagnosis (mismatch names both versions; a hang-up is pre-v6; silence is a
hang, not proof of age) — plus the v8 posture: a v7 peer is accepted in
both directions, and never receives a v8-only variant."""

import asyncio
import socket
import struct
import threading
import time

import pytest

import ikigai
from ikigai import aio, wire
from ikigai.serve import Server, endpoint
from ikigai.wire import Hello, HelloMode, decode_hello, encode_hello


def test_hello_golden_bytes_match_the_rust_layout():
    # The exact bytes are a PUBLIC contract (ikigai-wire locks the same
    # vector): magic + u32 BE version + u8 mode.
    assert encode_hello(Hello(7, HelloMode.ALIAS)) == b"IKWH\x00\x00\x00\x07\x01"
    assert encode_hello(Hello(7)) == b"IKWH\x00\x00\x00\x07\x00"
    assert encode_hello(Hello(8)) == b"IKWH\x00\x00\x00\x08\x00"


def test_hello_decode_is_prefix_only_and_hint_tolerant():
    # Trailing bytes are the extension mechanism; an unknown mode byte is a
    # hint from a NEWER peer and falls back to verbatim instead of failing.
    assert decode_hello(encode_hello(Hello(9)) + b"future") == Hello(9)
    odd = bytearray(encode_hello(Hello(9)))
    odd[8] = 7
    assert decode_hello(bytes(odd)) == Hello(9, HelloMode.VERBATIM)
    # A pre-v6 first frame (a postcard Call) has no magic.
    assert decode_hello(wire.encode_call(wire.EntriesCall())) is None


@endpoint("urn:py:hi", summary="hi", args=["who"])
def hi(who: str) -> str:
    return f"hi {who}"


def test_a_version_mismatch_names_both_versions(socket_dir):
    # A future v9 server: answers the hello with its own version, closes.
    path = socket_dir / "hello.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(1)

    def v9_server():
        conn, _ = listener.accept()
        with conn, conn.makefile("rwb") as f:
            wire.read_frame(f)
            wire.write_frame(f, encode_hello(Hello(9)))

    t = threading.Thread(target=v9_server, daemon=True)
    t.start()
    with pytest.raises(wire.ProtocolError, match=r"v9.*v8|v8.*v9"):
        ikigai.connect(path)
    t.join(timeout=5)
    listener.close()


def test_a_pre_v6_server_hang_up_is_refused_with_the_diagnosis(socket_dir):
    # A <= v5 server drops an undecodable frame SILENTLY (EOF). Since v7
    # there is no legacy reconnect: the client refuses, naming the diagnosis.
    path = socket_dir / "hello.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(1)

    def v5_rust_server():
        conn, _ = listener.accept()
        with conn, conn.makefile("rwb") as f:
            wire.read_frame(f)  # cannot decode it; hang up

    t = threading.Thread(target=v5_rust_server, daemon=True)
    t.start()
    with pytest.raises(wire.ProtocolError, match="predates wire v6"):
        ikigai.connect(path)
    t.join(timeout=5)
    listener.close()


def test_a_silent_server_is_reported_hung_not_ancient(socket_dir):
    # Silence on the hello is a HANG (the server may merely be overloaded) —
    # it must NOT be misdiagnosed as a pre-v6 hang-up. The read deadline
    # trips into ConnectionLost with the hung-or-overloaded wording.
    path = socket_dir / "hello.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(1)
    release = threading.Event()

    def silent_server():
        conn, _ = listener.accept()
        with conn:
            release.wait(10)  # hold the connection open, say nothing

    t = threading.Thread(target=silent_server, daemon=True)
    t.start()
    start = time.monotonic()
    with pytest.raises(ikigai.ConnectionLost, match="hung or overloaded"):
        ikigai.connect(path, timeout=0.2)
    assert time.monotonic() - start < 2
    release.set()
    t.join(timeout=5)
    listener.close()


def test_a_client_without_a_hello_is_refused(socket_dir, capsys):
    # v7: the hello is REQUIRED. A first frame without the magic is a <= v5
    # client; the server refuses it (no reply, connection closed) instead of
    # serving it in legacy mode.
    path = socket_dir / "hello.sock"
    with Server([hi], path) as server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(5)
        sock.connect(str(path))
        with sock, sock.makefile("rwb") as f:
            wire.write_frame(f, wire.encode_call(wire.EntriesCall()))
            with pytest.raises(EOFError):
                wire.read_frame(f)
    assert "without the version hello" in capsys.readouterr().err


def test_the_header_is_big_endian_not_little():
    # Guard the byte order explicitly: postcard varints elsewhere are LE, and
    # a LE u32 here would round-trip within one implementation undetected.
    payload = encode_hello(Hello(7))
    assert struct.unpack(">I", payload[4:8]) == (7,)
    assert payload[4:8] == b"\x00\x00\x00\x07"


# -- v8: backward compatible with v7 -----------------------------------------


@endpoint("urn:py:claim", summary="claim a square", args=["square"])
def claim(square: str) -> str:
    raise ikigai.ConflictError(f"{square} is taken")


def _raw_hello(path, version: int):
    """Dial ``path`` as a peer that speaks ``version`` — the v8 code under
    test sees exactly the hello a real peer of that version sends."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(5)
    sock.connect(str(path))
    f = sock.makefile("rwb")
    wire.write_frame(f, encode_hello(Hello(version)))
    return sock, f, decode_hello(wire.read_frame(f))


def _issue(f, iri: str, **args) -> bytes:
    request = wire.Request(
        wire.Verb.SOURCE, iri, {k: wire.Inline(v.encode()) for k, v in args.items()}
    )
    wire.write_frame(f, wire.encode_call(wire.Issue(request)))
    return wire.read_frame(f)


def test_a_v7_hello_is_accepted_and_answered_as_v7(socket_dir):
    # A v7-only peer (ikigai-cli 0.1.29) requires the answer to equal 7; a v8
    # server answers with the PEER'S version and serves the connection at it.
    path = socket_dir / "hello.sock"
    with Server([hi, claim], path) as server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        sock, f, answer = _raw_hello(path, 7)
        with sock, f:
            assert answer == Hello(7)
            reply = wire.decode_reply(_issue(f, "urn:py:hi", who="v7"))
            assert reply.representation.text == "hi v7"


def test_a_conflict_reaches_a_v7_peer_as_the_v7_bytes(socket_dir):
    # The downgrade, pinned at the byte level on a live connection: the peer
    # said 7, so a handler's ConflictError leaves as Endpoint("conflict: …")
    # — exactly what a v7 peer received for a Conflict before v8 existed.
    path = socket_dir / "hello.sock"
    with Server([claim], path) as server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        sock, f, _ = _raw_hello(path, 7)
        with sock, f:
            raw = _issue(f, "urn:py:claim", square="b2")
    expected = wire.encode_reply(
        wire.ErrorTypedReply(ikigai.EndpointError("conflict: b2 is taken"))
    )
    assert raw == expected
    assert raw[:2] == b"\x05\x03"  # ErrorTyped, WireError::Endpoint — never variant 8


def test_a_v8_peer_receives_the_typed_conflict(socket_dir):
    path = socket_dir / "hello.sock"
    with Server([claim], path) as server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        sock, f, answer = _raw_hello(path, 8)
        with sock, f:
            assert answer == Hello(8)
            raw = _issue(f, "urn:py:claim", square="b2")
        assert raw[:2] == b"\x05\x08"  # ErrorTyped, WireError::Conflict
        with ikigai.connect(path) as k:
            assert k.server_version == 8
            with pytest.raises(ikigai.ConflictError, match="b2 is taken") as e:
                k.source("urn:py:claim", square="b2")
            assert e.value.transient is False


@pytest.mark.parametrize("version", [6, 9])
def test_a_hello_out_of_reach_is_refused(socket_dir, version):
    # Below the floor (a v6 client) or above ours (a v9 client): answered with
    # OUR version so the peer can name both (or, if it negotiates, redial at
    # ours), then closed without serving a single Call.
    path = socket_dir / "hello.sock"
    with Server([hi], path) as server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        sock, f, answer = _raw_hello(path, version)
        with sock, f:
            assert answer == Hello(wire.PROTOCOL_VERSION)
            assert f.read(1) == b""  # the server closed: nothing is served


class V7OnlyServer:
    """What ikigai-cli 0.1.29 does with a hello: answer with 7 always, and
    hang up unless the client offered exactly 7. Serves ``Entries`` (so the
    client can prove the connection is live) and records every offer."""

    def __init__(self, path):
        self.offers: list[int] = []
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

    def _handle(self, conn):
        with conn, conn.makefile("rwb") as f:
            try:
                hello = decode_hello(wire.read_frame(f))
                self.offers.append(hello.version)
                wire.write_frame(f, encode_hello(Hello(7)))
                if hello.version != 7:
                    return
                while True:
                    wire.decode_call(wire.read_frame(f))
                    entries = (wire.SpaceEntry("urn:iki:fn:toUpper", "toUpper"),)
                    wire.write_frame(f, wire.encode_reply(wire.EntriesReply(entries)))
            except (EOFError, OSError):
                return

    def close(self):
        self._listener.close()


def test_a_v8_client_redials_a_v7_server_at_v7(socket_dir):
    path = socket_dir / "v7.sock"
    server = V7OnlyServer(path)
    try:
        with ikigai.connect(path) as k:
            assert k.server_version == 7
            assert [e.endpoint for e in k.entries()] == ["toUpper"]
        assert server.offers == [8, 7]  # offered ours, stepped down ONCE
    finally:
        server.close()


def test_the_async_client_redials_a_v7_server_at_v7(socket_dir):
    path = socket_dir / "v7.sock"
    server = V7OnlyServer(path)

    async def scenario():
        async with await aio.connect(path) as k:
            assert k.server_version == 7
            assert [e.endpoint for e in await k.entries()] == ["toUpper"]

    try:
        asyncio.run(scenario())
        assert server.offers == [8, 7]
    finally:
        server.close()


def test_a_v6_server_is_refused_naming_both_versions(socket_dir):
    # Below the floor: no redial, a clean error naming both sides.
    path = socket_dir / "hello.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(1)

    def v6_server():
        conn, _ = listener.accept()
        with conn, conn.makefile("rwb") as f:
            wire.read_frame(f)
            wire.write_frame(f, encode_hello(Hello(6)))

    t = threading.Thread(target=v6_server, daemon=True)
    t.start()
    with pytest.raises(wire.ProtocolError, match=r"speaks wire v6.*v8"):
        ikigai.connect(path)
    t.join(timeout=5)
    listener.close()
