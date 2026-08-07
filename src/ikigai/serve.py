"""Serve Python functions as ikigai resources over a Unix socket.

The peer-module seed: a Rust host mounts this server
(``ikigai --mount urn:py:=<socket>``) and the functions join its resolution
space — listed in the catalog with their origin, named-arg routed via their
declared ArgSpecs, invoked over the wire::

    from ikigai import endpoint, serve

    @endpoint("urn:py:hello", summary="Greet someone",
              args=[{"name": "who", "required": True,
                     "class": "http://www.w3.org/2001/XMLSchema#string"}])
    def hello(who: str) -> str:
        return f"Hello, {who}!"

    serve([hello], "/tmp/py.sock")   # blocks

**Alias mounts strip the prefix.** ``--mount urn:py:=<socket>`` rewrites
``urn:py:hello`` to ``urn:hello`` before forwarding, and re-prefixes catalog
patterns coming back. This server therefore answers BOTH the declared IRI
(``urn:py:hello`` — for ``--override`` mounts and direct ``--connect``
clients) and its alias-stripped form (``urn:hello``), and by default lists
the alias-stripped form in ``entries`` so an alias mount's catalog reads
correctly. Pass ``strip_alias=False`` when serving for an ``--override``
mount (patterns then list verbatim; an alias mount would double-prefix them).

**Security posture**: the socket is ``0600`` in a ``0700`` directory and
peers are refused unless their kernel-verified UID matches the server's —
the same transport trust as the Rust IPC server. A capability carried on
``IssueAs``/``IssueTraced`` is accepted but not enforced per-scope
(capability-on-the-wire for IPC is a known TODO on the Rust side too).
"""

from __future__ import annotations

import json
import socket
import struct
import sys
import threading
import time
from pathlib import Path

from . import wire
from .wire import (
    Cached,
    CacheStatus,
    EntriesCall,
    EntriesReply,
    ErrorReply,
    Expiry,
    Inline,
    IsCached,
    Issue,
    IssueAs,
    IssueTraced,
    Reply,
    Representation,
    Request,
    Resolved,
    ResolvedTraced,
    SpaceEntry,
    TraceEvent,
    Verb,
)

VOCAB_NS = "https://ikigai-rs.dev/ns#"


class ArgSpec:
    """One named input, mirroring ``ikigai_core::ArgSpec``. The describe face
    built from these is what the host engine routes named arguments by — the
    names and required/optional flags are load-bearing, not decoration."""

    def __init__(
        self,
        name: str,
        *,
        summary: str = "",
        required: bool = True,
        cls: str | None = None,
        default: str | None = None,
        one_of: list[str] | None = None,
    ):
        self.name = name
        self.summary = summary
        # A declared default implies the argument is optional (as in Rust).
        self.required = required if default is None else False
        self.cls = cls
        self.default = default
        self.one_of = list(one_of or [])

    @classmethod
    def of(cls, spec) -> ArgSpec:
        if isinstance(spec, ArgSpec):
            return spec
        if isinstance(spec, str):
            return cls(spec)
        if isinstance(spec, dict):
            return cls(
                spec["name"],
                summary=spec.get("summary", ""),
                required=spec.get("required", True),
                cls=spec.get("class"),
                default=spec.get("default"),
                one_of=spec.get("one_of"),
            )
        raise TypeError(f"not an ArgSpec: {spec!r}")

    def to_json(self) -> dict:
        """The serde shape of ``ikigai_core::ArgSpec`` (fields with
        ``skip_serializing_if`` omitted when unset, like the Rust side)."""
        out = {
            "name": self.name,
            "summary": self.summary,
            "required": self.required,
            "source": "argument",
        }
        if self.cls is not None:
            out["class"] = self.cls
        if self.default is not None:
            out["default"] = self.default
        if self.one_of:
            out["one_of"] = self.one_of
        return out


