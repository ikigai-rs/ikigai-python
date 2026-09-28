import io

import pytest

from ikigai import wire
from ikigai.wire import (
    Cached,
    CacheStatus,
    Capability,
    ConflictError,
    Content,
    DeniedError,
    EndpointError,
    EntriesCall,
    EntriesReply,
    ErrorReply,
    ErrorTypedReply,
    Expiry,
    Inline,
    InvalidArgumentError,
    IsCached,
    Issue,
    IssueAs,
    IssueTraced,
    MissingArgumentError,
    NotFoundError,
    ProtocolError,
    Reference,
    Representation,
    Request,
    Resolved,
    ResolvedTraced,
    SpaceEntry,
    TimeoutError,
    TraceContext,
    TraceEvent,
    UnavailableError,
    UnresolvedError,
    Verb,
)

# The one IRI the byte-exact fixtures below encode. It is deliberately in the
# urn:test: namespace: a codec fixture is not a real module, so it must not move
# when ikigai-fn is renamed. ikigai-cli's crates/ikigai-wire/src/lib.rs and
# ikigai-deno's tests/wire_test.ts pin this same value, which is what makes these
# vectors comparable across the three implementations.
SHARED_IRI = "urn:test:upper"

# Its postcard encoding: a varint length prefix then the UTF-8 bytes. Spelled as
# a literal here (that is the point of a byte-exact fixture) and checked against
# SHARED_IRI by test_shared_iri_encoding_matches_the_literal, so a rename cannot
# leave a stale prefix that silently decodes a truncated IRI.
SHARED_IRI_BYTES = b"\x0eurn:test:upper"


def upper_request() -> Request:
    return Request(Verb.SOURCE, SHARED_IRI, {"in": Inline(b"hi")})


# --- byte-exact fixtures (hand-derived from the Rust type declarations) ---


def test_entries_call_is_one_byte():
    assert wire.encode_call(EntriesCall()) == b"\x02"


def test_issue_call_bytes():
    encoded = wire.encode_call(Issue(upper_request()))
    expected = (
        b"\x00"  # Call::Issue
        b"\x00"  # Verb::Source (variant index 0, NOT the repr(u8) value 1)
        + SHARED_IRI_BYTES  # Iri newtype = string (0x0e = 14 bytes)
        + (
            b"\x01"  # args: 1 entry
            b"\x02in"  # key
            b"\x01"  # ArgRef::Inline
            b"\x02hi"  # value bytes
        )
    )
    assert encoded == expected


def test_shared_iri_encoding_matches_the_literal():
    # Guards the length trap: SHARED_IRI_BYTES is hand-written, so recompute the
    # prefix from the string every run. 14 bytes => 0x0e.
    assert SHARED_IRI_BYTES == bytes([len(SHARED_IRI.encode())]) + SHARED_IRI.encode()


def test_resolved_reply_bytes():
    reply = Resolved(Representation(b"HI", "text/plain"), CacheStatus.MISS)
    expected = (
        b"\x00"  # Reply::Resolved
        b"\x0atext/plain"  # ReprType.media_type
        b"\x00"  # ReprType.params: empty map
        b"\x02HI"  # bytes
        b"\x00"  # Expiry::Always
        b"\x01"  # CacheStatus::Miss
    )
    assert wire.encode_reply(reply) == expected


def test_framing_is_u32_be_length_prefixed():
    framed = wire.frame(b"\x02")
    assert framed == b"\x00\x00\x00\x01\x02"


def test_error_typed_wire_discriminant_is_five():
    # ErrorTyped's postcard discriminant is part of the public ABI — lock it
    # (the Rust suite pins the same vector).
    assert wire.encode_reply(ErrorTypedReply(EndpointError("x")))[0] == 5
    assert wire.encode_reply(ErrorTypedReply(DeniedError("x"))) == b"\x05\x04\x01x"


def test_conflict_is_wire_error_variant_8_reference_vector():
    # The v8 reference vector, pinned byte-exact in all three suites (Rust,
    # Python, Deno) beside the Denied one: ErrorTyped(Conflict("x")) is
    # 05 08 01 78 — Conflict APPENDED after Unavailable (7), never reordered.
    encoded = wire.encode_reply(ErrorTypedReply(ConflictError("x")))
    assert encoded == bytes.fromhex("05080178")
    got = wire.decode_reply(encoded).error
    assert type(got) is ConflictError
    assert got.message == "x"
    assert got.transient is False  # permanent: a retry meets the same state


# --- v8 backward compatibility: negotiation and the per-peer downgrade ---


def test_a_conflict_to_a_v7_peer_is_downgraded_to_the_v7_bytes():
    # A v7 peer has no variant 8. It receives Endpoint("conflict: x") — the
    # Rust core's Display of Conflict, byte-identical to what crossed before
    # v8 existed: ErrorTyped(3), then the string.
    downgraded = wire.encode_reply(ErrorTypedReply(ConflictError("x")), peer_version=7)
    assert downgraded == b"\x05\x03\x0bconflict: x"
    assert downgraded == wire.encode_reply(ErrorTypedReply(EndpointError("conflict: x")))
    # …while a v8 peer receives it typed.
    assert wire.encode_reply(ErrorTypedReply(ConflictError("x")), peer_version=8) == bytes.fromhex(
        "05080178"
    )


