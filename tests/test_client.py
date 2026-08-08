"""Sync-client behavior against the scripted stub server."""

import asyncio
import time

import pytest

import ikigai
from ikigai import aio, wire
from ikigai.wire import (
    Cached,
    CacheStatus,
    Capability,
    EntriesCall,
    EntriesReply,
    ErrorReply,
    Inline,
    IsCached,
    Issue,
    IssueAs,
    IssueTraced,
    Representation,
    Resolved,
    ResolvedTraced,
    SpaceEntry,
    TraceEvent,
    Verb,
)


def upper_responder(call):
    """A stub kernel: urn:fn:toUpper plus canned entries."""
    if isinstance(call, EntriesCall):
        return EntriesReply((SpaceEntry("urn:fn:toUpper", "toUpper"),))
    if isinstance(call, IsCached):
        return Cached(call.request.args.get("in") == Inline(b"cached"))
    if isinstance(call, Issue | IssueAs | IssueTraced):
        request = call.request
        if request.target != "urn:fn:toUpper":
            return ErrorReply(f"no endpoint resolved for {request.target}")
        arg = request.args.get("in")
        if not isinstance(arg, Inline):
            return ErrorReply("missing required argument `in`")
        rep = Representation(arg.data.upper(), "text/plain;charset=utf-8")
        if isinstance(call, IssueTraced):
            event = TraceEvent(target=request.target, thread="stub", span=0)
            return ResolvedTraced(rep, CacheStatus.MISS, (event,))
        return Resolved(rep, CacheStatus.MISS)
    return ErrorReply(f"unexpected call {call!r}")


@pytest.fixture
def upper_socket(stub_server):
    return stub_server(upper_responder)


def test_source_round_trip(upper_socket):
    with ikigai.connect(upper_socket) as k:
        # The stub answers the hello (required since v7); the failure shapes
        # of the exchange itself live in test_hello.
        assert k.server_version == wire.PROTOCOL_VERSION
        rep = k.source("urn:fn:toUpper", **{"in": "hi"})
        assert rep.text == "HI"
        assert rep.media_type == "text/plain;charset=utf-8"
        assert rep.cache_status == CacheStatus.MISS


def test_unresolved_target_raises_endpoint_error(upper_socket):
    with ikigai.connect(upper_socket) as k:
        with pytest.raises(ikigai.EndpointError, match="no endpoint resolved for urn:nope"):
            k.source("urn:nope")


def test_endpoint_error_prefix_is_stripped(stub_server):
    # The flat v6 Reply::Error is no longer sent but stays decodable; its
    # rendered string surfaces as the base EndpointError, prefix stripped.
    path = stub_server(lambda call: ErrorReply("endpoint error: boom"))
    with ikigai.connect(path) as k:
        with pytest.raises(ikigai.EndpointError, match="^boom$"):
            k.source("urn:x")


@pytest.mark.parametrize(
    "error,transient",
    [
        (ikigai.DeniedError("needs urn:cap:x"), False),
        (ikigai.NotFoundError("no such row"), False),
        (ikigai.TimeoutError("5s elapsed"), True),
        (ikigai.UnavailableError("connection refused"), True),
        (ikigai.MissingArgumentError("in"), False),
        (ikigai.InvalidArgumentError("n", "not a number"), False),
        (ikigai.UnresolvedError("urn:x"), False),
    ],
    ids=lambda v: type(v).__name__ if isinstance(v, Exception) else str(v),
)
def test_typed_errors_are_raised_as_their_taxonomy(stub_server, error, transient):
    # The wire v7 payoff on the client: the SAME variant the server saw is
    # what the caller catches, transience intact.
    path = stub_server(lambda call: wire.ErrorTypedReply(error))
    with ikigai.connect(path) as k:
        with pytest.raises(type(error)) as caught:
            k.source("urn:x")
    assert type(caught.value) is type(error)
    assert caught.value.message == error.message
    assert caught.value.transient is transient
    # Every typed error is still an EndpointError — existing broad handlers
    # keep working unchanged.
    assert isinstance(caught.value, ikigai.EndpointError)


