"""The QUIC transport against a scripted aioquic door (no Rust host needed).

What the Rust ``ikigai-quic`` crate fixes, pinned here: the stream carries the postcard
payload with NO length header and no hello, ALPN ``ikigai/8`` then ``ikigai/7`` is the
version, the server certificate is pinned byte for byte, a read whose reply was lost is
sent again on a new connection and a write never is. ``tests/test_quic_interop.py`` runs
the same client against the real Rust door.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

pytest.importorskip("aioquic")

from ikigai import ConnectionLost, ProtocolError, Representation, Request, Verb, wire  # noqa: E402
from ikigai.quic import (  # noqa: E402
    ALPNS,
    CertSet,
    CertSetError,
    NotEnrolledError,
    PinMismatchError,
    QuicClient,
    connect,
    fingerprint,
    fingerprint_of_pem,
    is_describe,
    parse_door,
    replayable,
)
from ikigai.wire import CacheStatus, Resolved  # noqa: E402
from tests.quic_stub import Close, StubDoor, identity, write_set  # noqa: E402
from tests.test_wire import CALLS, REPLIES  # noqa: E402


@pytest.fixture
def tmp():
    with tempfile.TemporaryDirectory(prefix="ik-quic-") as d:
        yield Path(d)


@pytest.fixture
def server():
    return identity()


@pytest.fixture
def client_id():
    return identity()


def upper(text: str = "hi") -> Request:
    return Request(Verb.SOURCE, "urn:test:upper", {"in": wire.Inline(text.encode())})


def answer(text: bytes = b"HI") -> Resolved:
    return Resolved(Representation(text, "text/plain"), CacheStatus.MISS)


@pytest.fixture
def door(tmp, server):
    doors = []

    def start(respond, **kwargs):
        d = StubDoor(server, respond, tmp=tmp, **kwargs)
        doors.append(d)
        return d

    yield start
    for d in doors:
        d.stop()


@pytest.fixture
def certs(tmp, server, client_id):
    return write_set(tmp / "set", server[0], client_id)


# -- the contract, without a network -------------------------------------------------


def test_alpn_ids_are_the_rust_ones_newest_first():
    assert ALPNS == ("ikigai/9", "ikigai/8", "ikigai/7")
    assert wire.PROTOCOL_VERSION == 9 and wire.MIN_PROTOCOL_VERSION == 7


@pytest.mark.parametrize("call", CALLS, ids=lambda c: type(c).__name__)
def test_a_stream_carries_the_uds_frame_minus_its_length_header(call):
    # The same codec on both transports: QUIC drops the u32 BE prefix (the stream's FIN
    # frames the message) and sends no hello.
    payload = wire.encode_call(call)
    assert wire.frame(payload) == len(payload).to_bytes(4, "big") + payload


def test_parse_door():
    assert parse_door("quic://ikigai-rs.example:4434") == ("ikigai-rs.example", 4434)
    assert parse_door("quic://[::1]:7") == ("::1", 7)
    for bad in ("quic://host", "udp://h:1", "h:1", "quic://h:1/path", "quic://h:x"):
        with pytest.raises(ValueError):
            parse_door(bad)


def test_reads_replay_and_writes_never_do():
    def call(verb):
        return wire.Issue(Request(verb, "urn:x", {}))

    assert replayable(wire.EntriesCall()) and replayable(wire.IsCached(upper()))
    assert [replayable(call(v)) for v in Verb] == [
        v in (Verb.SOURCE, Verb.EXISTS, Verb.META) for v in Verb
    ]
    assert is_describe(wire.EntriesCall()) and is_describe(call(Verb.META))
    assert not is_describe(call(Verb.SOURCE)) and not is_describe(wire.IsCached(upper()))


def test_fingerprint_is_sha256_of_the_der(client_id, tmp):
    pem = client_id[0]
    import ssl

    assert fingerprint_of_pem(pem) == fingerprint(ssl.PEM_cert_to_DER_cert(pem))
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("no openssl to cross-check against")
    (tmp / "c.crt").write_text(pem)
    der = subprocess.run(
        [openssl, "x509", "-in", str(tmp / "c.crt"), "-outform", "DER"],
        capture_output=True,
        check=True,
    ).stdout
    assert fingerprint_of_pem(pem) == fingerprint(der)


def test_a_missing_file_names_itself(tmp, server, client_id):
    certs = write_set(tmp / "s", server[0], client_id)
    (certs / "server.crt").unlink()
    with pytest.raises(CertSetError, match=r"server\.crt is missing: it should hold the door"):
        CertSet.from_dir(certs).load()


def test_a_key_from_another_pair_is_refused_before_dialing(tmp, server, client_id):
    certs = write_set(tmp / "s", server[0], client_id)
    (certs / "client.key").write_text(identity()[1])
    with pytest.raises(CertSetError, match="is not the key of"):
        CertSet.from_dir(certs).load()


def test_overrides_replace_one_file_each(tmp):
    s = CertSet.from_dir(tmp, server_cert=tmp / "door.crt")
    assert s.server_cert == tmp / "door.crt" and s.client_cert == tmp / "client.crt"


# -- against the stub door -----------------------------------------------------------


def test_round_trip_over_a_stream(door, certs):
    d = door(lambda call: answer())
    with connect(d.url, certs) as k:
        assert isinstance(k, QuicClient)
        assert k.server_version == 9
        assert k.source("urn:test:upper", **{"in": "hi"}).text == "HI"
    assert d.calls == [wire.Issue(upper())]


@pytest.mark.parametrize("reply", REPLIES, ids=lambda r: type(r).__name__)
def test_every_reply_variant_crosses_a_stream_intact(door, certs, reply):
    d = door(lambda call: reply)
    with connect(d.url, certs) as k:
        assert k._round_trip(wire.Issue(upper())) == reply


def test_typed_errors_raise_as_on_the_socket(door, certs):
    d = door(lambda call: wire.ErrorTypedReply(wire.ConflictError("square taken")))
    with connect(d.url, certs) as k, pytest.raises(wire.ConflictError) as raised:
        k.sink("urn:test:square", "x")
    assert raised.value.message == "square taken"


def test_a_v7_door_negotiates_7(door, certs):
    d = door(lambda call: answer(), alpns=["ikigai/7"])
    with connect(d.url, certs) as k:
        assert k.server_version == 7
        assert k.source("urn:test:upper").text == "HI"


def test_a_door_outside_the_range_is_refused_naming_versions(door, certs):
    d = door(lambda call: answer(), alpns=["ikigai/6"])
    with pytest.raises(ProtocolError, match="v7..=v9"):
        connect(d.url, certs)


def test_an_unpinned_server_is_refused_and_hears_nothing(door, tmp, client_id):
    d = door(lambda call: answer())
    other = write_set(tmp / "other", identity()[0], client_id)
    with pytest.raises(PinMismatchError, match="is not the pinned door"):
        connect(d.url, other)
    assert d.calls == []


def test_a_trusted_but_unenrolled_certificate_is_named(door, certs, client_id):
    d = door(lambda call: Close(1, "no authority is configured for this client certificate"))
    with connect(d.url, certs) as k, pytest.raises(NotEnrolledError) as refused:
        k.source("urn:test:upper")
    assert refused.value.fingerprint == fingerprint_of_pem(client_id[0])
    assert refused.value.fingerprint in str(refused.value)


def test_a_silent_door_costs_the_call_deadline(door, certs):
    d = door(lambda call: None)
    with (
        connect(d.url, certs, timeout=0.5) as k,
        pytest.raises(ConnectionLost, match=r"within 0\.5s"),
    ):
        k.source("urn:test:upper")


def test_an_unreachable_door_is_connection_lost(certs):
    with pytest.raises(ConnectionLost):
        connect("quic://127.0.0.1:9", certs, connect_timeout=0.5)


def test_a_read_whose_reply_was_lost_is_sent_again_on_a_new_connection(door, certs):
    seen = []

    def respond(call):
        seen.append(call)
        return Close(3, "aged") if len(seen) == 1 else answer()

    d = door(respond)
    with connect(d.url, certs) as k:
        assert k.source("urn:test:upper").text == "HI"
    assert len(d.calls) == 2 and d.connections == 2


def test_a_write_whose_reply_was_lost_is_never_sent_again(door, certs):
    d = door(lambda call: Close(3, "aged"))
    with connect(d.url, certs) as k, pytest.raises(ConnectionLost):
        k.sink("urn:test:notes", "once")
    assert len(d.calls) == 1


def test_a_connection_the_door_closed_between_calls_heals_even_for_a_write(door, certs):
    # The door bounds connection age and closes an idle connection (code 3). Nothing was in
    # flight, so the next call, a write included, goes on a new connection exactly once.
    d = door(lambda call: (answer(), Close(3, "aged")) if len(d.calls) == 1 else answer())
    with connect(d.url, certs) as k:
        assert k.source("urn:test:upper").text == "HI"
        deadline = time.monotonic() + 5
        while not k._inner._wire.protocol.ended:
            assert time.monotonic() < deadline, "the close never arrived"
            time.sleep(0.01)
        assert k.sink("urn:test:notes", "once").text == "HI"
    assert [type(c).__name__ for c in d.calls] == ["Issue", "Issue"]
    assert d.calls[1].request.verb == Verb.SINK and d.connections == 2
