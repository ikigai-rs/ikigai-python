"""The asyncio client: the same surface as :mod:`ikigai.client`, awaitable.

Shares the codec (:mod:`ikigai.wire`) with the sync path; only the transport
differs (asyncio streams instead of a blocking socket)::

    from ikigai import aio

    k = await aio.connect()
    rep = await k.source("urn:fn:toUpper", **{"in": "hi"})
    await k.close()

For web apps, :func:`lifespan` packages the connect/publish/close cycle as
an ASGI-style lifespan (one connection for the app's lifetime — see its
docstring for the Litestar/FastHTML/Falcon shapes).
"""

from __future__ import annotations

import asyncio
import json
import struct
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from . import wire
from .client import DEFAULT_TIMEOUT, ConnectionLost, build_request, default_socket_path
from .wire import (
    Cached,
    Capability,
    EndpointError,
    EntriesCall,
    EntriesReply,
    ErrorReply,
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
)


class AsyncClient:
    """A connected kernel client over asyncio streams. One request in flight
    at a time per connection (the wire is strictly call/reply)."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        capability: Capability | None = None,
        timeout: float | None = DEFAULT_TIMEOUT,
        server_version: int | None = None,
    ):
        self._reader = reader
        self._writer = writer
        self._timeout = timeout
        self._lock = asyncio.Lock()
        #: When set, requests go as ``Call::IssueAs`` under this capability
        #: (which the server clamps to its authenticated principal).
        self.capability = capability
        #: The version the server declared in its hello, or ``None`` for a
        #: legacy (<= v5) server reached through the fallback.
        self.server_version = server_version

    # -- transport ---------------------------------------------------------

    async def _round_trip(self, call: wire.Call) -> Reply:
        async with self._lock:  # keep concurrent tasks strictly call/reply
            try:
                payload = wire.encode_call(call)
                self._writer.write(wire.frame(payload))
                await self._writer.drain()
                header = await asyncio.wait_for(self._reader.readexactly(4), timeout=self._timeout)
                (length,) = struct.unpack(">I", header)
                if length > wire.MAX_FRAME:
                    raise ProtocolError(
                        f"framed message of {length} bytes exceeds the {wire.MAX_FRAME}-byte limit"
                    )
                body = await asyncio.wait_for(
                    self._reader.readexactly(length), timeout=self._timeout
                )
                return wire.decode_reply(body)
            except TimeoutError as e:
                raise ConnectionLost(
                    "no response from the kernel server (it may be hung or gone)"
                ) from e
            except (asyncio.IncompleteReadError, ConnectionError, BrokenPipeError) as e:
                raise ConnectionLost(f"the kernel server is unreachable: {e}") from e

    async def _issue(self, request: Request) -> Representation:
        call = Issue(request) if self.capability is None else IssueAs(request, self.capability)
        reply = await self._round_trip(call)
        if isinstance(reply, Resolved):
            representation = reply.representation
            representation.cache_status = reply.cache_status
            return representation
        if isinstance(reply, ErrorReply):
            raise EndpointError(wire.decode_error_message(reply.message))
        raise ProtocolError(f"unexpected reply to {type(call).__name__}: {reply!r}")

    # -- the five verbs ----------------------------------------------------

    async def source(self, iri: str, **args) -> Representation:
        return await self._issue(build_request(Verb.SOURCE, iri, args))

    async def sink(self, iri: str, value=None, **args) -> Representation:
        if value is not None:
            args["content"] = value
        return await self._issue(build_request(Verb.SINK, iri, args))

    async def exists(self, iri: str, **args) -> Representation:
        return await self._issue(build_request(Verb.EXISTS, iri, args))

    async def delete(self, iri: str, **args) -> Representation:
        return await self._issue(build_request(Verb.DELETE, iri, args))

    async def meta(self, iri: str, as_: str | None = None, **args) -> Representation:
        if as_ is not None:
            args["as"] = as_
        return await self._issue(build_request(Verb.META, iri, args))

    # -- sugar -------------------------------------------------------------

    async def describe(self, iri: str) -> dict | None:
        try:
            representation = await self.meta(iri, as_="application/json")
        except EndpointError:
            return None
        try:
            return json.loads(representation.data)
        except ValueError:
            return None

    async def entries(self) -> list[SpaceEntry] | None:
        reply = await self._round_trip(EntriesCall())
        if isinstance(reply, EntriesReply):
            return None if reply.entries is None else list(reply.entries)
        if isinstance(reply, ErrorReply):
            raise EndpointError(wire.decode_error_message(reply.message))
        raise ProtocolError(f"unexpected reply to Entries: {reply!r}")

    async def is_cached(self, iri: str, **args) -> bool:
        reply = await self._round_trip(IsCached(build_request(Verb.SOURCE, iri, args)))
        return isinstance(reply, Cached) and reply.cached

    async def source_traced(self, iri: str, **args) -> tuple[Representation, list[TraceEvent]]:
        call = IssueTraced(
            build_request(Verb.SOURCE, iri, args),
            self.capability or Capability.root(),
            TraceContext(trace_id=1, parent_span=None),
        )
        reply = await self._round_trip(call)
        if isinstance(reply, ResolvedTraced):
            representation = reply.representation
            representation.cache_status = reply.cache_status
            return representation, list(reply.events)
        if isinstance(reply, ErrorReply):
            raise EndpointError(wire.decode_error_message(reply.message))
        raise ProtocolError(f"unexpected reply to IssueTraced: {reply!r}")

    # -- lifecycle ---------------------------------------------------------

    async def close(self) -> None:
        self._writer.close()
        try:
            await self._writer.wait_closed()
        except (ConnectionError, BrokenPipeError):
            pass

    async def __aenter__(self) -> AsyncClient:
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()


async def connect(
    path: str | Path | None = None,
    *,
    capability: Capability | None = None,
    timeout: float | None = DEFAULT_TIMEOUT,
    mode: wire.HelloMode = wire.HelloMode.VERBATIM,
) -> AsyncClient:
    """Connect to a kernel server's Unix socket (same default path as the
    sync client and the Rust CLI). Opens with the wire v6 hello, with the
    same one-version legacy fallback as the sync client."""
    path = Path(path) if path is not None else default_socket_path()
    reader, writer = await _dial(path)
    server_version: int | None = wire.PROTOCOL_VERSION
    try:
        writer.write(wire.frame(wire.encode_hello(wire.Hello(wire.PROTOCOL_VERSION, mode))))
        await writer.drain()
        answer = wire.decode_hello(await _read_frame(reader, timeout))
    except (asyncio.IncompleteReadError, ConnectionError, BrokenPipeError, OSError):
        # A <= v5 server hangs up on the hello, silently. Reconnect legacy.
        writer.close()
        print(
            f"ikigai: the kernel server at {path} hung up on the version hello — it likely "
            "predates wire v6; reconnected WITHOUT the hello (tolerated until v7). "
            "Update the server.",
            file=sys.stderr,
        )
        reader, writer = await _dial(path)
        server_version = None
    else:
        if answer is None:
            writer.close()
            raise ProtocolError(
                "the kernel server answered the version hello with something else entirely"
            )
        if answer.version != wire.PROTOCOL_VERSION:
            writer.close()
            raise ProtocolError(
                f"the kernel server speaks wire v{answer.version}, this client speaks "
                f"v{wire.PROTOCOL_VERSION} — update the older side"
            )
    return AsyncClient(
        reader,
        writer,
        capability=capability,
        timeout=timeout,
        server_version=server_version,
    )


def lifespan(
    path: str | Path | None = None,
    *,
    state_attr: str = "kernel",
    capability: Capability | None = None,
    timeout: float | None = DEFAULT_TIMEOUT,
    mode: wire.HelloMode = wire.HelloMode.VERBATIM,
):
    """One kernel connection for an app's lifetime, as an ASGI-style lifespan.

    Returns an async-context-manager factory: called (with or without the app
    instance) it connects, yields the :class:`AsyncClient`, and closes it on
    exit. When the app has a ``state`` (Litestar, Starlette, FastHTML), the
    client is also published there as ``state.<state_attr>`` (default
    ``state.kernel``) so handlers can reach it; pass ``state_attr=""`` to skip
    that. One connection per app is the intended shape — never connect per
    request; a single client serializes concurrent requests internally.

    **Litestar** — usable directly::

        app = Litestar(route_handlers=[...], lifespan=[aio.lifespan(path)])
        # handlers: await state.kernel.source(...)

    **FastHTML / Starlette** — these want an async *generator* function, so
    adapt in one line::

        connection = aio.lifespan(path)
        async def kernel_lifespan(app):
            async with connection(app):
                yield  # handlers: await app.state.kernel.source(...)
        app, rt = fast_app(lifespan=kernel_lifespan)

    **Falcon (ASGI)** — drive the same context manager from lifespan
    middleware::

        class KernelConnection:
            def __init__(self, path=None):
                self._cm = aio.lifespan(path)()
            async def process_startup(self, scope, event):
                self.kernel = await self._cm.__aenter__()
            async def process_shutdown(self, scope, event):
                await self._cm.__aexit__(None, None, None)

    Outside a framework it is an ordinary context manager::

        async with aio.lifespan(path)() as kernel:
            await kernel.source(...)
    """

    @asynccontextmanager
    async def connection(app=None):
        kernel = await connect(path, capability=capability, timeout=timeout, mode=mode)
        state = getattr(app, "state", None)
        if state is not None and state_attr:
            setattr(state, state_attr, kernel)
        try:
            yield kernel
        finally:
            await kernel.close()

    return connection


async def _dial(path: Path):
    try:
        return await asyncio.open_unix_connection(str(path))
    except OSError as e:
        raise ConnectionLost(
            f"cannot reach a kernel server at {path} ({e}); "
            "is `ikigai serve` (or the daemon) running?"
        ) from e


async def _read_frame(reader: asyncio.StreamReader, timeout: float | None) -> bytes:
    header = await asyncio.wait_for(reader.readexactly(4), timeout=timeout)
    (length,) = struct.unpack(">I", header)
    if length > wire.MAX_FRAME:
        raise ProtocolError(
            f"framed message of {length} bytes exceeds the {wire.MAX_FRAME}-byte limit"
        )
    return await asyncio.wait_for(reader.readexactly(length), timeout=timeout)
