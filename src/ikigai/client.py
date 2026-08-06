"""The synchronous client: drive a running ikigai kernel over its Unix socket.

The front door for scripts and notebooks::

    import ikigai

    with ikigai.connect() as k:
        rep = k.source("urn:fn:toUpper", **{"in": "hi"})
        rep.text  # "HI"

Errors surface as exceptions carrying the server's error string (with the
``endpoint error: `` prefix stripped, the way the Rust wire clients do); the
wire does not yet carry a structured error taxonomy, so none is fabricated
here.
"""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path

from . import wire
from .wire import (
    ArgRef,
    Cached,
    Capability,
    EndpointError,
    EntriesCall,
    EntriesReply,
    ErrorReply,
    Inline,
    IsCached,
    Issue,
    IssueAs,
    IssueTraced,
    ProtocolError,
    Reply,
    Representation,
    Request,
    Resolved,
    ResolvedTraced,
    SpaceEntry,
    TraceContext,
    TraceEvent,
    Verb,
    WireError,
)

# Mirrors the Rust IPC client's DEFAULT_TIMEOUT: five minutes, because what it
# bounds is SILENCE from the server, and for a long resolution the silence IS
# the work (e.g. a large model loading before its first token). A genuinely
# gone server usually fails fast anyway (connection refused, EOF).
DEFAULT_TIMEOUT = 300.0


class ConnectionLost(WireError):
    """The kernel server is unreachable, hung past the timeout, or gone."""


def default_socket_path() -> Path:
    """The per-user socket path the Rust CLI serves on by default:
    ``<runtime-dir>/ikigai/kernel.sock`` where ``<runtime-dir>`` is
    ``$XDG_RUNTIME_DIR`` when set, else ``$TMPDIR``/``/tmp`` plus the uid."""
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        base = Path(runtime)
    else:
        tmp = Path(os.environ.get("TMPDIR") or "/tmp")
        base = tmp / f"ikigai-{os.getuid()}"
    return base / "ikigai" / "kernel.sock"


def coerce_arg(value) -> ArgRef:
    """An argument value as an ``ArgRef``: pass ``Reference``/``Inline``/
    ``Content`` through; encode ``str``/``bytes`` inline; render bools the
    way the REPL grammar does (``true``/``false``) and numbers via ``str``."""
    if isinstance(value, wire.Reference | Inline | wire.Content):
        return value
    if isinstance(value, bytes):
        return Inline(value)
    if isinstance(value, str):
        return Inline(value.encode("utf-8"))
    if isinstance(value, bool):
        return Inline(b"true" if value else b"false")
    if isinstance(value, int | float):
        return Inline(str(value).encode("utf-8"))
    raise TypeError(f"cannot send {type(value).__name__} as an argument (use str or bytes)")


def build_request(verb: Verb, iri: str, args: dict) -> Request:
    return Request(verb, iri, {name: coerce_arg(value) for name, value in args.items()})


