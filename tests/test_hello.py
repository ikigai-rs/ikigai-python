"""The wire v6 hello: codec golden bytes, mismatch errors, both tolerances."""

import socket
import struct
import threading

import pytest

import ikigai
from ikigai import wire
from ikigai.serve import Server, endpoint
from ikigai.wire import Hello, HelloMode, decode_hello, encode_hello


def test_hello_golden_bytes_match_the_rust_layout():
    # The exact bytes are a PUBLIC contract (ikigai-wire locks the same
    # vector): magic + u32 BE version + u8 mode.
    assert encode_hello(Hello(6, HelloMode.ALIAS)) == b"IKWH\x00\x00\x00\x06\x01"
    assert encode_hello(Hello(6)) == b"IKWH\x00\x00\x00\x06\x00"


def test_hello_decode_is_prefix_only_and_hint_tolerant():
    # Trailing bytes are the extension mechanism; an unknown mode byte is a
    # hint from a NEWER peer and falls back to verbatim instead of failing.
    assert decode_hello(encode_hello(Hello(9)) + b"future") == Hello(9)
    odd = bytearray(encode_hello(Hello(9)))
    odd[8] = 7
    assert decode_hello(bytes(odd)) == Hello(9, HelloMode.VERBATIM)
    # A legacy first frame (a postcard Call) has no magic.
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
    with pytest.raises(wire.ProtocolError, match=r"v9.*v6|v6.*v9"):
        ikigai.connect(path)
    t.join(timeout=5)
    listener.close()


def test_a_new_client_falls_back_against_a_pre_hello_server(socket_dir, capsys):
    # A <= v5 RUST server drops an undecodable frame SILENTLY; the client
    # must reconnect without the hello (warning loudly) and still work.
    path = socket_dir / "hello.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(2)

    def v5_rust_server():
        # First connection: read the hello, cannot decode it, hang up.
        conn, _ = listener.accept()
        with conn, conn.makefile("rwb") as f:
            wire.read_frame(f)
        # Second connection: legacy service, straight Calls.
        conn, _ = listener.accept()
        with conn, conn.makefile("rwb") as f:
            try:
                while True:
                    call = wire.decode_call(wire.read_frame(f))
                    if isinstance(call, wire.EntriesCall):
                        reply = wire.EntriesReply(())
                    else:
                        reply = wire.ErrorReply("endpoint error: not in this test")
                    wire.write_frame(f, wire.encode_reply(reply))
            except (EOFError, OSError):
                pass

    t = threading.Thread(target=v5_rust_server, daemon=True)
    t.start()
    k = ikigai.connect(path)
    assert k.server_version is None, "the fallback marks the server legacy"
    assert k.entries() == []
    k.close()
    t.join(timeout=5)
    listener.close()
    assert "predates wire v6" in capsys.readouterr().err


def test_a_legacy_client_without_a_hello_is_still_served(socket_dir, capsys):
    # A <= v5 client's first frame is a Call; the server serves it (warning)
    # under its configured default entries form.
    path = socket_dir / "hello.sock"
    with Server([hi], path) as server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(5)
        sock.connect(str(path))
        with sock, sock.makefile("rwb") as f:
            wire.write_frame(f, wire.encode_call(wire.EntriesCall()))
            reply = wire.decode_reply(wire.read_frame(f))
            assert isinstance(reply, wire.EntriesReply)
            assert {e.pattern for e in reply.entries} == {"urn:hi"}
    assert "without the version hello" in capsys.readouterr().err


def test_the_header_is_big_endian_not_little():
    # Guard the byte order explicitly: postcard varints elsewhere are LE, and
    # a LE u32 here would round-trip within one implementation undetected.
    payload = encode_hello(Hello(6))
    assert struct.unpack(">I", payload[4:8]) == (6,)
    assert payload[4:8] == b"\x00\x00\x00\x06"
