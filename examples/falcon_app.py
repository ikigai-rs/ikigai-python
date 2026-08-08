"""Falcon face: bare ASGI resource classes over the ikigai wire client.

The pattern to notice: Falcon adds almost nothing between HTTP and the wire —
a resource class per route, ``req.get_param(..., required=True)`` as the whole
input contract (Falcon 400s a missing param), and the responder body is one
``kernel.source(...)``. Lifecycle is ASGI lifespan via middleware; error
mapping is ``add_error_handler`` (wire errors become 502/503).

Run (direct, no Rust needed)::

    python -m examples.endpoints /tmp/py-examples.sock &
    IKIGAI_SOCKET=/tmp/py-examples.sock uvicorn examples.falcon_app:app
"""

from __future__ import annotations

import json
import os

import falcon
import falcon.asgi

from examples import error_status
from ikigai import ConnectionLost, EndpointError, aio, default_socket_path


def socket_path() -> str:
    return os.environ.get("IKIGAI_SOCKET") or str(default_socket_path())


class KernelConnection:
    """ASGI lifespan middleware: one connection for the app's lifetime."""

    def __init__(self):
        self.kernel = None

    async def process_startup(self, scope, event):
        self.kernel = await aio.connect(socket_path())

    async def process_shutdown(self, scope, event):
        if self.kernel is not None:
            await self.kernel.close()


def send_rep(resp: falcon.asgi.Response, rep) -> None:
    resp.data = rep.data
    resp.content_type = rep.media_type
    resp.set_header("X-Ikigai-Cache", rep.cache_status.name)


class HelloResource:
    def __init__(self, conn: KernelConnection):
        self._conn = conn

    async def on_get(self, req, resp, who):
        send_rep(resp, await self._conn.kernel.source("urn:py:hello", who=who))


class SourceResource:
    """One class serves both /upper and /reverse: the route only decides
    which resource IRI the ``text`` param is resolved against."""

    def __init__(self, conn: KernelConnection, iri: str):
        self._conn = conn
        self._iri = iri

    async def on_get(self, req, resp):
        text = req.get_param("text", required=True)  # Falcon 400s when absent
        send_rep(resp, await self._conn.kernel.source(self._iri, text=text))


class CatalogResource:
    def __init__(self, conn: KernelConnection):
        self._conn = conn

    async def on_get(self, req, resp):
        entries = await self._conn.kernel.entries() or []
        resp.text = json.dumps(
            [{"pattern": e.pattern, "endpoint": e.endpoint, "origin": e.origin} for e in entries]
        )
        resp.content_type = falcon.MEDIA_JSON


async def endpoint_error(req, resp, exc: EndpointError, params):
    # The typed taxonomy picks the status (wire v7): Denied→403, NotFound→404,
    # bad input→400, transient→503, anything else→502.
    raise falcon.HTTPError(falcon.util.code_to_http_status(error_status(exc)), description=str(exc))


async def connection_lost(req, resp, exc: ConnectionLost, params):
    raise falcon.HTTPServiceUnavailable(description=f"{exc} — is the peer running?")


def create_app() -> falcon.asgi.App:
    conn = KernelConnection()
    app = falcon.asgi.App(middleware=[conn])
    app.add_route("/hello/{who}", HelloResource(conn))
    app.add_route("/upper", SourceResource(conn, "urn:py:upper"))
    app.add_route("/reverse", SourceResource(conn, "urn:py:reverse"))
    app.add_route("/catalog", CatalogResource(conn))
    app.add_error_handler(EndpointError, endpoint_error)
    app.add_error_handler(ConnectionLost, connection_lost)
    return app


app = create_app()