class Client:
    """A connected kernel client. One request in flight at a time (the wire
    is strictly call/reply per connection, like the Rust ``IpcResolver``)."""

    def __init__(self, sock: socket.socket, *, capability: Capability | None = None):
        self._sock = sock
        self._file = sock.makefile("rwb")
        #: When set, requests go as ``Call::IssueAs`` under this capability
        #: (which the server clamps to its authenticated principal).
        self.capability = capability

    # -- transport ---------------------------------------------------------

    def _round_trip(self, call: wire.Call) -> Reply:
        try:
            wire.write_frame(self._file, wire.encode_call(call))
            return wire.decode_reply(wire.read_frame(self._file))
        except TimeoutError as e:
            raise ConnectionLost(
                "no response from the kernel server (it may be hung or gone)"
            ) from e
        except (BrokenPipeError, ConnectionError, EOFError) as e:
            raise ConnectionLost(f"the kernel server is unreachable: {e}") from e

    def _issue(self, request: Request) -> Representation:
        call = Issue(request) if self.capability is None else IssueAs(request, self.capability)
        reply = self._round_trip(call)
        if isinstance(reply, Resolved):
            representation = reply.representation
            representation.cache_status = reply.cache_status
            return representation
        if isinstance(reply, ErrorReply):
            raise EndpointError(wire.decode_error_message(reply.message))
        raise ProtocolError(f"unexpected reply to {type(call).__name__}: {reply!r}")

    # -- the five verbs ----------------------------------------------------

    def source(self, iri: str, **args) -> Representation:
        """Read a resource's representation."""
        return self._issue(build_request(Verb.SOURCE, iri, args))

    def sink(self, iri: str, value=None, **args) -> Representation:
        """Write to a resource. ``value`` rides as the ``content`` argument —
        the wire convention for the piped value (the Rust engine does the
        same for ``… | sink <iri>``)."""
        if value is not None:
            args["content"] = value
        return self._issue(build_request(Verb.SINK, iri, args))

    def exists(self, iri: str, **args) -> Representation:
        """Test for a resource's existence. The representation is whatever
        the bound endpoint answers (conventionally ``true``/``false`` text)."""
        return self._issue(build_request(Verb.EXISTS, iri, args))

    def delete(self, iri: str, **args) -> Representation:
        """Remove a resource."""
        return self._issue(build_request(Verb.DELETE, iri, args))

    def meta(self, iri: str, as_: str | None = None, **args) -> Representation:
        """Read a resource's self-description, rendered ``as_`` a media type
        (the server's default face is ``text/turtle``)."""
        if as_ is not None:
            args["as"] = as_
        return self._issue(build_request(Verb.META, iri, args))

    # -- sugar -------------------------------------------------------------

    def describe(self, iri: str) -> dict | None:
        """The structured self-description (Meta rendered as JSON), parsed —
        the same face the Rust engine uses to route named arguments. ``None``
        when the endpoint has none or the face isn't JSON-renderable."""
        try:
            representation = self.meta(iri, as_="application/json")
        except EndpointError:
            return None
        try:
            return json.loads(representation.data)
        except ValueError:
            return None

    def entries(self) -> list[SpaceEntry] | None:
        """The catalog: every binding the server's space enumerates (already
        capability-scoped by the server). ``None`` if the space does not
        support enumeration."""
        reply = self._round_trip(EntriesCall())
        if isinstance(reply, EntriesReply):
            return None if reply.entries is None else list(reply.entries)
        if isinstance(reply, ErrorReply):
            raise EndpointError(wire.decode_error_message(reply.message))
        raise ProtocolError(f"unexpected reply to Entries: {reply!r}")

    def is_cached(self, iri: str, **args) -> bool:
        """Whether sourcing ``iri`` with these args would be served from the
        server's cache. (The probe runs under the server's own authority;
        the wire does not carry the caller's capability for this call.)"""
        reply = self._round_trip(IsCached(build_request(Verb.SOURCE, iri, args)))
        return isinstance(reply, Cached) and reply.cached

    def source_traced(self, iri: str, **args) -> tuple[Representation, list[TraceEvent]]:
        """Source a resource AND record the resolution: returns the
        representation plus the server's trace spans (the execution tree —
        ``(span, parent)`` edges, per-node cache outcome and authority)."""
        call = IssueTraced(
            build_request(Verb.SOURCE, iri, args),
            self.capability or Capability.root(),
            TraceContext(trace_id=1, parent_span=None),
        )
        reply = self._round_trip(call)
        if isinstance(reply, ResolvedTraced):
            representation = reply.representation
            representation.cache_status = reply.cache_status
            return representation, list(reply.events)
        if isinstance(reply, ErrorReply):
            raise EndpointError(wire.decode_error_message(reply.message))
        raise ProtocolError(f"unexpected reply to IssueTraced: {reply!r}")

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        try:
            self._file.close()
        finally:
            self._sock.close()

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def connect(
    path: str | Path | None = None,
    *,
    capability: Capability | None = None,
    timeout: float | None = DEFAULT_TIMEOUT,
) -> Client:
    """Connect to a kernel server's Unix socket. ``path`` defaults to the
    same per-user location the Rust CLI uses; ``timeout`` bounds each socket
    read/write (``None`` blocks indefinitely)."""
    path = Path(path) if path is not None else default_socket_path()
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(str(path))
    except OSError as e:
        sock.close()
        raise ConnectionLost(
            f"cannot reach a kernel server at {path} ({e}); "
            "is `ikigai serve` (or the daemon) running?"
        ) from e
    return Client(sock, capability=capability)
