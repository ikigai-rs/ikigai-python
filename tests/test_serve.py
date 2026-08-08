"""serve() behavior, driven by the real Python client (Python <-> Python)."""

import threading

import pytest

import ikigai
from ikigai import wire
from ikigai.serve import Server, endpoint
from ikigai.wire import CacheStatus, Verb


@endpoint(
    "urn:py:hello",
    summary="Greet someone",
    args=[
        {
            "name": "who",
            "required": True,
            "summary": "the name to greet",
            "class": "http://www.w3.org/2001/XMLSchema#string",
        },
        {"name": "greeting", "default": "Hello", "summary": "the salutation"},
    ],
)
def hello(who: str, greeting: str = "Hello") -> str:
    return f"{greeting}, {who}!"


@endpoint("urn:py:reverse", summary="Reverse a string", args=["in"], cacheable=True)
def reverse(**kwargs) -> str:
    return kwargs["in"][::-1]


@endpoint("urn:py:boom", summary="Always fails")
def boom() -> str:
    raise RuntimeError("kaboom")


@endpoint("urn:py:vault", summary="Raises the taxonomy deliberately", args=["key"])
def vault(key: str) -> str:
    # A handler that KNOWS its failure taxonomy: absent → NotFound, refused →
    # Denied — the typed variant must cross the wire intact, not be flattened
    # into a generic endpoint error.
    if key == "secret":
        raise ikigai.DeniedError("needs urn:cap:vault")
    raise ikigai.NotFoundError(f"no entry for `{key}`")


