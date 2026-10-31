"""The ikigai IPC wire protocol: types, postcard codec, and framing.

Mirrors ``ikigai-wire`` (Rust) at ``PROTOCOL_VERSION`` 9. The codec is
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
mismatch is a clean error naming both sides instead of garbled postcard.

Since v7 the hello is REQUIRED (the one-version pre-v6 tolerances are gone:
no legacy client reconnect, no serving a first frame without the magic), and
the error TAXONOMY crosses the wire: ``Reply::ErrorTyped`` carries the same
variant the server saw (Unresolved / MissingArgument / InvalidArgument /
Endpoint / Denied / NotFound / Timeout / Unavailable), decoded here as a
typed exception hierarchy under :class:`EndpointError` — a remote denial
stays a permanent denial, a remote timeout stays TRANSIENT, and an HTTP face
can answer 403/404/400 instead of a blanket 502.

v8 appends ONE variant, ``Conflict`` (8): well-formed, authorized, the thing
exists, and its current state refuses the request — permanent, and an HTTP
409. It is BACKWARD COMPATIBLE, not a flag day: a v8 side accepts a v7 hello
(:data:`MIN_PROTOCOL_VERSION`), a connection remembers the version its peer
spoke, and a reply to a v7 peer downgrades ``Conflict(msg)`` to
``Endpoint("conflict: {msg}")`` — byte-identical to what a v7 peer received
before v8 existed (:func:`downgrade_error`). A v8 client that dials a v7
server redials once, offering 7 (:func:`connect_version`).
"""

from __future__ import annotations

import builtins
import struct
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import BinaryIO

from .postcard import DecodeError, Reader, encode_varint

# Bumped in lockstep with the Rust `ikigai_wire::PROTOCOL_VERSION`. v5 era:
# core 0.1.48 `TraceEvent.notes` changed the postcard layout of traced replies.
# v6 adds the hello exchange (version + mount mode at connection open).
# v7 adds Reply::ErrorTyped (the taxonomy crosses) and removes the v6
# tolerances (hello required; no legacy fallback either direction).
# v8 appends WireError::Conflict (variant 8) and is backward compatible with v7:
# see MIN_PROTOCOL_VERSION.
PROTOCOL_VERSION = 9

# The oldest peer this side still speaks to. A hello of MIN..=PROTOCOL_VERSION
# is accepted and the connection proceeds at the PEER's version (answered with
# that version, so a v7 peer sees exactly the hello it expects); below it is
# refused as before, naming both versions.
MIN_PROTOCOL_VERSION = 7

# The first wire version that carries WireError::Conflict as its own variant.
CONFLICT_SINCE = 8

# The magic prefix of a hello payload; a first frame without it is a legacy
# (<= v5) client and is refused since v7.
HELLO_MAGIC = b"IKWH"

# The largest framed message accepted (matches the Rust MAX_FRAME).
MAX_FRAME = 64 * 1024 * 1024


class WireError(Exception):
    """Base for everything this package raises on purpose."""


class ProtocolError(WireError):
    """A frame or message that violates the wire protocol.

    The hello exchange settles versions at connection open (and since v7 it
    is required — no legacy fallback path remains), so this means
    same-version layout corruption, a hello-refusing pre-v6 peer, or a
    version mismatch reported at connect. The message names the version this
    package speaks so every case stays diagnosable.
    """