class EndpointDef:
    """A served endpoint: a handler plus its self-description."""

    def __init__(
        self,
        handler,
        iri: str,
        *,
        id: str | None = None,
        title: str = "",
        summary: str = "",
        args: list | None = None,
        output: str = "text/plain;charset=utf-8",
        cacheable: bool = False,
        requires: list[str] | None = None,
    ):
        if not iri.startswith("urn:"):
            raise ValueError(f"endpoint IRI must be a urn: ({iri!r})")
        self.handler = handler
        self.iri = iri
        self.id = id or handler.__name__
        self.title = title
        self.summary = summary or (handler.__doc__ or "").strip().split("\n")[0]
        self.args = [ArgSpec.of(a) for a in (args or [])]
        self.output = output
        self.cacheable = cacheable
        self.requires = list(requires or [])

    @property
    def alias_iri(self) -> str | None:
        """The alias-stripped form an alias mount forwards: ``urn:py:hello``
        arrives as ``urn:hello`` after ``--mount urn:py:=…`` strips its
        prefix. ``None`` when the IRI has no namespace segment to strip."""
        parts = self.iri.split(":", 2)
        if len(parts) == 3:
            return f"urn:{parts[2]}"
        return None

    # -- the Meta faces ----------------------------------------------------

    def description_json(self) -> dict:
        """The serde shape of ``ikigai_core::Description`` — the face the
        host's engine parses to route named arguments over a mount."""
        out = {
            "id": self.id,
            "title": self.title,
            "summary": self.summary,
            "verbs": ["Source", "Meta"],
            "inputs": [a.to_json() for a in self.args],
            "outputs": [self.output],
        }
        if self.requires:
            out["requires"] = self.requires
        return out

    def description_text(self) -> str:
        """The human face (mirrors ``ikigai_vocab::to_text``)."""
        s = f"{self.id} — {self.title}\n"
        if self.summary:
            s += f"{self.summary}\n"
        s += "verbs: Source, Meta\n"
        for arg in self.args:
            opt = "" if arg.required else " (optional)"
            s += f"  input {arg.name} [argument]{opt}: {arg.summary}\n"
        s += f"outputs: {self.output}\n"
        return s

    def description_turtle(self) -> str:
        """The graph face (mirrors ``ikigai_vocab::to_turtle``): skolemized
        node IRIs, no blank nodes, the shared ``ik:`` vocabulary."""

        def lit(s: str) -> str:
            return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'

        def cap_term(scope: str) -> str:
            if scope.startswith(("urn:", "http://", "https://")):
                return f"<{scope}>"
            return lit(scope)

        endpoint_iri = f"urn:ikigai:endpoint:{self.id}"
        preds = [
            "a ik:Endpoint",
            f"ik:id {lit(self.id)}",
        ]
        if self.title:
            preds.append(f"ik:title {lit(self.title)}")
        if self.summary:
            preds.append(f"ik:summary {lit(self.summary)}")
        preds.append('ik:verb "Source", "Meta"')
        preds.append(f"ik:output {lit(self.output)}")
        if self.requires:
            preds.append("ik:requires " + ", ".join(cap_term(c) for c in self.requires))

        extra_nodes: list[str] = []

        def input_predicates(arg: ArgSpec) -> str:
            node = (
                f"ik:inputName {lit(arg.name)} ;\n"
                f'    ik:source "argument" ;\n'
                f"    ik:required {'true' if arg.required else 'false'}"
            )
            if arg.summary:
                node += f" ;\n    ik:summary {lit(arg.summary)}"
            if arg.cls is not None:
                node += f" ;\n    ik:class <{arg.cls}>"
            if arg.default is not None:
                node += f" ;\n    ik:default {lit(arg.default)}"
            for value in arg.one_of:
                node += f" ;\n    ik:oneOf {lit(value)}"
            return node

        for arg in self.args:
            node_iri = f"{endpoint_iri}:input:{arg.name}"
            preds.append(f"ik:input <{node_iri}>")
            extra_nodes.append(f"<{node_iri}> {input_predicates(arg)} .")

        # The synthesized Source action (the flat form's per-verb view),
        # referencing the same input nodes.
        action_iri = f"{endpoint_iri}:action:source"
        preds.append(f"ik:action <{action_iri}>")
        action_preds = ["a ik:Action", 'ik:verb "Source"']
        action_preds.append(f"ik:output {lit(self.output)}")
        for cap in self.requires:
            action_preds.append(f"ik:requires {cap_term(cap)}")
        for arg in self.args:
            action_preds.append(f"ik:input <{endpoint_iri}:input:{arg.name}>")
        extra_nodes.append(f"<{action_iri}> " + " ;\n    ".join(action_preds) + " .")

        ttl = f"@prefix ik: <{VOCAB_NS}> .\n\n<{endpoint_iri}> " + " ;\n    ".join(preds) + " .\n"
        for node in extra_nodes:
            ttl += f"\n{node}\n"
        return ttl


