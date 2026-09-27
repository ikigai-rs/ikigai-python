"""Families: templated doors and multi-verb endpoints, over the real wire."""

import threading
from typing import Annotated

import pytest

import ikigai
from ikigai import family
from ikigai.serve import Server, Space, endpoint
from ikigai.wire import CacheStatus, HelloMode, Issue, Request, Verb

XSD_INTEGER = "http://www.w3.org/2001/XMLSchema#integer"


@endpoint("urn:py:echo/{message}", summary="Echo the name back")
def echo(message: Annotated[str, "what to echo"], suffix: str = "") -> str:
    return message + suffix


def notes_family():
    """A small multi-verb family over its own state."""
    notes: dict[str, str] = {}
    f = family("urn:py:note:{key}", id="note", title="Notes", summary="A key-value note")

    @f.source(cacheable=True, summary="read a note")
    def read(key: str) -> str:
        if key not in notes:
            raise ikigai.NotFoundError(f"no note `{key}`")
        return notes[key]

    @f.sink(summary="write a note")
    def write(key: str, content: str) -> None:
        notes[key] = content  # None answers `ok`

    @f.delete(summary="forget a note")
    def forget(key: str):
        notes.pop(key, None)

    return f, notes


@pytest.fixture
def served(socket_dir):
    notes, _ = notes_family()
    path = socket_dir / "fam.sock"
    server = Server([echo, notes], path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield path
    server.shutdown()
    thread.join(timeout=5)


# -- templated doors --------------------------------------------------------


def test_a_templated_endpoint_answers_every_name_it_matches(served):
    with ikigai.connect(served) as k:
        assert k.source("urn:py:echo/hello").text == "hello"
        assert k.source("urn:py:echo/a/b:c").text == "a/b:c"  # the trailing var takes the rest
        assert k.source("urn:py:echo/hi", suffix="!").text == "hi!"
        with pytest.raises(ikigai.UnresolvedError):
            k.source("urn:py:echo/")  # an empty capture matches nothing


def test_the_catalog_lists_the_template(served):
    with ikigai.connect(served) as k:
        assert {e.pattern: e.endpoint for e in k.entries()} == {
            "urn:py:echo/{message}": "echo",
            "urn:py:note:{key}": "note",
        }
    with ikigai.connect(served, mode=HelloMode.ALIAS) as k:
        assert {e.pattern for e in k.entries()} == {"urn:echo/{message}", "urn:note:{key}"}


def test_an_alias_stripped_name_resolves_a_family_too(served):
    # --mount urn:py:=sock forwards urn:py:note:a as urn:note:a.
    with ikigai.connect(served, mode=HelloMode.ALIAS) as k:
        k.sink("urn:note:a", "via the alias")
        assert k.source("urn:note:a").text == "via the alias"
    with ikigai.connect(served) as k:  # the same state under the declared name
        assert k.source("urn:py:note:a").text == "via the alias"


def test_a_binding_is_described_as_a_binding(served):
    with ikigai.connect(served) as k:
        description = k.describe("urn:py:echo/anything")
    inputs = {i["name"]: i for i in description["inputs"]}
    assert inputs["message"] == {
        "name": "message",
        "summary": "what to echo",
        "required": True,
        "source": "binding",
        "class": "http://www.w3.org/2001/XMLSchema#string",
    }
    assert inputs["suffix"]["source"] == "argument"
    assert [i["name"] for i in description["inputs"]] == ["message", "suffix"]  # bindings first


def test_meta_on_the_catalog_pattern_describes_the_family(served):
    with ikigai.connect(served) as k:
        assert k.describe("urn:py:note:{key}")["id"] == "note"
        text = k.meta("urn:py:echo/{message}", as_="text/plain").text
    assert "input message [binding]: what to echo" in text


def test_an_argument_cannot_override_a_binding(served):
    # Identity lives in the name; a same-named arg is ignored, not obeyed.
    with ikigai.connect(served) as k:
        assert k.source("urn:py:echo/real", message="forged").text == "real"


# -- multi-verb dispatch ----------------------------------------------------


def test_source_sink_delete_round_trip(served):
    with ikigai.connect(served) as k:
        with pytest.raises(ikigai.NotFoundError, match="no note `a`"):
            k.source("urn:py:note:a")
        written = k.sink("urn:py:note:a", "hello")
        assert written.text == "ok"
        read = k.source("urn:py:note:a")
        assert read.text == "hello"
        assert read.expiry.kind == "never"
        assert read.cache_status == CacheStatus.MISS
        assert k.delete("urn:py:note:a").text == "ok"
        assert k.delete("urn:py:note:a").text == "ok"  # idempotent
        with pytest.raises(ikigai.NotFoundError):
            k.source("urn:py:note:a")


def test_a_write_answer_is_never_cacheable(served):
    with ikigai.connect(served) as k:
        written = k.sink("urn:py:note:b", "x")
        assert written.expiry.kind == "always"
        assert written.cache_status == CacheStatus.UNCACHEABLE
        assert k.delete("urn:py:note:b").expiry.kind == "always"


def test_sink_requires_content(served):
    with ikigai.connect(served) as k:
        with pytest.raises(ikigai.MissingArgumentError) as e:
            k.sink("urn:py:note:c")
        assert e.value.name == "content"


def test_exists_defaults_to_source_would_succeed(served):
    with ikigai.connect(served) as k:
        assert k.exists("urn:py:note:d").text == "false"  # NotFound → false
        k.sink("urn:py:note:d", "here")
        exists = k.exists("urn:py:note:d")
        assert exists.text == "true"
        assert exists.expiry.kind == "always"
        # a flat endpoint keeps its old answer: it is bound, so it exists
        assert k.exists("urn:py:echo/x").text == "true"


def test_an_unsupported_verb_names_the_verbs_that_are(served):
    with ikigai.connect(served) as k:
        with pytest.raises(ikigai.EndpointError) as e:
            k.sink("urn:py:echo/x", "y")
    assert type(e.value) is ikigai.EndpointError
    assert str(e.value).endswith(
        "verb Sink is not supported by `echo` (it answers Source, Exists, Meta)"
    )


def test_the_json_face_carries_one_action_per_verb(served):
    with ikigai.connect(served) as k:
        description = k.describe("urn:py:note:x")
    assert description["verbs"] == ["Source", "Sink", "Delete", "Meta"]
    assert description["inputs"] == [] and description["outputs"] == []  # all per-action
    actions = {a["verb"]: a for a in description["actions"]}
    assert list(actions) == ["Source", "Sink", "Delete"]
    assert actions["Source"]["summary"] == "read a note"
    assert [(i["name"], i["source"]) for i in actions["Sink"]["inputs"]] == [
        ("key", "binding"),
        ("content", "argument"),
    ]
    for action in actions.values():
        assert action["outputs"] == ["text/plain;charset=utf-8"]
        assert action["inputs"][0]["name"] == "key"  # every action names the binding


def test_the_turtle_face_scopes_inputs_per_action(served):
    with ikigai.connect(served) as k:
        ttl = k.meta("urn:py:note:x").text
    assert 'ik:verb "Source", "Sink", "Delete", "Meta"' in ttl
    assert "ik:action <urn:ikigai:endpoint:note:action:sink>" in ttl
    assert "<urn:ikigai:endpoint:note:action:sink> a ik:Action" in ttl
    assert '<urn:ikigai:endpoint:note:action:sink:input:content> ik:inputName "content"' in ttl
    assert '<urn:ikigai:endpoint:note:action:delete:input:key> ik:inputName "key"' in ttl
    assert 'ik:source "binding"' in ttl
    assert "action:meta" not in ttl  # Meta is never a selectable action
    assert "action:exists" not in ttl  # the default Exists is not declared


def test_a_custom_exists_replaces_the_default(socket_dir):
    calls = []
    f = family("urn:py:probe:{n}", id="probe")

    @f.source
    def read(n: int) -> str:
        calls.append(n)
        return str(n)

    @f.exists(cacheable=True)
    def present(n: int) -> bool:
        return n % 2 == 0

    space = Space([f])

    def issue(verb, target):
        return space.dispatch(Issue(Request(verb, target, {})))

    assert issue(Verb.EXISTS, "urn:py:probe:2").representation.text == "true"
    reply = issue(Verb.EXISTS, "urn:py:probe:3")
    assert reply.representation.text == "false"
    assert reply.representation.expiry.kind == "never"
    assert calls == []  # the Source handler never ran
    assert f.description_json()["verbs"] == ["Source", "Exists", "Meta"]


# -- bindings: typing and the one-spelling rule -----------------------------


def int_family():
    f = family("urn:py:at:{x}:{y}", id="at")

    @f.source
    def read(x: int, y: Annotated[int, "the row"]) -> str:
        assert type(x) is int and type(y) is int
        return f"{x + y}"

    return f


@pytest.mark.parametrize("good", ["0", "7", "-1", "40", "123456789012345678901234567890"])
def test_an_int_binding_takes_its_plain_spelling(good):
    reply = Space([int_family()]).dispatch(Issue(Request(Verb.SOURCE, f"urn:py:at:{good}:0", {})))
    assert reply.representation.text == str(int(good))


@pytest.mark.parametrize("bad", ["01", "+1", "-0", "00", " 1", "1_0", "١", "x", "1.0"])
def test_an_int_binding_refuses_every_other_spelling(bad):
    reply = Space([int_family()]).dispatch(Issue(Request(Verb.SOURCE, f"urn:py:at:{bad}:0", {})))
    error = reply.error
    assert isinstance(error, ikigai.InvalidArgumentError)
    assert error.name == "x"
    assert error.detail == f"`{bad}` is not an integer in its plain form (e.g. 0, 2, -1)"


def test_an_int_binding_is_declared_xsd_integer():
    [source] = int_family().description_json()["actions"]
    x, y = source["inputs"]
    assert x == {
        "name": "x",
        "summary": "",
        "required": True,
        "source": "binding",
        "class": XSD_INTEGER,
    }
    assert y["summary"] == "the row"


def test_a_binding_absorbed_by_kwargs_is_declared_untyped():
    f = family("urn:py:kw:{a}", id="kw")

    @f.source
    def read(**kwargs) -> str:
        return kwargs["a"]

    reply = Space([f]).dispatch(Issue(Request(Verb.SOURCE, "urn:py:kw:z", {})))
    assert reply.representation.text == "z"
    assert f.description_json()["actions"][0]["inputs"][0]["source"] == "binding"


# -- declaration errors -----------------------------------------------------


def test_a_variable_no_parameter_receives_fails_at_declaration():
    f = family("urn:py:v:{x}:{y}", id="v")
    with pytest.raises(TypeError, match=r"template variable `y` matches no parameter of read\(\)"):

        @f.source
        def read(x: int) -> str:
            return ""


def test_a_declared_arg_named_like_a_variable_fails_at_declaration():
    with pytest.raises(TypeError, match="declared arg `x` is also a variable of the template"):

        @endpoint("urn:py:w:{x}", args=["x"])
        def w(x: str) -> str:
            return x


def test_explicit_args_beside_bindings():
    @endpoint("urn:py:e:{x}", args=[{"name": "n", "required": False, "default": "1"}])
    def e(x: int, n: str = "1") -> str:
        return f"{x}:{n}"

    inputs = e.ikigai_endpoint.args
    assert [(i.name, i.source) for i in inputs] == [("x", "binding"), ("n", "argument")]
    reply = Space([e]).dispatch(Issue(Request(Verb.SOURCE, "urn:py:e:5", {})))
    assert reply.representation.text == "5:1"  # the binding is typed; the arg is wire text


def test_a_verb_declared_twice_fails():
    f, _ = notes_family()
    with pytest.raises(ValueError, match=r"Source is already declared \(by read\(\)\)"):

        @f.source
        def again(key: str) -> str:
            return ""


def test_a_write_cannot_be_declared_cacheable():
    f = family("urn:py:c:{k}", id="c")
    with pytest.raises(TypeError):
        f.sink(cacheable=True)  # not even a parameter
    with pytest.raises(ValueError, match="never cacheable"):
        from ikigai.serve import Action
        from ikigai.template import UriTemplate

        Action(Verb.DELETE, lambda k: None, UriTemplate("urn:py:c:{k}"), "t", cacheable=True)


def test_a_family_with_no_verbs_is_refused():
    with pytest.raises(ValueError, match="declares no verb"):
        Space([family("urn:py:empty:{x}", id="empty")])


def test_a_malformed_template_fails_at_declaration():
    with pytest.raises(ikigai.TemplateError, match="adjacent variables"):
        family("urn:py:{a}{b}", id="bad")


# -- ordering: first declared wins ------------------------------------------


def test_the_first_declared_template_wins():
    @endpoint("urn:py:o:{a}:{b}")
    def pair(a: str, b: str) -> str:
        return "pair"

    @endpoint("urn:py:o:{a}")
    def single(a: str) -> str:
        return "single"

    def answer(space, target):
        return space.dispatch(Issue(Request(Verb.SOURCE, target, {}))).representation.text

    # `pair` first: a two-part name is a pair, a one-part name falls to `single`
    space = Space([pair, single])
    assert answer(space, "urn:py:o:1:2") == "pair"
    assert answer(space, "urn:py:o:1") == "single"
    # `single` first: its trailing variable takes `1:2` whole
    space = Space([single, pair])
    assert answer(space, "urn:py:o:1:2") == "single"


def test_an_exact_door_shadowed_by_an_earlier_template_is_refused():
    @endpoint("urn:py:s:{name}")
    def general(name: str) -> str:
        return name

    @endpoint("urn:py:s:special")
    def special() -> str:
        return "special"

    with pytest.raises(ValueError, match="special can never answer urn:py:s:special"):
        Space([general, special])
    Space([special, general])  # the specific door first is fine


def test_two_identical_templates_are_refused():
    a, _ = notes_family()
    b, _ = notes_family()
    with pytest.raises(ValueError, match=r"two endpoints answer urn:py:note:\{key\}"):
        Space([a, b])