class EndpointError(WireError):
    """A server-reported resolution failure, and the base of the typed
    hierarchy the v7 wire carries (``Reply::ErrorTyped``, a field-for-field
    mirror of ``ikigai_core::Error``). The base class itself is the
    ``Endpoint`` variant — a domain failure inside the endpoint — and is also
    what a flat v6 ``Reply::Error`` string and an unknown-future variant
    surface as.

    ``message`` (and ``str(e)``) is the endpoint's own message, with no
    ``endpoint error: `` / ``denied: `` rendering prefix — the TYPE carries
    the taxonomy, and this is what crosses the wire, so a prefix here would be
    doubled by the next peer's rendering. :meth:`display` is the other form:
    the error as ``ikigai_core::Error``'s ``Display`` spells it, for a person.
    ``transient`` is True only for Timeout/Unavailable: re-issuing might
    succeed, which is what retry/failover logic gates on (mirrors
    ``ikigai_core::Error::is_transient``); :meth:`failure_class` is the finer
    rule (``ikigai_core::Error::class``): ``RetryAfterError`` is NOT transient."""

    transient = False

    # ``ikigai_core::Error::kind``: the variant's stable word.
    kind = "endpoint"

    # What ``ikigai_core::Error``'s Display puts before ``message`` for this
    # variant. Empty for the three variants whose ``message`` is already built
    # in the Display's own words (Unresolved, MissingArgument, InvalidArgument).
    _display_prefix = "endpoint error: "

    def __init__(self, message: str):
        """``message`` is the endpoint's own message; the variant's rendering prefix
        (``_display_prefix``) is added only by :meth:`display`, never stored."""
        super().__init__(message)
        self.message = message

    def display(self) -> str:
        """The error as the Rust kernel's ``Display`` renders it — what a
        person should read. ``str(e)`` stays the bare message, which is what
        the wire carries.

        >>> EndpointError("boom").display()
        'endpoint error: boom'
        >>> DeniedError("needs urn:cap:x").display(), str(DeniedError("needs urn:cap:x"))
        ('denied: needs urn:cap:x', 'needs urn:cap:x')
        >>> InvalidArgumentError("n", "not a number").display()
        'invalid argument `n`: not a number'
        """
        return self._display_prefix + self.message

    def failure_class(self) -> str:
        """Who may repeat this failure, and when (``ikigai_core::Error::class``): one
        of ``transient``, ``retry-after``, ``over-budget``, ``denied``, ``refused``,
        ``permanent``, ``unknown``. A pure function of the variant. (Not ``class``:
        that is a keyword here.)

        >>> EndpointError("boom").failure_class(), DeniedError("x").failure_class()
        ('unknown', 'denied')
        >>> TimeoutError("slow").failure_class(), NotFoundError("x").failure_class()
        ('transient', 'permanent')
        """
        return self._failure_class

    _failure_class = "unknown"

    # Structural equality (type + args), so replies carrying errors compare
    # by value like every other wire type — exceptions default to identity,
    # which would make ErrorTypedReply round-trip comparisons meaningless.
    def _identity(self) -> tuple:
        return self.args

    def __eq__(self, other) -> bool:
        """Same variant and same args — value equality, as :meth:`__hash__` matches."""
        return type(self) is type(other) and self._identity() == other._identity()

    def __hash__(self) -> int:
        """Hash of the variant and its args, so an error is usable as a dict key."""
        return hash((type(self), self._identity()))


class UnresolvedError(EndpointError):
    """``WireError::Unresolved``: the KERNEL found no binding for the target
    (contrast :class:`NotFoundError` — a bound endpoint reporting the thing
    it fronts absent). Permanent."""

    def __init__(self, iri: str):
        """``iri`` is the unresolved target, kept as :attr:`iri`; ``message`` is the
        ``no endpoint resolved for {iri}`` form the wire carries."""
        super().__init__(f"no endpoint resolved for {iri}")
        self.iri = iri

    _display_prefix = ""
    kind = "unresolved"
    _failure_class = "permanent"


class MissingArgumentError(EndpointError):
    """``WireError::MissingArgument``: a required argument was absent.
    Permanent."""

    def __init__(self, name: str):
        """``name`` is the absent argument, kept as :attr:`name`."""
        super().__init__(f"missing required argument `{name}`")
        self.name = name

    _display_prefix = ""
    kind = "missing-argument"
    _failure_class = "permanent"