def endpoint(
    iri: str,
    *,
    id: str | None = None,
    title: str = "",
    summary: str = "",
    args: list | None = None,
    output: str = "text/plain;charset=utf-8",
    cacheable: bool = False,
    requires: list[str] | None = None,
):
    """Declare a function as a single-verb Source endpoint. The ArgSpecs are
    explicit spec data in L0 (typing-derived specs are a later rung) but they
    are REAL: the host engine routes ``key=value`` arguments by this
    declaration. ``cacheable=True`` marks the result a pure function of its
    inputs (``Expiry::Never``) — the HOST kernel then caches it."""

    def wrap(fn):
        fn.ikigai_endpoint = EndpointDef(
            fn,
            iri,
            id=id,
            title=title,
            summary=summary,
            args=args,
            output=output,
            cacheable=cacheable,
            requires=requires,
        )
        return fn

    return wrap


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def _decode_arg(name: str, arg) -> str | bytes:
    if isinstance(arg, Inline):
        try:
            return arg.data.decode("utf-8")
        except UnicodeDecodeError:
            return arg.data
    # A peer has no back-channel to the host to dereference a Reference or
    # fetch a Content id — fail loud rather than hand the handler an IRI
    # pretending to be a value.
    raise LookupError(f"argument `{name}` arrived by reference; this peer only takes inline values")