def test_accepted_versions_are_the_floor_through_ours():
    assert (wire.MIN_PROTOCOL_VERSION, wire.PROTOCOL_VERSION) == (7, 8)
    assert wire.accepted_version(8) == 8
    assert wire.accepted_version(7) == 7  # served AT the peer's version
    assert wire.accepted_version(6) is None  # below the floor: refused as before
    assert wire.accepted_version(9) is None  # a newer peer redials at ours


def test_a_dialer_redials_only_down_and_only_within_reach():
    assert wire.connect_version(8, 8) == "proceed"
    assert wire.connect_version(8, 7) == 7  # a v7 server: redial offering 7
    assert wire.connect_version(7, 7) == "proceed"
    assert wire.connect_version(8, 6) == "refuse"  # below the floor
    assert wire.connect_version(8, 9) == "refuse"  # a newer server answers ours, not its own
    assert wire.connect_version(7, 6) == "refuse"


# --- round trips over every variant ---


CALLS = [
    Issue(upper_request()),
    IsCached(upper_request()),
    EntriesCall(),
    IssueAs(upper_request(), Capability.root()),
    IssueAs(
        Request(
            Verb.SINK,
            "urn:file:notes.txt",
            {
                "ref": Reference("urn:x"),
                "cid": Content("b3:" + "ab" * 32),
                "value": Inline(b"\x00\xff"),
            },
        ),
        Capability.scoped(["urn:cap:fs:write", "urn:cap:demo"]),
    ),
    IssueTraced(
        upper_request(),
        Capability.root(),
        TraceContext(trace_id=7, parent_span=None),
    ),
    IssueTraced(
        upper_request(),
        Capability.scoped([]),
        TraceContext(trace_id=2**40, parent_span=3),
    ),
]

REPLIES = [
    Resolved(Representation(b"HI", "text/plain"), CacheStatus.MISS),
    Resolved(
        Representation(
            b"{}",
            "application/json",
            params={"charset": "utf-8"},
            expiry=Expiry.never(),
        ),
        CacheStatus.HIT,
    ),
    Resolved(
        Representation(b"x", "text/plain", expiry=Expiry.at(1_722_000_000_000)),
        CacheStatus.UNCACHEABLE,
    ),
    Cached(True),
    Cached(False),
    EntriesReply(None),
    EntriesReply(()),
    EntriesReply(
        (
            SpaceEntry(SHARED_IRI, "upper"),
            SpaceEntry("urn:py:hello", "hello", origin="/tmp/py.sock"),
        )
    ),
    ErrorReply("endpoint error: boom"),
    ErrorTypedReply(UnresolvedError("urn:x:y")),
    ErrorTypedReply(MissingArgumentError("in")),
    ErrorTypedReply(InvalidArgumentError("n", "not a number")),
    ErrorTypedReply(EndpointError("boom")),
    ErrorTypedReply(DeniedError("needs urn:cap:x")),
    ErrorTypedReply(NotFoundError("no such row")),
    ErrorTypedReply(TimeoutError("5s elapsed")),
    ErrorTypedReply(UnavailableError("connection refused")),
    ErrorTypedReply(ConflictError("square taken")),
    ResolvedTraced(
        Representation(b"HI", "text/plain"),
        CacheStatus.MISS,
        (
            TraceEvent(
                target=SHARED_IRI,
                thread="ikigai-sched-0",
                started=None,
                ended=None,
                cache_hit=False,
                span=0,
                parent=None,
                capability=("urn:cap:demo",),
                notes=(("model", "llama3.2:3b"),),
            ),
            TraceEvent(
                target="urn:child",
                thread="t",
                started=1000,
                ended=2000,
                cache_hit=True,
                span=1,
                parent=0,
                capability=None,
            ),
        ),
    ),
]


@pytest.mark.parametrize("call", CALLS, ids=lambda c: type(c).__name__)
def test_call_round_trip(call):
    assert wire.decode_call(wire.encode_call(call)) == call


@pytest.mark.parametrize("reply", REPLIES, ids=lambda r: type(r).__name__)
def test_reply_round_trip(reply):
    assert wire.decode_reply(wire.encode_reply(reply)) == reply


def test_args_encode_in_btreemap_key_order():
    # Rust's BTreeMap<String, _> iterates in UTF-8 byte order; the canonical
    # encoding must match regardless of Python insertion order.
    a = Request(Verb.SOURCE, "urn:x", {"b": Inline(b"2"), "a": Inline(b"1")})
    b = Request(Verb.SOURCE, "urn:x", {"a": Inline(b"1"), "b": Inline(b"2")})
    assert wire.encode_call(Issue(a)) == wire.encode_call(Issue(b))


# --- failure modes ---