class InvalidArgumentError(EndpointError):
    """``WireError::InvalidArgument``: an argument was present but unusable.
    Permanent."""

    def __init__(self, name: str, detail: str):
        """``name`` and ``detail`` are kept as :attr:`name`/:attr:`detail`; the wire
        carries ``invalid argument `{name}`: {detail}``."""
        super().__init__(f"invalid argument `{name}`: {detail}")
        self.name = name
        self.detail = detail

    _display_prefix = ""
    kind = "invalid-argument"
    _failure_class = "permanent"


class DeniedError(EndpointError):
    """``WireError::Denied``: the capability did not authorize it — a
    PERMANENT denial (a failover must not paper over it; an HTTP face says
    403)."""

    _display_prefix = "denied: "
    kind = "denied"
    _failure_class = "denied"


class NotFoundError(EndpointError):
    """``WireError::NotFound``: a bound endpoint reports the fronted thing
    absent — a permanent 404-equivalent."""

    _display_prefix = "not found: "
    kind = "not-found"
    _failure_class = "permanent"


class TimeoutError(EndpointError, builtins.TimeoutError):
    """``WireError::Timeout``: the operation exceeded its time budget on the
    SERVER side. TRANSIENT — re-issuing an idempotent verb may succeed. Also
    a ``builtins.TimeoutError`` (the asyncio precedent for shadowing the
    name), so generic timeout handling catches it — but note a LOCAL read
    deadline surfaces as ``client.ConnectionLost``, not this."""

    transient = True
    _display_prefix = "timeout: "
    kind = "timeout"
    _failure_class = "transient"


class UnavailableError(EndpointError):
    """``WireError::Unavailable``: a dependency or transport is down /
    refused / unreachable. TRANSIENT, like :class:`TimeoutError`."""

    transient = True
    _display_prefix = "unavailable: "
    kind = "unavailable"
    _failure_class = "transient"


class ConflictError(EndpointError):
    """``WireError::Conflict`` (wire v8, variant 8): the request is
    well-formed and authorized and the thing exists, but its CURRENT STATE
    refuses it — a move onto an occupied square, a create over an existing
    row. PERMANENT (re-issuing the same request meets the same state, so a
    retry overlay must not retry it); an HTTP face says 409. Contrast a
    412, which answers a precondition the CALLER stated (``If-Match``).

    A v7 peer cannot receive this variant: a reply to one carries
    ``EndpointError("conflict: {message}")`` instead."""

    _display_prefix = "conflict: "
    kind = "conflict"
    _failure_class = "permanent"


class DepthExceededError(EndpointError):
    """``WireError::DepthExceeded`` (wire v9, tag 9): a sub-request was refused
    because it would nest deeper than the kernel's budget. PERMANENT (the same
    request takes the same path). ``depth`` is the nesting depth the refused
    request would have run at, ``target`` the name it asked for; ``message`` is
    the ``nesting budget exceeded: …`` sentence core's ``Display`` writes."""

    kind = "depth-exceeded"
    _failure_class = "permanent"
    _display_prefix = ""

    def __init__(self, depth: int, target: str):
        """``depth`` and ``target`` are kept as attributes; ``message`` is core's sentence."""
        super().__init__(
            f"nesting budget exceeded: sub-request for {target} would run at depth {depth}"
        )
        self.depth = depth
        self.target = target

    def _identity(self) -> tuple:
        return (self.depth, self.target)