class Space:
    """The served resolution space: endpoint lookup + call dispatch."""

    def __init__(self, endpoints, *, strip_alias: bool = True):
        defs = [fn.ikigai_endpoint if hasattr(fn, "ikigai_endpoint") else fn for fn in endpoints]
        for d in defs:
            if not isinstance(d, EndpointDef):
                raise TypeError(f"not an @endpoint-decorated function or EndpointDef: {d!r}")
        self.strip_alias = strip_alias
        self._by_target: dict[str, EndpointDef] = {}
        for d in defs:
            self._bind(d.iri, d)
            if d.alias_iri:
                self._bind(d.alias_iri, d)
        self._defs = defs

    def _bind(self, target: str, d: EndpointDef) -> None:
        held = self._by_target.get(target)
        if held is not None and held is not d:
            raise ValueError(f"two endpoints answer {target}: {held.id} and {d.id}")
        self._by_target[target] = d

    def entries(self, strip_alias: bool | None = None) -> tuple[SpaceEntry, ...]:
        """``strip_alias=None`` uses the server's configured default; a v6
        connection overrides it per its hello mode, which is what retires the
        guessing (a peer finally KNOWS how its mounter addresses it)."""
        strip = self.strip_alias if strip_alias is None else strip_alias
        return tuple(
            SpaceEntry((d.alias_iri if strip else None) or d.iri, d.id) for d in self._defs
        )

    def dispatch(self, call: wire.Call, strip_alias: bool | None = None) -> Reply:
        if isinstance(call, EntriesCall):
            return EntriesReply(self.entries(strip_alias))
        if isinstance(call, IsCached):
            return Cached(False)  # this peer keeps no representation cache
        if isinstance(call, Issue | IssueAs):
            return self._resolve(call.request)
        if isinstance(call, IssueTraced):
            started = int(time.time() * 1000)
            reply = self._resolve(call.request)
            ended = int(time.time() * 1000)
            if isinstance(reply, ErrorReply):
                return reply
            capability = call.capability
            event = TraceEvent(
                target=call.request.target,
                thread=threading.current_thread().name,
                started=started,
                ended=ended,
                cache_hit=False,
                span=0,
                parent=None,
                capability=(None if capability.is_root else tuple(sorted(capability.scopes or ()))),
            )
            assert isinstance(reply, Resolved)
            return ResolvedTraced(reply.representation, reply.cache_status, (event,))
        return ErrorReply(f"endpoint error: unsupported call {type(call).__name__}")

    def _resolve(self, request: Request) -> Reply:
        d = self._by_target.get(request.target)
        if d is None:
            # Exactly the Rust Error::Unresolved rendering, so the host-side
            # engine reports it natively.
            return ErrorReply(f"no endpoint resolved for {request.target}")
        if request.verb == Verb.META:
            return self._meta(d, request)
        if request.verb == Verb.EXISTS:
            return Resolved(
                Representation(b"true", "text/plain;charset=utf-8"),
                CacheStatus.UNCACHEABLE,
            )
        if request.verb != Verb.SOURCE:
            return ErrorReply(
                f"endpoint error: verb {request.verb.wire_name} is not supported by "
                f"`{d.id}` (a single-verb Source endpoint)"
            )
        return self._invoke(d, request)

    def _invoke(self, d: EndpointDef, request: Request) -> Reply:
        kwargs = {}
        try:
            for arg in d.args:
                if arg.name in request.args:
                    kwargs[arg.name] = _decode_arg(arg.name, request.args[arg.name])
                elif arg.default is not None:
                    kwargs[arg.name] = arg.default
                elif arg.required:
                    return ErrorReply(f"missing required argument `{arg.name}`")
        except LookupError as e:
            return ErrorReply(f"endpoint error: {e}")
        try:
            result = d.handler(**kwargs)
        except Exception as e:  # a handler bug crosses as an endpoint error
            return ErrorReply(f"endpoint error: {e}")
        return Resolved(*self._representation(d, result))

    def _representation(self, d: EndpointDef, result) -> tuple[Representation, CacheStatus]:
        media_type = d.output
        if isinstance(result, tuple) and len(result) == 2:
            result, media_type = result
        if isinstance(result, Representation):
            rep = result
        else:
            data = result.encode("utf-8") if isinstance(result, str) else bytes(result)
            expiry = Expiry.never() if d.cacheable else Expiry.always()
            rep = Representation(data, media_type, expiry=expiry)
        # No cache here: cacheable results report MISS ("computed now,
        # cacheable downstream" — the HOST kernel caches by the expiry),
        # everything else UNCACHEABLE.
        status = CacheStatus.MISS if rep.expiry.kind != "always" else CacheStatus.UNCACHEABLE
        return rep, status

    def _meta(self, d: EndpointDef, request: Request) -> Reply:
        target = "text/turtle"  # the kernel's default Meta face
        as_arg = request.args.get("as")
        if isinstance(as_arg, Inline):
            try:
                target = as_arg.data.decode("utf-8")
            except UnicodeDecodeError:
                pass
        if target in ("text/turtle", "*/*", ""):
            body = d.description_turtle().encode("utf-8")
            rep = Representation(body, "text/turtle")
        elif target == "text/plain":
            rep = Representation(d.description_text().encode("utf-8"), "text/plain;charset=utf-8")
        elif target == "application/json":
            body = json.dumps(d.description_json(), separators=(",", ":")).encode("utf-8")
            rep = Representation(body, "application/json")
        else:
            return ErrorReply(f"endpoint error: meta renderer does not support target `{target}`")
        return Resolved(rep, CacheStatus.UNCACHEABLE)


# ---------------------------------------------------------------------------
# The socket server
# ---------------------------------------------------------------------------


def _peer_uid(conn: socket.socket) -> int | None:
    """The connected peer's kernel-verified UID, or ``None`` if unreadable."""
    try:
        if sys.platform == "linux":
            raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            _pid, uid, _gid = struct.unpack("3i", raw)
            return uid
        # macOS / BSD: LOCAL_PEERCRED yields a `struct xucred`
        # (u_int cr_version; uid_t cr_uid; short cr_ngroups; gid_t cr_groups[16]).
        sol_local = 0
        local_peercred = 0x001
        raw = conn.getsockopt(sol_local, local_peercred, 128)
        version, uid = struct.unpack_from("II", raw)
        if version != 0:  # XUCRED_VERSION
            return None
        return uid
    except OSError:
        return None


