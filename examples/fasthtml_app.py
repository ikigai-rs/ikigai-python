"""FastHTML face: server-driven HTML + htmx over the ikigai wire client.

The pattern to notice: the hypermedia idiom (the house style of
ikigai-runbook). No JSON API, no client-side app — each form ``hx-get``s a
route, the route resolves an ikigai resource, and the returned HTML fragment
swaps into the page. The catalog page is the same idea pointed at
``kernel.entries()``: the UI is *discovered* from the running space.

Every result fragment shows ``rep.cache_status`` — served direct it reads
MISS; served through a Rust kernel mount, repeats read HIT.

Run (direct, no Rust needed)::

    python -m examples.endpoints /tmp/py-examples.sock &
    IKIGAI_SOCKET=/tmp/py-examples.sock uvicorn examples.fasthtml_app:app
"""

from __future__ import annotations

import os

from fasthtml.common import (
    H2,
    A,
    Button,
    Code,
    Div,
    Form,
    Input,
    P,
    Small,
    Table,
    Td,
    Th,
    Titled,
    Tr,
    fast_app,
)
from starlette.responses import PlainTextResponse

from examples import error_status
from ikigai import ConnectionLost, EndpointError, aio, default_socket_path


def socket_path() -> str:
    return os.environ.get("IKIGAI_SOCKET") or str(default_socket_path())


class _Connection:
    kernel = None


conn = _Connection()


async def kernel_connection(app):
    # A bare async generator: FastHTML iterates it around the app's life
    # (it does NOT take an asynccontextmanager, unlike Litestar).
    conn.kernel = await aio.connect(socket_path())
    try:
        yield
    finally:
        await conn.kernel.close()


def endpoint_error(request, exc: EndpointError):
    # The typed taxonomy picks the status (wire v7): Denied→403, NotFound→404,
    # bad input→400, transient→503, anything else→502.
    return PlainTextResponse(str(exc), status_code=error_status(exc))


def connection_lost(request, exc: ConnectionLost):
    return PlainTextResponse(f"{exc} — is the peer running?", status_code=503)


app, rt = fast_app(
    lifespan=kernel_connection,
    exception_handlers={EndpointError: endpoint_error, ConnectionLost: connection_lost},
    secret_key="ikigai-examples",  # examples keep no sessions; skip the keyfile
)


async def resolved(iri: str, **args):
    """Resolve and render: one fragment shape for every endpoint."""
    rep = await conn.kernel.source(iri, **args)
    return P(rep.text, Small(f"  [cache: {rep.cache_status.name}]"))


def demo_form(title: str, action: str, field: str, target: str):
    """A form per endpoint: htmx GETs the route, the fragment swaps in."""
    return Div(
        H2(title),
        Form(
            Input(name=field, placeholder=field, required=True),
            Button("Resolve"),
            hx_get=action,
            hx_target=f"#{target}",
        ),
        Div(id=target),
    )


@rt("/")
def index():
    return Titled(
        "ikigai examples",
        P("Each form resolves a ", Code("urn:py:*"), " resource over the wire."),
        demo_form("hello", "/hello", "who", "hello-out"),
        demo_form("upper", "/upper", "text", "upper-out"),
        demo_form("reverse", "/reverse", "text", "reverse-out"),
        P(A("Browse the catalog", href="/catalog")),
    )


@rt("/hello/{who}")
async def hello_path(who: str):
    return await resolved("urn:py:hello", who=who)


@rt("/hello")
async def hello_query(who: str):
    # The form's face: htmx sends the input as a query parameter.
    return await resolved("urn:py:hello", who=who)


@rt("/upper")
async def upper(text: str):
    return await resolved("urn:py:upper", text=text)


@rt("/reverse")
async def reverse(text: str):
    return await resolved("urn:py:reverse", text=text)


@rt("/catalog")
async def catalog():
    entries = await conn.kernel.entries() or []
    rows = [Tr(Td(Code(e.pattern)), Td(e.endpoint), Td(e.origin or "local")) for e in entries]
    return Titled(
        "Catalog",
        P("Every binding the connected space enumerates."),
        Table(Tr(Th("pattern"), Th("endpoint"), Th("origin")), *rows),
        P(A("Back", href="/")),
    )
