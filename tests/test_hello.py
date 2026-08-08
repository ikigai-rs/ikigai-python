"""The version hello: codec golden bytes, mismatch errors, and the v7
posture — the hello is REQUIRED, and each failure shape gets its own honest
diagnosis (mismatch names both versions; a hang-up is pre-v6; silence is a
hang, not proof of age)."""

import socket
import struct
import threading
import time

import pytest

import ikigai
from ikigai import wire
from ikigai.serve import Server, endpoint
from ikigai.wire import Hello, HelloMode, decode_hello, encode_hello


def test_hello_golden_bytes_match_the_rust_layout():
    # The exact bytes are a PUBLIC contract (ikigai-wire locks the same
    # vector): magic + u32 BE version + u8 mode.
    assert encode_hello(Hello(7, HelloMode.ALIAS)) == b"IKWH\x00\x00\x00\x07\x01"
    assert encode_hello(Hello(7)) == b"IKWH\x00\x00\x00\x07\x00"


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
    with pytest.raises(wire.ProtocolError, match=r"v9.*v7|v7.*v9"):
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
