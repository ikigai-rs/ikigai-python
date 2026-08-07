"""The ikigai IPC wire protocol: types, postcard codec, and framing.

Mirrors ``ikigai-wire`` (Rust) at ``PROTOCOL_VERSION`` 5. The codec is
non-self-describing, so every type here restates a Rust layout
field-for-field; the Rust declaration is the normative source
(``ikigai-wire/src/lib.rs`` and the ``ikigai-core`` types it serializes).

Framing: a ``u32`` **big-endian** length header, then the postcard payload.
Frames above 64 MiB are rejected before allocation, both directions.

Since v6 the connection opens with a **hello exchange** (see the Rust
``docs/wire-hello-design.md``): the first frame each way is
``b"IKWH" + u32 BE version + u8 mode``, deliberately NOT postcard (the codec
whose version is being negotiated must not be needed to negotiate it), and
readers ignore trailing bytes — that is the extension mechanism. A version
mismatch is finally a clean error naming both sides instead of garbled
postcard. Pre-v6 peers are tolerated for one version: a server hung up on our
hello means a ≤v5 Rust server (reconnect without the hello, warn), and a
first frame without the magic means a ≤v5 client (serve it, warn).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from enum import IntEnum
from typing import BinaryIO

from .postcard import DecodeError, Reader, encode_varint

# Bumped in lockstep with the Rust `ikigai_wire::PROTOCOL_VERSION`. v5 era:
# core 0.1.48 `TraceEvent.notes` changed the postcard layout of traced replies.
# v6 adds the hello exchange (version + mount mode at connection open).
PROTOCOL_VERSION = 6

# The magic prefix of a hello payload; a first frame without it is a legacy
# (<= v5) Call.
HELLO_MAGIC = b"IKWH"

# The largest framed message accepted (matches the Rust MAX_FRAME).
MAX_FRAME = 64 * 1024 * 1024


class WireError(Exception):
    """Base for everything this package raises on purpose."""


class ProtocolError(WireError):
    """A frame or message that violates wire protocol v5.

    Because the wire carries no version handshake, a peer speaking a different
    protocol version also lands here — the message names the version this
    package speaks so the mismatch is diagnosable.
    """


class EndpointError(WireError):
    """A server-reported resolution failure (the wire's ``Reply::Error``)."""


# ---------------------------------------------------------------------------
# Core types (mirroring ikigai-core)
# ---------------------------------------------------------------------------


class Verb(IntEnum):
    """Request verbs. Values are the **postcard variant indexes** (0-based
    declaration order), not the ``#[repr(u8)]`` codes 1-5 — those exist only
    for Rust-side identity hashing and never cross the wire."""

    SOURCE = 0
    SINK = 1
    EXISTS = 2
    DELETE = 3
    META = 4

    @property
    def wire_name(self) -> str:
        """The serde name (used in JSON faces): ``Source``, ``Sink``, ..."""
        return self.name.capitalize()


@dataclass(frozen=True)
class Reference:
    """``ArgRef::Reference`` — an argument by IRI."""

    iri: str


@dataclass(frozen=True)
class Inline:
    """``ArgRef::Inline`` — a small literal value carried inline."""

    data: bytes


@dataclass(frozen=True)
class Content:
    """``ArgRef::Content`` — a content-store id. On the wire this is the
    STRING form ``b3:<hex>`` (Rust's ``ContentId`` serializes via
    ``#[serde(into = "String")]``), not the raw 32-byte digest."""

    content_id: str


ArgRef = Reference | Inline | Content


@dataclass(frozen=True)
class Request:
    """``ikigai_core::Request``: a verb, a target IRI, named arguments.

    Encoded as: verb enum, target (a plain string — ``Iri`` is a newtype),
    then the args as a map sorted by key (UTF-8 byte order, matching Rust's
    ``BTreeMap<String, _>``)."""

    verb: Verb
    target: str
    args: dict[str, ArgRef] = field(default_factory=dict)


@dataclass(frozen=True)
class Capability:
    """``ikigai_core::Capability``: root, or a set of ``urn:cap:`` scopes.

    Layout: the struct holds one private enum ``Kind`` — variant 0 ``Root``
    (unit), variant 1 ``Scoped(BTreeSet<String>)`` (sorted strings).
    ``scopes is None`` means root."""

    scopes: frozenset[str] | None = None

    @classmethod
    def root(cls) -> Capability:
        return cls(None)

    @classmethod
    def scoped(cls, scopes) -> Capability:
        return cls(frozenset(scopes))

    @property
    def is_root(self) -> bool:
        return self.scopes is None


@dataclass(frozen=True)
class Expiry:
    """``ikigai_core::Expiry``: Always (variant 0) | At(Time) (1) | Never (2).

    ``Time`` is a newtype over u64 milliseconds since the Unix epoch."""

    kind: str  # "always" | "at" | "never"
    at_millis: int | None = None

    @classmethod
    def always(cls) -> Expiry:
        return cls("always")

    @classmethod
    def never(cls) -> Expiry:
        return cls("never")

    @classmethod
    def at(cls, millis: int) -> Expiry:
        return cls("at", millis)


class CacheStatus(IntEnum):
    """``ikigai_resolve::CacheStatus`` (variant indexes)."""

    HIT = 0
    MISS = 1
    UNCACHEABLE = 2


class Representation:
    """``ikigai_core::Representation``: typed bytes plus cache validity.

    Wire layout: ``ReprType { media_type: String, params: BTreeMap }``, then
    the bytes, then the ``Expiry``. The Rust ``threads`` field is
    ``#[serde(skip)]`` — golden threads are kernel-local and never cross the
    wire.
    """

    def __init__(
        self,
        data: bytes,
        media_type: str = "text/plain",
        *,
        params: dict[str, str] | None = None,
        expiry: Expiry | None = None,
        cache_status: CacheStatus | None = None,
    ):
        base, _, rest = media_type.partition(";")
        parsed: dict[str, str] = {}
        if rest:
            for piece in rest.split(";"):
                key, _, value = piece.partition("=")
                parsed[key.strip()] = value.strip()
        if params:
            parsed.update(params)
        self.data = bytes(data)
        self.base_media_type = base.strip()
        self.params = parsed
        self.expiry = expiry or Expiry.always()
        #: How the server's cache answered (stamped by the client on receipt;
        #: not part of the representation itself).
        self.cache_status = cache_status

    @property
    def media_type(self) -> str:
        """The canonical form ``media/type;k=v;...`` with sorted params."""
        out = self.base_media_type
        for key in sorted(self.params):
            out += f";{key}={self.params[key]}"
        return out

    @property
    def text(self) -> str:
        return self.data.decode("utf-8")

    def __eq__(self, other) -> bool:
        return (
            isinstance(other, Representation)
            and self.data == other.data
            and self.base_media_type == other.base_media_type
            and self.params == other.params
            and self.expiry == other.expiry
        )

    def __repr__(self) -> str:
        return f"Representation({self.media_type!r}, {len(self.data)} bytes, {self.expiry.kind})"


@dataclass(frozen=True)
class SpaceEntry:
    """``ikigai_core::SpaceEntry``: one catalog binding.

    ``origin`` is ``None`` for a kernel's own bindings; a mount label for
    bindings surfaced from a mounted remote."""

    pattern: str
    endpoint: str
    origin: str | None = None


@dataclass(frozen=True)
class TraceContext:
    """``ikigai_wire::TraceContext``: trace id + optional parent span."""

    trace_id: int
    parent_span: int | None = None


@dataclass(frozen=True)
class TraceEvent:
    """``ikigai_core::TraceEvent`` (the v5 layout, including ``notes``)."""

    target: str
    thread: str
    started: int | None = None  # Time, millis since epoch
    ended: int | None = None
    cache_hit: bool = False
    span: int = 0
    parent: int | None = None
    capability: tuple[str, ...] | None = None  # None = root authority
    notes: tuple[tuple[str, str], ...] = ()


# ---------------------------------------------------------------------------
# Calls and replies (mirroring ikigai-wire)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Issue:
    """``Call::Issue`` (variant 0)."""

    request: Request


@dataclass(frozen=True)
class IsCached:
    """``Call::IsCached`` (variant 1)."""

    request: Request


@dataclass(frozen=True)
class EntriesCall:
    """``Call::Entries`` (variant 2, unit)."""


@dataclass(frozen=True)
class IssueAs:
    """``Call::IssueAs`` (variant 3): resolve under an explicit capability."""

    request: Request
    capability: Capability


@dataclass(frozen=True)
class IssueTraced:
    """``Call::IssueTraced`` (variant 4): resolve and record the spans."""

    request: Request
    capability: Capability
    context: TraceContext


Call = Issue | IsCached | EntriesCall | IssueAs | IssueTraced


@dataclass(frozen=True)
class Resolved:
    """``Reply::Resolved`` (variant 0)."""

    representation: Representation
    cache_status: CacheStatus


@dataclass(frozen=True)
class Cached:
    """``Reply::Cached`` (variant 1)."""

    cached: bool


@dataclass(frozen=True)
class EntriesReply:
    """``Reply::Entries`` (variant 2). ``entries is None`` = the space does
    not support enumeration (Rust's ``Option::None``)."""

    entries: tuple[SpaceEntry, ...] | None


@dataclass(frozen=True)
class ErrorReply:
    """``Reply::Error`` (variant 3): the server's rendered error string."""

    message: str


@dataclass(frozen=True)
class ResolvedTraced:
    """``Reply::ResolvedTraced`` (variant 4)."""

    representation: Representation
    cache_status: CacheStatus
    events: tuple[TraceEvent, ...]


Reply = Resolved | Cached | EntriesReply | ErrorReply | ResolvedTraced


# ---------------------------------------------------------------------------
# Encoding
# ---------------------------------------------------------------------------


def _sorted_utf8(keys) -> list[str]:
    """Rust ``BTreeMap<String, _>`` order: lexicographic over UTF-8 bytes."""
    return sorted(keys, key=lambda k: k.encode("utf-8"))


def _put_string(out: bytearray, s: str) -> None:
    raw = s.encode("utf-8")
    out += encode_varint(len(raw))
    out += raw


def _put_bytes(out: bytearray, b: bytes) -> None:
    out += encode_varint(len(b))
    out += b


def _put_option_varint(out: bytearray, value: int | None) -> None:
    if value is None:
        out.append(0)
    else:
        out.append(1)
        out += encode_varint(value)


def _put_arg_ref(out: bytearray, arg: ArgRef) -> None:
    if isinstance(arg, Reference):
        out += encode_varint(0)
        _put_string(out, arg.iri)
    elif isinstance(arg, Inline):
        out += encode_varint(1)
        _put_bytes(out, arg.data)
    elif isinstance(arg, Content):
        out += encode_varint(2)
        _put_string(out, arg.content_id)
    else:
        raise TypeError(f"not an ArgRef: {arg!r}")


def _put_request(out: bytearray, request: Request) -> None:
    out += encode_varint(int(request.verb))
    _put_string(out, request.target)
    out += encode_varint(len(request.args))
    for name in _sorted_utf8(request.args):
        _put_string(out, name)
        _put_arg_ref(out, request.args[name])


def _put_capability(out: bytearray, capability: Capability) -> None:
    if capability.scopes is None:
        out += encode_varint(0)  # Kind::Root
    else:
        out += encode_varint(1)  # Kind::Scoped(BTreeSet<String>)
        scopes = _sorted_utf8(capability.scopes)
        out += encode_varint(len(scopes))
        for scope in scopes:
            _put_string(out, scope)


def _put_expiry(out: bytearray, expiry: Expiry) -> None:
    if expiry.kind == "always":
        out += encode_varint(0)
    elif expiry.kind == "at":
        out += encode_varint(1)
        out += encode_varint(expiry.at_millis or 0)
    elif expiry.kind == "never":
        out += encode_varint(2)
    else:
        raise ValueError(f"unknown expiry kind {expiry.kind!r}")


def _put_representation(out: bytearray, representation: Representation) -> None:
    _put_string(out, representation.base_media_type)
    out += encode_varint(len(representation.params))
    for key in _sorted_utf8(representation.params):
        _put_string(out, key)
        _put_string(out, representation.params[key])
    _put_bytes(out, representation.data)
    _put_expiry(out, representation.expiry)


def _put_space_entry(out: bytearray, entry: SpaceEntry) -> None:
    _put_string(out, entry.pattern)
    _put_string(out, entry.endpoint)
    if entry.origin is None:
        out.append(0)
    else:
        out.append(1)
        _put_string(out, entry.origin)


def _put_trace_event(out: bytearray, event: TraceEvent) -> None:
    _put_string(out, event.target)
    _put_string(out, event.thread)
    _put_option_varint(out, event.started)
    _put_option_varint(out, event.ended)
    out.append(1 if event.cache_hit else 0)
    out += encode_varint(event.span)
    _put_option_varint(out, event.parent)
    if event.capability is None:
        out.append(0)
    else:
        out.append(1)
        out += encode_varint(len(event.capability))
        for scope in event.capability:
            _put_string(out, scope)
    out += encode_varint(len(event.notes))
    for key, value in event.notes:
        _put_string(out, key)
        _put_string(out, value)


def encode_call(call: Call) -> bytes:
    out = bytearray()
    if isinstance(call, Issue):
        out += encode_varint(0)
        _put_request(out, call.request)
    elif isinstance(call, IsCached):
        out += encode_varint(1)
        _put_request(out, call.request)
    elif isinstance(call, EntriesCall):
        out += encode_varint(2)
    elif isinstance(call, IssueAs):
        out += encode_varint(3)
        _put_request(out, call.request)
        _put_capability(out, call.capability)
    elif isinstance(call, IssueTraced):
        out += encode_varint(4)
        _put_request(out, call.request)
        _put_capability(out, call.capability)
        out += encode_varint(call.context.trace_id)
        _put_option_varint(out, call.context.parent_span)
    else:
        raise TypeError(f"not a Call: {call!r}")
    return bytes(out)


def encode_reply(reply: Reply) -> bytes:
    out = bytearray()
    if isinstance(reply, Resolved):
        out += encode_varint(0)
        _put_representation(out, reply.representation)
        out += encode_varint(int(reply.cache_status))
    elif isinstance(reply, Cached):
        out += encode_varint(1)
        out.append(1 if reply.cached else 0)
    elif isinstance(reply, EntriesReply):
        out += encode_varint(2)
        if reply.entries is None:
            out.append(0)
        else:
            out.append(1)
            out += encode_varint(len(reply.entries))
            for entry in reply.entries:
                _put_space_entry(out, entry)
    elif isinstance(reply, ErrorReply):
        out += encode_varint(3)
        _put_string(out, reply.message)
    elif isinstance(reply, ResolvedTraced):
        out += encode_varint(4)
        _put_representation(out, reply.representation)
        out += encode_varint(int(reply.cache_status))
        out += encode_varint(len(reply.events))
        for event in reply.events:
            _put_trace_event(out, event)
    else:
        raise TypeError(f"not a Reply: {reply!r}")
    return bytes(out)


# ---------------------------------------------------------------------------
# Decoding
# ---------------------------------------------------------------------------


def _version_mismatch(what: str, discriminant: int) -> ProtocolError:
    return ProtocolError(
        f"unknown {what} variant {discriminant}: this side speaks ikigai wire "
        f"protocol v{PROTOCOL_VERSION}; the peer is probably a different version "
        "(the wire carries no version handshake, so a mismatch surfaces here)"
    )


def _get_arg_ref(r: Reader) -> ArgRef:
    variant = r.varint(32)
    if variant == 0:
        return Reference(r.string())
    if variant == 1:
        return Inline(r.byte_string())
    if variant == 2:
        return Content(r.string())
    raise _version_mismatch("ArgRef", variant)


def _get_request(r: Reader) -> Request:
    verb_variant = r.varint(32)
    try:
        verb = Verb(verb_variant)
    except ValueError:
        raise _version_mismatch("Verb", verb_variant) from None
    target = r.string()
    args: dict[str, ArgRef] = {}
    for _ in range(r.varint()):
        name = r.string()
        args[name] = _get_arg_ref(r)
    return Request(verb, target, args)


def _get_capability(r: Reader) -> Capability:
    variant = r.varint(32)
    if variant == 0:
        return Capability.root()
    if variant == 1:
        return Capability.scoped(r.string() for _ in range(r.varint()))
    raise _version_mismatch("Capability", variant)


def _get_expiry(r: Reader) -> Expiry:
    variant = r.varint(32)
    if variant == 0:
        return Expiry.always()
    if variant == 1:
        return Expiry.at(r.varint())
    if variant == 2:
        return Expiry.never()
    raise _version_mismatch("Expiry", variant)


def _get_representation(r: Reader) -> Representation:
    media_type = r.string()
    params: dict[str, str] = {}
    for _ in range(r.varint()):
        key = r.string()
        params[key] = r.string()
    data = r.byte_string()
    expiry = _get_expiry(r)
    return Representation(data, media_type, params=params, expiry=expiry)


def _get_cache_status(r: Reader) -> CacheStatus:
    variant = r.varint(32)
    try:
        return CacheStatus(variant)
    except ValueError:
        raise _version_mismatch("CacheStatus", variant) from None


def _get_space_entry(r: Reader) -> SpaceEntry:
    pattern = r.string()
    endpoint = r.string()
    origin = r.string() if r.option() else None
    return SpaceEntry(pattern, endpoint, origin)


def _get_trace_event(r: Reader) -> TraceEvent:
    target = r.string()
    thread = r.string()
    started = r.varint() if r.option() else None
    ended = r.varint() if r.option() else None
    cache_hit = r.bool()
    span = r.varint()
    parent = r.varint() if r.option() else None
    capability = tuple(r.string() for _ in range(r.varint())) if r.option() else None
    notes = tuple((r.string(), r.string()) for _ in range(r.varint()))
    return TraceEvent(target, thread, started, ended, cache_hit, span, parent, capability, notes)


def decode_call(payload: bytes) -> Call:
    try:
        r = Reader(payload)
        variant = r.varint(32)
        call: Call
        if variant == 0:
            call = Issue(_get_request(r))
        elif variant == 1:
            call = IsCached(_get_request(r))
        elif variant == 2:
            call = EntriesCall()
        elif variant == 3:
            call = IssueAs(_get_request(r), _get_capability(r))
        elif variant == 4:
            request = _get_request(r)
            capability = _get_capability(r)
            trace_id = r.varint()
            parent_span = r.varint() if r.option() else None
            call = IssueTraced(request, capability, TraceContext(trace_id, parent_span))
        else:
            raise _version_mismatch("Call", variant)
        r.finish()
        return call
    except DecodeError as e:
        raise ProtocolError(f"undecodable Call (wire protocol v{PROTOCOL_VERSION}): {e}") from e


def decode_reply(payload: bytes) -> Reply:
    try:
        r = Reader(payload)
        variant = r.varint(32)
        reply: Reply
        if variant == 0:
            reply = Resolved(_get_representation(r), _get_cache_status(r))
        elif variant == 1:
            reply = Cached(r.bool())
        elif variant == 2:
            if r.option():
                entries = tuple(_get_space_entry(r) for _ in range(r.varint()))
                reply = EntriesReply(entries)
            else:
                reply = EntriesReply(None)
        elif variant == 3:
            reply = ErrorReply(r.string())
        elif variant == 4:
            representation = _get_representation(r)
            status = _get_cache_status(r)
            events = tuple(_get_trace_event(r) for _ in range(r.varint()))
            reply = ResolvedTraced(representation, status, events)
        else:
            raise _version_mismatch("Reply", variant)
        r.finish()
        return reply
    except DecodeError as e:
        raise ProtocolError(f"undecodable Reply (wire protocol v{PROTOCOL_VERSION}): {e}") from e


# ---------------------------------------------------------------------------
# Framing
# ---------------------------------------------------------------------------


def frame(payload: bytes) -> bytes:
    """A wire frame: u32 big-endian length + payload."""
    if len(payload) > MAX_FRAME:
        raise ProtocolError(f"message of {len(payload)} bytes exceeds the {MAX_FRAME}-byte limit")
    return struct.pack(">I", len(payload)) + payload


def write_frame(writer: BinaryIO, payload: bytes) -> None:
    writer.write(frame(payload))
    writer.flush()


def _read_exact(reader: BinaryIO, n: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < n:
        chunk = reader.read(n - len(chunks))
        if not chunk:
            raise EOFError(f"connection closed mid-frame ({len(chunks)}/{n} bytes)")
        chunks += chunk
    return bytes(chunks)


def read_frame(reader: BinaryIO) -> bytes:
    """Read one length-prefixed frame. Raises ``EOFError`` on a clean or
    mid-frame close, ``ProtocolError`` on an oversized length header (checked
    before allocating)."""
    header = reader.read(4)
    if not header:
        raise EOFError("connection closed")
    if len(header) < 4:
        header += _read_exact(reader, 4 - len(header))
    (length,) = struct.unpack(">I", header)
    if length > MAX_FRAME:
        raise ProtocolError(f"framed message of {length} bytes exceeds the {MAX_FRAME}-byte limit")
    return _read_exact(reader, length)


class HelloMode(IntEnum):
    """How the dialing side will address the connection — a hint for peers
    whose canonical IRIs carry a namespace prefix (this package's servers),
    which otherwise cannot know what form ``Entries`` should list."""

    VERBATIM = 0  # plain client / --connect / --override / --prefer
    ALIAS = 1  # an alias --mount: IRIs arrive prefix-stripped


@dataclass(frozen=True)
class Hello:
    """One side's hello: ``HELLO_MAGIC + u32 BE version + u8 mode``."""

    version: int
    mode: HelloMode = HelloMode.VERBATIM


def encode_hello(hello: Hello) -> bytes:
    return HELLO_MAGIC + struct.pack(">I", hello.version) + bytes([hello.mode])


def decode_hello(payload: bytes) -> Hello | None:
    """``None`` if the magic is absent (a legacy first frame). A missing mode
    byte defaults to verbatim; an UNKNOWN mode value also falls back to
    verbatim rather than failing — the mode is a hint, and a newer peer's new
    mode must not break an older reader. Trailing bytes are ignored: that is
    the extension mechanism."""
    if len(payload) < 8 or payload[:4] != HELLO_MAGIC:
        return None
    (version,) = struct.unpack(">I", payload[4:8])
    mode = HelloMode.ALIAS if len(payload) > 8 and payload[8] == 1 else HelloMode.VERBATIM
    return Hello(version, mode)


def decode_error_message(message: str) -> str:
    """Strip the ``endpoint error: `` prefix the server's Display rendering
    adds, the way the Rust wire clients do — the remainder is the endpoint's
    own message."""
    prefix = "endpoint error: "
    return message[len(prefix) :] if message.startswith(prefix) else message
