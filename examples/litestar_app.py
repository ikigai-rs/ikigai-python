"""Litestar face: typed handlers over the ikigai wire client.

The pattern to notice: a handler's *signature* is its contract — Litestar
validates path/query parameters from the type hints before the handler runs,
which is the closest Python analog to an ikigai ArgSpec (a missing ``text``
is a 400 from the framework, never a half-formed resolution). The handler
body is then one line of HTTP-independent resolution: ``kernel.source(...)``.

Run (direct, no Rust needed)::

    python -m examples.endpoints /tmp/py-examples.sock &
    IKIGAI_SOCKET=/tmp/py-examples.sock uvicorn examples.litestar_app:app

Or through a Rust kernel (see examples/README.md) — the ``X-Ikigai-Cache``
response header then reports the kernel cache's answer (HIT on repeats).
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from litestar import Litestar, Request, Response, get
from litestar.datastructures import State
from litestar.params import FromPath, FromQuery

from examples import error_status
from ikigai import ConnectionLost, EndpointError, aio, default_socket_path

if TYPE_CHECKING:
    from ikigai.wire import Representation


def socket_path() -> str:
    return os.environ.get("IKIGAI_SOCKET") or str(default_socket_path())


@asynccontextmanager
async def kernel_connection(app: Litestar):
    """One connection for the app's lifetime (never connect per request).
    A single AsyncClient serializes concurrent requests internally — fine at
    example scale."""
    kernel = await aio.connect(socket_path())
    app.state.kernel = kernel
    try:
        yield
    finally:
        await kernel.close()


def rep_response(rep: Representation) -> Response[bytes]:
    """A representation IS a response: bytes + media type + cache verdict."""
    return Response(
        rep.data,
        media_type=rep.media_type,
        headers={"X-Ikigai-Cache": rep.cache_status.name},
    )


@get("/hello/{who:str}")
async def hello(who: FromPath[str], state: State) -> Response[bytes]:
    return rep_response(await state.kernel.source("urn:py:hello", who=who))


@get("/upper")
async def upper(text: FromQuery[str], state: State) -> Response[bytes]:
    return rep_response(await state.kernel.source("urn:py:upper", text=text))


@get("/reverse")
async def reverse(text: FromQuery[str], state: State) -> Response[bytes]:
    return rep_response(await state.kernel.source("urn:py:reverse", text=text))


@get("/catalog")
async def catalog(state: State) -> list[dict]:
    """The kernel's catalog, as JSON: what this app could reach, discovered
    from the running space rather than hard-coded."""
    entries = await state.kernel.entries() or []
    return [{"pattern": e.pattern, "endpoint": e.endpoint, "origin": e.origin} for e in entries]


def endpoint_error(request: Request, exc: EndpointError) -> Response[str]:
    """The peer's failure, with its taxonomy intact (wire v7): the typed
    exception picks the status — Denied→403, NotFound→404, bad input→400,
    transient→503, anything else→502 — carrying the endpoint's message."""
    return Response(str(exc), status_code=error_status(exc), media_type="text/plain")


def connection_lost(request: Request, exc: ConnectionLost) -> Response[str]:
    return Response(f"{exc} — is the peer running?", status_code=503, media_type="text/plain")


app = Litestar(
    route_handlers=[hello, upper, reverse, catalog],
    lifespan=[kernel_connection],
    exception_handlers={EndpointError: endpoint_error, ConnectionLost: connection_lost},
)