class RetryAfterError(EndpointError):
    """``WireError::RetryAfter`` (wire v9, tag 10): the same request may succeed, but
    NOT before a stated time (a rate limit, a saturated store, an open circuit).
    ``delay_ms`` is how long to wait in whole milliseconds, rounded up, or ``None``
    for "not now" without a time; ``detail`` says why. Class ``retry-after``, and
    deliberately NOT :attr:`transient`: code that gates on ``transient`` treats it as
    permanent, the safe reading of a rate limit. An HTTP edge answers 429 with
    ``Retry-After`` (:meth:`retry_after_seconds`)."""

    kind = "retry-after"
    _failure_class = "retry-after"

    def __init__(self, delay_ms: int | None, detail: str):
        """``delay_ms`` (whole milliseconds, or ``None``) and ``detail``, kept as attributes."""
        super().__init__(detail)
        self.delay_ms = delay_ms
        self.detail = detail

    def retry_after_seconds(self) -> int | None:
        """The ``Retry-After`` header's delay-seconds: rounded up and never 0; ``None``
        without a delay (no header is sent).

        >>> [RetryAfterError(ms, "x").retry_after_seconds() for ms in (None, 0, 2, 1500, 30000)]
        [None, 1, 1, 2, 30]
        """
        if self.delay_ms is None:
            return None
        return max(1, -(-self.delay_ms // 1000))

    def display(self) -> str:
        """``retry later: {detail}`` without a delay, else ``retry after {ms} ms: {detail}``."""
        if self.delay_ms is None:
            return f"retry later: {self.detail}"
        return f"retry after {self.delay_ms} ms: {self.detail}"

    def _identity(self) -> tuple:
        return (self.delay_ms, self.detail)


class OverBudgetError(EndpointError):
    """``WireError::OverBudget`` (wire v9, tag 11): the work ran past a budget the caller
    set (a deadline, a quota). Class ``over-budget``: repeating it with the same budget
    meets the same wall; an HTTP edge says 422."""

    kind = "over-budget"
    _failure_class = "over-budget"
    _display_prefix = "over budget: "

    def __init__(self, detail: str):
        """``detail`` is the message, kept as :attr:`detail` too."""
        super().__init__(detail)
        self.detail = detail


class RefusedError(EndpointError):
    """``WireError::Refused`` (wire v9, tag 12): an upstream understood the request and
    declined it (an origin that answered 401). Class ``refused``; an HTTP edge says 502."""

    kind = "refused"
    _failure_class = "refused"
    _display_prefix = "refused: "


class UnsupportedError(EndpointError):
    """``WireError::Unsupported`` (wire v9, tag 13): the target does not answer ``verb``
    (or, with ``verb`` ``None``, the host lacks a facility). Permanent; an HTTP edge says
    405 with a verb and 501 without."""

    kind = "unsupported"
    _failure_class = "permanent"
    _display_prefix = "unsupported: "

    def __init__(self, detail: str, verb: Verb | None = None):
        """``detail`` says what is unsupported; ``verb`` is the verb, ``None`` for a facility."""
        super().__init__(detail)
        self.detail = detail
        self.verb = verb

    def _identity(self) -> tuple:
        return (self.detail, self.verb)


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
        """Root authority: ``scopes is None`` (the wire's ``Kind::Root``)."""
        return cls(None)

    @classmethod
    def scoped(cls, scopes) -> Capability:
        """Authority limited to ``scopes`` (``urn:cap:`` strings), sorted on the wire."""
        return cls(frozenset(scopes))

    @property
    def is_root(self) -> bool:
        """True when this capability is root, i.e. carries no scope limit."""
        return self.scopes is None

    def allows(self, scope: str) -> bool:
        """Whether this grants ``scope`` matched exactly: core's ``allows``, see
        :func:`ikigai.capability.allows` (an exclusion is never granted)."""
        from .capability import allows

        return allows(self, scope)

    def satisfies(self, scope: str) -> bool:
        """Whether this satisfies one DECLARED scope as the kernel's floor decides it
        (a trailing ``*`` is "holds any scope under this prefix"): core's
        ``satisfies``, see :func:`ikigai.capability.satisfies`."""
        from .capability import satisfies

        return satisfies(self, scope)


@dataclass(frozen=True)
class Expiry:
    """``ikigai_core::Expiry``: Always (variant 0) | At(Time) (1) | Never (2).

    ``Time`` is a newtype over u64 milliseconds since the Unix epoch."""

    kind: str  # "always" | "at" | "never"
    at_millis: int | None = None

    @classmethod
    def always(cls) -> Expiry:
        """No expiry claimed: the result may be cached and re-validated freely."""
        return cls("always")

    @classmethod
    def never(cls) -> Expiry:
        """The result is a pure function of its inputs — cacheable indefinitely."""
        return cls("never")

    @classmethod
    def at(cls, millis: int) -> Expiry:
        """Expire at ``millis`` since the Unix epoch (the wire's ``Expiry::At``)."""
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
        """``media_type`` may carry parameters (``text/plain;charset=utf-8``), which are
        split out and merged under ``params``; ``expiry`` defaults to ``Expiry.always()``.
        ``cache_status`` is stamped by the client on receipt, not part of the value."""
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
        """The data decoded as UTF-8; raises ``UnicodeDecodeError`` on other bytes."""
        return self.data.decode("utf-8")

    def __eq__(self, other) -> bool:
        """Value equality over data, media type, params and expiry — but NOT
        ``cache_status``, which is stamped on receipt and is not part of the value."""
        return (
            isinstance(other, Representation)
            and self.data == other.data
            and self.base_media_type == other.base_media_type
            and self.params == other.params
            and self.expiry == other.expiry
        )

    def __repr__(self) -> str:
        """``Representation('media/type', N bytes, kind)`` — no body, so a log line
        stays short."""
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


@dataclass(frozen=True)
class IssueWith:
    """``ikigai_wire::IssueWith`` (wire v9): what a run carries across a hop, three
    optional fields. ``time_left_ms`` is TIME LEFT in whole milliseconds, never an
    absolute instant: the server adds it to its OWN now, so the two clocks never meet."""

    trace: TraceContext | None = None
    retries: int | None = None  # the retry pool remaining (a u32)
    time_left_ms: int | None = None


@dataclass(frozen=True)
class IssueWithCall:
    """``Call::IssueWith`` (variant 5, wire v9): where ``IssueTraced`` puts its
    ``TraceContext``, this puts an :class:`IssueWith`."""

    request: Request
    capability: Capability
    context: IssueWith


Call = Issue | IsCached | EntriesCall | IssueAs | IssueTraced | IssueWithCall

#: The first wire version that has ``Call::IssueWith`` and error tags 9-13.
ISSUE_WITH_SINCE = 9


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
    """``Reply::Error`` (variant 3): the server's rendered error string.
    Since v7 servers answer with :class:`ErrorTypedReply` instead; this
    variant remains decodable (discriminants are append-only) but is no
    longer sent."""

    message: str


@dataclass(frozen=True)
class ResolvedTraced:
    """``Reply::ResolvedTraced`` (variant 4)."""

    representation: Representation
    cache_status: CacheStatus
    events: tuple[TraceEvent, ...]


@dataclass(frozen=True)
class ErrorTypedReply:
    """``Reply::ErrorTyped`` (variant 5, wire v7): a failure with its
    taxonomy intact. ``error`` is the typed exception the client raises —
    the same ``ikigai_core::Error`` variant the server saw, rebuilt (the
    Rust ``WireError`` enum, variants 0-8 in declaration order; 8,
    ``Conflict``, since v8)."""

    error: EndpointError


Reply = Resolved | Cached | EntriesReply | ErrorReply | ResolvedTraced | ErrorTypedReply


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


def _put_wire_error(out: bytearray, error: EndpointError) -> None:
    """The Rust ``WireError`` enum: variant index in declaration order, then
    the payload. Subclass checks come first — the base class IS variant 3."""
    if isinstance(error, UnresolvedError):
        out += encode_varint(0)
        _put_string(out, error.iri)
    elif isinstance(error, MissingArgumentError):
        out += encode_varint(1)
        _put_string(out, error.name)
    elif isinstance(error, InvalidArgumentError):
        out += encode_varint(2)
        _put_string(out, error.name)
        _put_string(out, error.detail)
    elif isinstance(error, DeniedError):
        out += encode_varint(4)
        _put_string(out, error.message)
    elif isinstance(error, NotFoundError):
        out += encode_varint(5)
        _put_string(out, error.message)
    elif isinstance(error, TimeoutError):
        out += encode_varint(6)
        _put_string(out, error.message)
    elif isinstance(error, UnavailableError):
        out += encode_varint(7)
        _put_string(out, error.message)
    elif isinstance(error, ConflictError):
        out += encode_varint(8)
        _put_string(out, error.message)
    elif isinstance(error, DepthExceededError):
        out += encode_varint(9)
        out += encode_varint(error.depth)
        _put_string(out, error.target)
    elif isinstance(error, RetryAfterError):
        out += encode_varint(10)
        _put_option_varint(out, error.delay_ms)
        _put_string(out, error.detail)
    elif isinstance(error, OverBudgetError):
        out += encode_varint(11)
        _put_string(out, error.detail)
    elif isinstance(error, RefusedError):
        out += encode_varint(12)
        _put_string(out, error.message)
    elif isinstance(error, UnsupportedError):
        out += encode_varint(13)
        _put_option_varint(out, None if error.verb is None else int(error.verb))
        _put_string(out, error.detail)
    elif isinstance(error, EndpointError):
        out += encode_varint(3)  # WireError::Endpoint
        _put_string(out, error.message)
    else:
        raise TypeError(f"not an EndpointError: {error!r}")


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


def _put_issue_with(out: bytearray, context: IssueWith) -> None:
    if context.trace is None:
        out.append(0)
    else:
        out.append(1)
        out += encode_varint(context.trace.trace_id)
        _put_option_varint(out, context.trace.parent_span)
    _put_option_varint(out, context.retries)
    _put_option_varint(out, context.time_left_ms)


def encode_issue_with(context: IssueWith) -> bytes:
    """The ``IssueWith`` struct alone, postcard-encoded (the cross-language vectors)."""
    out = bytearray()
    _put_issue_with(out, context)
    return bytes(out)


def encode_call(call: Call) -> bytes:
    """A Call as one postcard payload: the variant's index, then its fields in Rust
    declaration order (maps and sets in ``BTreeMap``/``BTreeSet`` key order). Raises
    ``TypeError`` for anything that is not a Call — the encoder never guesses."""
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
    elif isinstance(call, IssueWithCall):
        out += encode_varint(5)
        _put_request(out, call.request)
        _put_capability(out, call.capability)
        _put_issue_with(out, call.context)
    else:
        raise TypeError(f"not a Call: {call!r}")
    return bytes(out)


_V9_ERRORS = (DepthExceededError, RetryAfterError, OverBudgetError, RefusedError, UnsupportedError)


def downgrade_error(error: EndpointError, peer_version: int) -> EndpointError:
    """``error`` as a peer speaking ``peer_version`` can receive it. A v7
    peer has no ``Conflict`` variant, so it gets ``Endpoint("conflict:
    {msg}")`` — the Rust core's ``Display`` of the error, which is exactly
    what crossed untyped before v8. Every other variant is unchanged."""
    if isinstance(error, ConflictError) and peer_version < CONFLICT_SINCE:
        return EndpointError(f"conflict: {error.message}")
    if isinstance(error, _V9_ERRORS) and peer_version < ISSUE_WITH_SINCE:
        # A v7/v8 peer has no tags 9-13: it gets what it has always received for them.
        return EndpointError(error.display())
    return error


def encode_reply(reply: Reply, *, peer_version: int = PROTOCOL_VERSION) -> bytes:
    """Encode ``reply`` for a peer that spoke ``peer_version`` in its hello:
    a variant that peer's version cannot decode is downgraded first
    (:func:`downgrade_error`), so a v8 side never sends variant 8 to a v7
    peer. Defaults to this side's own version."""
    if isinstance(reply, ErrorTypedReply):
        downgraded = downgrade_error(reply.error, peer_version)
        if downgraded is not reply.error:
            reply = ErrorTypedReply(downgraded)
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
    elif isinstance(reply, ErrorTypedReply):
        out += encode_varint(5)
        _put_wire_error(out, reply.error)
    else:
        raise TypeError(f"not a Reply: {reply!r}")
    return bytes(out)


# ---------------------------------------------------------------------------
# Decoding
# ---------------------------------------------------------------------------


def _version_mismatch(what: str, discriminant: int) -> ProtocolError:
    return ProtocolError(
        f"unknown {what} variant {discriminant}: this side speaks ikigai wire "
        f"protocol v{PROTOCOL_VERSION} (and down to v{MIN_PROTOCOL_VERSION}), and the "
        "required hello exchange rules out a peer outside that range, so this is a "
        "corrupt frame"
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
        if name in args:
            # A map holds one value per key, and a decoder that keeps the last (or the
            # first) answers a different call than the peer that sent it meant. Refuse it.
            raise DecodeError(f"duplicate argument key `{name}`")
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


def _get_wire_error(r: Reader) -> EndpointError:
    variant = r.varint(32)
    if variant == 0:
        return UnresolvedError(r.string())
    if variant == 1:
        return MissingArgumentError(r.string())
    if variant == 2:
        return InvalidArgumentError(r.string(), r.string())
    if variant == 3:
        return EndpointError(r.string())
    if variant == 4:
        return DeniedError(r.string())
    if variant == 5:
        return NotFoundError(r.string())
    if variant == 6:
        return TimeoutError(r.string())
    if variant == 7:
        return UnavailableError(r.string())
    if variant == 8:
        return ConflictError(r.string())
    # Wire v9 tags (RECEIVE only; the layouts follow core's field order).
    if variant == 9:
        return DepthExceededError(r.varint(32), r.string())
    if variant == 10:
        delay_ms = r.varint(64) if r.option() else None
        return RetryAfterError(delay_ms, r.string())
    if variant == 11:
        return OverBudgetError(r.string())
    if variant == 12:
        return RefusedError(r.string())
    if variant == 13:
        verb = None
        if r.option():
            try:
                verb = Verb(r.varint(32))
            except ValueError as e:
                raise DecodeError(f"unknown verb in Unsupported: {e}") from e
        return UnsupportedError(r.string(), verb)
    # A NEWER peer's taxonomy addition (the enum is append-only, and additions
    # are wire-version events, so the hello should have refused this peer —
    # belt and braces). The payload layout is unknowable, so consume the rest
    # of the frame and degrade to the base class, loudly named, rather than
    # failing the whole reply.
    r.remainder()
    return EndpointError(
        f"unknown wire error variant {variant} from a newer peer "
        f"(this side speaks wire protocol v{PROTOCOL_VERSION}; "
        "the failure's message could not be decoded)"
    )


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


def _get_issue_with(r: Reader) -> IssueWith:
    trace = None
    if r.option():
        trace_id = r.varint()
        trace = TraceContext(trace_id, r.varint() if r.option() else None)
    retries = r.varint(32) if r.option() else None
    time_left_ms = r.varint() if r.option() else None
    return IssueWith(trace, retries, time_left_ms)


def decode_issue_with(payload: bytes) -> IssueWith:
    """Decode the ``IssueWith`` struct alone, asserting it is consumed exactly."""
    try:
        r = Reader(payload)
        context = _get_issue_with(r)
        r.finish()
        return context
    except DecodeError as e:
        raise ProtocolError(f"undecodable IssueWith: {e}") from e


def build_issue_call(
    request: Request,
    capability: Capability | None,
    *,
    server_version: int | None,
    retries: int | None = None,
    deadline: float | None = None,
    trace: TraceContext | None = None,
) -> Call:
    """The Call a client sends for ``request``. ``IssueWith`` goes only to a peer that
    negotiated v9 AND only when the run carries a retry pool or a ``deadline`` (a
    ``time.monotonic()`` instant); otherwise the exact pre-v9 ``Issue``/``IssueAs``
    (or ``IssueTraced`` when ``trace`` is given). The deadline crosses as TIME LEFT
    in whole ms, computed now."""
    if (
        (retries is not None or deadline is not None)
        and server_version is not None
        and server_version >= ISSUE_WITH_SINCE
    ):
        time_left = None if deadline is None else max(0, int((deadline - time.monotonic()) * 1000))
        return IssueWithCall(
            request, capability or Capability.root(), IssueWith(trace, retries, time_left)
        )
    if trace is not None:
        return IssueTraced(request, capability or Capability.root(), trace)
    return Issue(request) if capability is None else IssueAs(request, capability)


def downgrade_call(call: Call, peer_version: int | None) -> Call:
    """``call`` as a peer speaking ``peer_version`` can decode it: an ``IssueWith`` for a
    pre-v9 (or unknown) peer becomes the ``IssueTraced``/``IssueAs`` it extends, dropping
    the retry pool and the time left, which that peer has no field for."""
    if isinstance(call, IssueWithCall) and (peer_version or 0) < ISSUE_WITH_SINCE:
        if call.context.trace is not None:
            return IssueTraced(call.request, call.capability, call.context.trace)
        return IssueAs(call.request, call.capability)
    return call


def decode_call(payload: bytes) -> Call:
    """Decode one postcard Call payload, asserting it is consumed exactly. A truncated
    or unknown-variant payload raises :class:`ProtocolError` (never ``DecodeError``), so
    a caller has one exception type to catch for a bad frame."""
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
        elif variant == 5:
            request = _get_request(r)
            capability = _get_capability(r)
            call = IssueWithCall(request, capability, _get_issue_with(r))
        else:
            raise _version_mismatch("Call", variant)
        r.finish()
        return call
    except DecodeError as e:
        raise ProtocolError(f"undecodable Call (wire protocol v{PROTOCOL_VERSION}): {e}") from e


def decode_reply(payload: bytes) -> Reply:
    """Decode one postcard Reply payload, asserting it is consumed exactly. An
    unknown-future error variant degrades to a named :class:`EndpointError` (see
    :func:`_get_wire_error`); a corrupt frame raises :class:`ProtocolError`."""
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
        elif variant == 5:
            reply = ErrorTypedReply(_get_wire_error(r))
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
    """Write one :func:`frame` and flush, so the peer is not left waiting on a buffer."""
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

    VERBATIM = 0  # plain client / --connect / --override / --prefer / a deeper alias --mount
    ALIAS = 1  # a first-segment alias --mount: IRIs arrive prefix-stripped


@dataclass(frozen=True)
class Hello:
    """One side's hello: ``HELLO_MAGIC + u32 BE version + u8 mode``."""

    version: int
    mode: HelloMode = HelloMode.VERBATIM


def accepted_version(offered: int) -> int | None:
    """The version a connection proceeds at when a peer's hello offers
    ``offered``, or ``None`` if this side refuses it. ``MIN_PROTOCOL_VERSION``
    through ``PROTOCOL_VERSION`` is accepted AT THE PEER'S VERSION — the
    server answers the hello with it, so a v7 peer sees exactly the v7 hello
    it requires. Anything else is refused: a server answers with its own
    version and closes, and the dialing side renders the mismatch (a NEWER
    dialer that also negotiates redials at the version it was answered
    with — see :func:`connect_version`)."""
    if MIN_PROTOCOL_VERSION <= offered <= PROTOCOL_VERSION:
        return offered
    return None


def connect_version(offered: int, answered: int) -> str | int:
    """What a dialing side does with the version a server ``answered`` to
    its hello offering ``offered``:

    - ``"proceed"`` — the server accepted the offer (it answered with it);
    - an ``int`` — the server is OLDER but within reach: it answered with its
      own version and (being older) closed the connection, so redial once
      offering that version. This is how a v8 client talks to a v7 server
      (0.1.29 answers ``7`` and hangs up on an ``8``);
    - ``"refuse"`` — out of reach either way; name both versions."""
    if answered == offered:
        return "proceed"
    if answered < offered and accepted_version(answered) == answered:
        return answered
    return "refuse"


def encode_hello(hello: Hello) -> bytes:
    """A hello payload: the magic, the version as ``u32`` big-endian, then the mode
    byte. Deliberately not postcard — the codec whose version is being negotiated must
    not be needed to negotiate it."""
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