def test_unknown_call_variant_names_the_protocol_version():
    with pytest.raises(ProtocolError, match=r"v8"):
        wire.decode_call(b"\x09")


def test_unknown_reply_variant_names_the_protocol_version():
    with pytest.raises(ProtocolError, match=r"protocol v8"):
        wire.decode_reply(b"\x2a")


def test_typed_errors_round_trip_with_taxonomy_intact():
    # Every taxonomy variant crosses and comes back as the SAME exception
    # type, with transience preserved — the property the HTTP faces and any
    # retry/failover logic depend on (mirrors the Rust suite's
    # typed_errors_round_trip_with_taxonomy_intact).
    cases = [
        (UnresolvedError("urn:x:y"), False),
        (MissingArgumentError("in"), False),
        (InvalidArgumentError("n", "not a number"), False),
        (EndpointError("boom"), False),
        (DeniedError("needs urn:cap:x"), False),
        (NotFoundError("no such row"), False),
        (TimeoutError("5s elapsed"), True),
        (UnavailableError("connection refused"), True),
        (ConflictError("square taken"), False),
    ]
    for original, transient in cases:
        got = wire.decode_reply(wire.encode_reply(ErrorTypedReply(original))).error
        assert type(got) is type(original), original
        assert got.message == original.message
        assert got.transient is transient, original


def test_typed_error_fields_survive_the_wire():
    encoded = wire.encode_reply(ErrorTypedReply(InvalidArgumentError("n", "x")))
    got = wire.decode_reply(encoded).error
    assert (got.name, got.detail) == ("n", "x")
    assert got.message == "invalid argument `n`: x"
    unresolved = wire.decode_reply(b"\x05\x00\x05urn:x").error
    assert unresolved.iri == "urn:x"
    assert unresolved.message == "no endpoint resolved for urn:x"
    missing = wire.decode_reply(b"\x05\x01\x02in").error
    assert missing.name == "in"
    assert missing.message == "missing required argument `in`"


def test_a_remote_timeout_is_also_a_builtin_timeout():
    import builtins

    got = wire.decode_reply(b"\x05\x06\x04slow").error
    assert isinstance(got, builtins.TimeoutError)
    assert isinstance(got, EndpointError)


def test_unknown_future_error_variant_degrades_to_the_base_loudly():
    # A newer peer's taxonomy addition: the payload layout is unknowable, so
    # the reply degrades to the BASE EndpointError naming the variant — never
    # a mistyped (e.g. transient) failure, never a decode crash.
    reply = wire.decode_reply(b"\x05\x63\x04\xde\xad\xbe\xef")
    assert type(reply.error) is EndpointError
    assert "unknown wire error variant 99" in reply.error.message
    assert reply.error.transient is False


def test_truncated_payload_is_a_decode_error():
    encoded = wire.encode_call(Issue(upper_request()))
    with pytest.raises(Exception, match="truncated"):
        wire.decode_call(encoded[:-1])


def test_trailing_garbage_is_rejected():
    encoded = wire.encode_call(EntriesCall()) + b"\x00"
    with pytest.raises(Exception, match="trailing"):
        wire.decode_call(encoded)


def test_read_frame_round_trip():
    buf = io.BytesIO()
    wire.write_frame(buf, b"payload")
    wire.write_frame(buf, b"")
    buf.seek(0)
    assert wire.read_frame(buf) == b"payload"
    assert wire.read_frame(buf) == b""


def test_read_frame_eof_on_close():
    with pytest.raises(EOFError):
        wire.read_frame(io.BytesIO(b""))


def test_read_frame_eof_mid_frame():
    framed = wire.frame(b"payload")[:-1]
    with pytest.raises(EOFError, match="mid-frame"):
        wire.read_frame(io.BytesIO(framed))


def test_oversized_length_header_rejected_before_allocating():
    header = (wire.MAX_FRAME + 1).to_bytes(4, "big") + b"\x00"
    with pytest.raises(ProtocolError, match="exceeds"):
        wire.read_frame(io.BytesIO(header))


def test_error_prefix_stripping():
    assert wire.decode_error_message("endpoint error: boom") == "boom"
    assert wire.decode_error_message("no endpoint resolved for urn:x") == (
        "no endpoint resolved for urn:x"
    )


def test_representation_media_type_is_canonical():
    rep = Representation(b"", "text/plain;charset=utf-8")
    assert rep.base_media_type == "text/plain"
    assert rep.media_type == "text/plain;charset=utf-8"
    both = Representation(b"", "text/plain", params={"charset": "utf-8", "boundary": "x"})
    assert both.media_type == "text/plain;boundary=x;charset=utf-8"


@pytest.mark.parametrize("reply", REPLIES, ids=lambda r: type(r).__name__)
def test_only_conflict_changes_for_a_v7_peer(reply):
    # Every other reply a v7 peer receives is exactly what v8 sends: the
    # downgrade touches variant 8 and nothing else.
    if isinstance(reply, ErrorTypedReply) and isinstance(reply.error, ConflictError):
        return
    assert wire.encode_reply(reply, peer_version=7) == wire.encode_reply(reply)