class Server:
    """A wire server for a set of endpoints. ``serve_forever`` blocks; call
    ``shutdown`` from another thread (or use as a context manager)."""

    def __init__(
        self,
        endpoints,
        path: str | Path,
        *,
        strip_alias: bool = True,
        check_peer_uid: bool = True,
    ):
        self.space = Space(endpoints, strip_alias=strip_alias)
        self.path = Path(path)
        self._check_peer_uid = check_peer_uid
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.unlink(missing_ok=True)  # a leftover socket would fail the bind
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._listener.bind(str(self.path))
        self.path.chmod(0o600)
        self._listener.listen()
        self._closing = False

    def serve_forever(self) -> None:
        while True:
            try:
                conn, _ = self._listener.accept()
            except OSError:
                if self._closing:
                    return
                raise
            if self._closing:
                conn.close()  # the shutdown wake-up connection
                return
            if self._check_peer_uid and _peer_uid(conn) != _own_uid():
                conn.close()  # not our user (or unverifiable) — drop it
                continue
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        with conn, conn.makefile("rwb") as f:
            # The FIRST frame decides the connection's era (wire v6): a hello
            # is answered with ours — equal versions proceed (and its mode
            # picks this connection's entries form), unequal versions get the
            # answer (so the client names both in its error) and a close. A
            # frame WITHOUT the magic is a <= v5 client's first Call: served
            # under the server's configured default, with a warning — the
            # one-version tolerance removed at v7.
            strip_alias: bool | None = None
            try:
                first = wire.read_frame(f)
            except (EOFError, OSError):
                return
            hello = wire.decode_hello(first)
            if hello is not None:
                try:
                    wire.write_frame(f, wire.encode_hello(wire.Hello(wire.PROTOCOL_VERSION)))
                except OSError:
                    return
                if hello.version != wire.PROTOCOL_VERSION:
                    return  # the client renders the mismatch
                strip_alias = hello.mode == wire.HelloMode.ALIAS
            else:
                print(
                    "ikigai-python: a client connected without the version hello "
                    "(wire <= v5) — served in legacy mode (tolerated until v7). "
                    "Update the client.",
                    file=sys.stderr,
                )
                if not self._serve_one_frame(f, first, strip_alias):
                    return
            while True:
                try:
                    frame = wire.read_frame(f)
                except (EOFError, OSError):
                    return  # peer hung up
                if not self._serve_one_frame(f, frame, strip_alias):
                    return

    def _serve_one_frame(self, f, frame: bytes, strip_alias: bool | None) -> bool:
        """Decode and answer one Call frame; ``False`` ends the connection."""
        try:
            call = wire.decode_call(frame)
        except wire.ProtocolError as e:
            # An undecodable frame. Answer once, loudly, then drop the
            # connection — framing after a bad frame is unreliable.
            try:
                wire.write_frame(f, wire.encode_reply(ErrorReply(f"endpoint error: {e}")))
            except OSError:
                pass
            return False
        try:
            wire.write_frame(f, wire.encode_reply(self.space.dispatch(call, strip_alias)))
        except OSError:
            return False
        return True

    def shutdown(self) -> None:
        self._closing = True
        # On Linux, closing a listening socket does NOT wake a thread blocked
        # in accept() — connect once to nudge the accept loop awake, so a
        # serve_forever thread exits promptly instead of only on join timeout.
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as nudge:
                nudge.settimeout(1)
                nudge.connect(str(self.path))
        except OSError:
            pass  # nothing accepting (already down) is fine
        self._listener.close()
        self.path.unlink(missing_ok=True)

    def __enter__(self) -> Server:
        return self

    def __exit__(self, *exc) -> None:
        self.shutdown()


def _own_uid() -> int:
    import os

    return os.getuid()


def serve(
    endpoints,
    path: str | Path,
    *,
    strip_alias: bool = True,
    check_peer_uid: bool = True,
) -> None:
    """Serve ``endpoints`` (functions decorated with :func:`endpoint`) on the
    Unix socket at ``path``. Blocks until interrupted."""
    server = Server(
        endpoints,
        path,
        strip_alias=strip_alias,
        check_peer_uid=check_peer_uid,
    )
    try:
        server.serve_forever()
    finally:
        server.shutdown()
