"""aio.lifespan (the one lifespan helper) and the AsyncClient constructor
surface, driven against a real ikigai.serve Server (which answers the v6
hello, unlike the scripted stub)."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest

from ikigai import aio, wire
from ikigai.serve import Server, endpoint


@endpoint("urn:py:echo")
def echo(text: str) -> str:
    return text


@pytest.fixture
def served(socket_dir):
    path = socket_dir / "aio.sock"
    server = Server([echo], path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield path
    server.shutdown()
    thread.join(timeout=5)


def test_lifespan_yields_a_connected_client_and_closes_it(served):
    async def scenario():
        connection = aio.lifespan(served)
        async with connection() as kernel:
            rep = await kernel.source("urn:py:echo", text="hi")
            assert rep.text == "hi"
        assert kernel._writer.is_closing()  # closed on exit, not leaked

    asyncio.run(scenario())


def test_lifespan_publishes_the_client_on_app_state(served):
    app = SimpleNamespace(state=SimpleNamespace())

    async def scenario():
        async with aio.lifespan(served)(app) as kernel:
            assert app.state.kernel is kernel

    asyncio.run(scenario())


def test_lifespan_state_attr_opt_out(served):
    app = SimpleNamespace(state=SimpleNamespace())

    async def scenario():
        async with aio.lifespan(served, state_attr="")(app):
            assert not hasattr(app.state, "kernel")

    asyncio.run(scenario())


def test_lifespan_without_an_app_or_state_is_fine(served):
    async def scenario():
        # No app at all, and an app without .state — both just skip publish.
        async with aio.lifespan(served)() as kernel:
            assert (await kernel.source("urn:py:echo", text="a")).text == "a"
        async with aio.lifespan(served)(object()) as kernel:
            assert (await kernel.source("urn:py:echo", text="b")).text == "b"

    asyncio.run(scenario())


def test_lifespan_is_usable_directly_by_litestar(served):
    # The claim in the docstring, proven: lifespan=[aio.lifespan(path)] and
    # handlers reach the client as state.kernel.
    pytest.importorskip("litestar")
    from litestar import Litestar, get
    from litestar.datastructures import State
    from litestar.testing import TestClient

    @get("/echo")
    async def echo_route(text: str, state: State) -> str:
        rep = await state.kernel.source("urn:py:echo", text=text)
        return rep.text

    app = Litestar(route_handlers=[echo_route], lifespan=[aio.lifespan(served)])
    with TestClient(app) as client:
        assert client.get("/echo?text=hi").text == "hi"


def test_connect_sets_server_version_via_the_constructor(served):
    # Report item 7: server_version rides the AsyncClient constructor (like
    # the sync Client), not a post-construction attribute poke.
    async def scenario():
        kernel = await aio.connect(served)
        try:
            assert kernel.server_version == wire.PROTOCOL_VERSION
        finally:
            await kernel.close()

    asyncio.run(scenario())
    assert aio.AsyncClient(None, None, server_version=None).server_version is None
    assert aio.AsyncClient(None, None).server_version is None