def test_typed_errors_cross_the_async_client_too(stub_server):
    path = stub_server(lambda call: wire.ErrorTypedReply(ikigai.DeniedError("no grant")))

    async def scenario():
        k = await aio.connect(path)
        try:
            with pytest.raises(ikigai.DeniedError, match="no grant"):
                await k.source("urn:x")
        finally:
            await k.close()

    asyncio.run(scenario())


def test_entries(upper_socket):
    with ikigai.connect(upper_socket) as k:
        entries = k.entries()
        assert entries == [SpaceEntry("urn:fn:toUpper", "toUpper")]


def test_is_cached(upper_socket):
    with ikigai.connect(upper_socket) as k:
        assert k.is_cached("urn:fn:toUpper", **{"in": "cached"})
        assert not k.is_cached("urn:fn:toUpper", **{"in": "fresh"})


def test_source_traced_returns_events(upper_socket):
    with ikigai.connect(upper_socket) as k:
        rep, events = k.source_traced("urn:fn:toUpper", **{"in": "hi"})
        assert rep.text == "HI"
        assert [e.target for e in events] == ["urn:fn:toUpper"]


def test_capability_rides_as_issue_as(stub_server):
    seen = []

    def responder(call):
        seen.append(call)
        return Resolved(Representation(b"ok"), CacheStatus.UNCACHEABLE)

    path = stub_server(responder)
    cap = Capability.scoped(["urn:cap:demo"])
    with ikigai.connect(path, capability=cap) as k:
        k.source("urn:x")
    assert isinstance(seen[0], IssueAs)
    assert seen[0].capability == cap


def test_sink_routes_value_as_content(stub_server):
    seen = []

    def responder(call):
        seen.append(call)
        return Resolved(Representation(b"ok"), CacheStatus.UNCACHEABLE)

    path = stub_server(responder)
    with ikigai.connect(path) as k:
        k.sink("urn:file:notes.txt", "hello")
    request = seen[0].request
    assert request.verb == Verb.SINK
    assert request.args["content"] == Inline(b"hello")


def test_absent_socket_is_a_clean_exception(socket_dir):
    with pytest.raises(ikigai.ConnectionLost, match="cannot reach a kernel server"):
        ikigai.connect(socket_dir / "nothing-here.sock")


def test_hung_server_times_out_instead_of_hanging(stub_server):
    path = stub_server(lambda call: None)  # accepts, never replies
    with ikigai.connect(path, timeout=0.2) as k:
        start = time.monotonic()
        with pytest.raises(ikigai.ConnectionLost, match="hung or gone"):
            k.source("urn:fn:toUpper", **{"in": "hi"})
        assert time.monotonic() - start < 2


def test_argument_coercion():
    from ikigai.client import coerce_arg

    assert coerce_arg("hi") == Inline(b"hi")
    assert coerce_arg(b"\x00") == Inline(b"\x00")
    assert coerce_arg(True) == Inline(b"true")
    assert coerce_arg(7) == Inline(b"7")
    assert coerce_arg(wire.Reference("urn:x")) == wire.Reference("urn:x")
    with pytest.raises(TypeError):
        coerce_arg(object())


# -- the asyncio face ------------------------------------------------------


def test_async_client_shares_the_surface(upper_socket):
    async def scenario():
        k = await aio.connect(upper_socket)
        try:
            rep = await k.source("urn:fn:toUpper", **{"in": "hi"})
            assert rep.text == "HI"
            entries = await k.entries()
            assert entries == [SpaceEntry("urn:fn:toUpper", "toUpper")]
            with pytest.raises(ikigai.EndpointError):
                await k.source("urn:nope")
            rep, events = await k.source_traced("urn:fn:toUpper", **{"in": "yo"})
            assert rep.text == "YO"
            assert len(events) == 1
        finally:
            await k.close()

    asyncio.run(scenario())


def test_async_absent_socket(socket_dir):
    async def scenario():
        with pytest.raises(ikigai.ConnectionLost):
            await aio.connect(socket_dir / "nope.sock")

    asyncio.run(scenario())


def test_async_hung_server_times_out(stub_server):
    path = stub_server(lambda call: None)

    async def scenario():
        k = await aio.connect(path, timeout=0.2)
        try:
            with pytest.raises(ikigai.ConnectionLost, match="hung or gone"):
                await k.source("urn:fn:toUpper", **{"in": "hi"})
        finally:
            await k.close()

    asyncio.run(scenario())