@pytest.fixture
def served(socket_dir):
    path = socket_dir / "py.sock"
    server = Server([hello, reverse, boom, vault], path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield path
    server.shutdown()
    thread.join(timeout=5)


def test_source_with_named_args(served):
    with ikigai.connect(served) as k:
        assert k.source("urn:py:hello", who="Ada").text == "Hello, Ada!"
        assert k.source("urn:py:hello", who="Ada", greeting="Hi").text == "Hi, Ada!"


def test_alias_stripped_target_also_resolves(served):
    # An alias mount (--mount urn:py:=sock) forwards urn:py:hello as urn:hello.
    with ikigai.connect(served) as k:
        assert k.source("urn:hello", who="Ada").text == "Hello, Ada!"


def test_entries_follow_the_hello_mode_per_connection(served):
    # The hello's mode decides the entries form PER CONNECTION — the fix
    # for "a peer cannot know its mount mode". An alias-mode client gets the
    # stripped patterns its mount will re-prefix; a verbatim client (a plain
    # client, or an --override/--prefer mount) gets the declared IRIs — even
    # from the same server, at the same time.
    with ikigai.connect(served, mode=wire.HelloMode.ALIAS) as k:
        stripped = {e.pattern for e in k.entries()}
        assert stripped == {"urn:hello", "urn:reverse", "urn:boom", "urn:vault"}
    with ikigai.connect(served) as k:  # verbatim is the default
        verbatim = {e.pattern for e in k.entries()}
        assert verbatim == {"urn:py:hello", "urn:py:reverse", "urn:py:boom", "urn:py:vault"}


def test_describe_face_routes_named_args(served):
    with ikigai.connect(served) as k:
        description = k.describe("urn:py:hello")
        assert description["id"] == "hello"
        assert description["verbs"] == ["Source", "Meta"]
        inputs = {i["name"]: i for i in description["inputs"]}
        assert inputs["who"]["required"] is True
        assert inputs["who"]["class"] == "http://www.w3.org/2001/XMLSchema#string"
        assert inputs["greeting"]["required"] is False
        assert inputs["greeting"]["default"] == "Hello"


def test_meta_faces(served):
    with ikigai.connect(served) as k:
        turtle = k.meta("urn:py:hello")
        assert turtle.base_media_type == "text/turtle"
        assert "<urn:ikigai:endpoint:hello>" in turtle.text
        assert "ik:input <urn:ikigai:endpoint:hello:input:who>" in turtle.text
        text = k.meta("urn:py:hello", as_="text/plain")
        assert "input who [argument]" in text.text
        with pytest.raises(ikigai.EndpointError, match="does not support target"):
            k.meta("urn:py:hello", as_="application/pdf")


def test_missing_required_argument_crosses_typed(served):
    with ikigai.connect(served) as k:
        with pytest.raises(
            ikigai.MissingArgumentError, match="missing required argument `who`"
        ) as e:
            k.source("urn:py:hello")
        assert e.value.name == "who"


def test_unresolved_target_crosses_typed(served):
    with ikigai.connect(served) as k:
        with pytest.raises(
            ikigai.UnresolvedError, match="no endpoint resolved for urn:py:nope"
        ) as e:
            k.source("urn:py:nope")
        assert e.value.iri == "urn:py:nope"


def test_handler_exception_crosses_as_an_endpoint_error(served):
    with ikigai.connect(served) as k:
        with pytest.raises(ikigai.EndpointError, match="kaboom") as e:
            k.source("urn:py:boom")
        assert type(e.value) is ikigai.EndpointError  # the Endpoint variant, untyped domain failure


def test_handler_raised_taxonomy_crosses_typed(served):
    # A handler that raises NotFoundError/DeniedError sends the VARIANT, not
    # a flattened endpoint-error string — the far side can answer 404/403.
    with ikigai.connect(served) as k:
        with pytest.raises(ikigai.NotFoundError, match="no entry for `x`"):
            k.source("urn:py:vault", key="x")
        with pytest.raises(ikigai.DeniedError, match="needs urn:cap:vault") as e:
            k.source("urn:py:vault", key="secret")
        assert e.value.transient is False


def test_cacheable_result_carries_expiry_never(served):
    with ikigai.connect(served) as k:
        rep = k.source("urn:py:reverse", **{"in": "abc"})
        assert rep.text == "cba"
        assert rep.expiry.kind == "never"  # the HOST kernel may cache this
        assert rep.cache_status == CacheStatus.MISS
        plain = k.source("urn:py:hello", who="x")
        assert plain.expiry.kind == "always"
        assert plain.cache_status == CacheStatus.UNCACHEABLE


def test_exists_and_unsupported_verbs(served):
    with ikigai.connect(served) as k:
        assert k.exists("urn:py:hello").text == "true"
        with pytest.raises(ikigai.EndpointError, match="verb Sink is not supported"):
            k.sink("urn:py:hello", "x")
        with pytest.raises(ikigai.EndpointError, match="verb Delete is not supported"):
            k.delete("urn:py:hello")


def test_is_cached_is_false_on_a_peer(served):
    with ikigai.connect(served) as k:
        assert not k.is_cached("urn:py:reverse", **{"in": "abc"})


def test_traced_call_returns_a_span(served):
    with ikigai.connect(served) as k:
        rep, events = k.source_traced("urn:py:hello", who="Ada")
        assert rep.text == "Hello, Ada!"
        assert len(events) == 1
        assert events[0].target == "urn:py:hello"
        assert events[0].capability is None  # root authority
        assert events[0].started is not None


def test_capability_scopes_show_in_the_trace(served):
    cap = ikigai.Capability.scoped(["urn:cap:demo", "urn:cap:a"])
    with ikigai.connect(served, capability=cap) as k:
        _, events = k.source_traced("urn:py:hello", who="Ada")
        assert events[0].capability == ("urn:cap:a", "urn:cap:demo")


def test_undeclared_extra_args_are_ignored(served):
    # The engine adds e.g. `as=` for conneg; a Source handler must not choke.
    with ikigai.connect(served) as k:
        assert k.source("urn:py:hello", who="Ada", as_ignored="x").text == "Hello, Ada!"


def test_strip_alias_false_lists_declared_iris(socket_dir):
    path = socket_dir / "verbatim.sock"
    server = Server([hello], path, strip_alias=False)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with ikigai.connect(path) as k:
            assert [e.pattern for e in k.entries()] == ["urn:py:hello"]
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_colliding_endpoints_fail_loud(socket_dir):
    @endpoint("urn:py:hello")
    def other() -> str:
        return "x"

    with pytest.raises(ValueError, match="two endpoints answer"):
        Server([hello, other], socket_dir / "clash.sock")


def test_shutdown_wakes_the_accept_loop_promptly(socket_dir):
    # On Linux, closing a listening socket does not wake a blocked accept();
    # shutdown() must nudge the loop so the serving thread exits fast (this
    # is what keeps every fixture teardown from burning its join timeout).
    import time

    path = socket_dir / "prompt.sock"
    server = Server([hello], path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.05)
    start = time.monotonic()
    server.shutdown()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert time.monotonic() - start < 2


def test_verbs_and_request_shapes():
    # The described verbs must be the serde names.
    assert Verb.SOURCE.wire_name == "Source"
    assert Verb.META.wire_name == "Meta"
