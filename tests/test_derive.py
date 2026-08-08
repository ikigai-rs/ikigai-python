"""Signature-derived ArgSpecs: the derivation table, the explicit-args
override, and the served round-trip behavior of derived endpoints."""

from __future__ import annotations

import threading
import typing
from typing import Annotated, Literal, Optional, Union

import pytest

import ikigai
from ikigai.serve import ArgSpec, Server, endpoint

XSD = "http://www.w3.org/2001/XMLSchema#"


def specs(fn) -> dict[str, ArgSpec]:
    return {a.name: a for a in fn.ikigai_endpoint.args}


# ---------------------------------------------------------------------------
# The derivation table
# ---------------------------------------------------------------------------


def test_scalar_annotations_map_to_xsd_classes():
    @endpoint("urn:py:t")
    def t(a: str, b: int, c: float, d: bool, e: bytes):
        pass

    s = specs(t)
    assert s["a"].cls == XSD + "string"
    assert s["b"].cls == XSD + "integer"
    assert s["c"].cls == XSD + "double"
    assert s["d"].cls == XSD + "boolean"
    assert s["e"].cls is None  # raw bytes: accepted, no class
    assert all(spec.required for spec in s.values())


def test_missing_or_unknown_annotations_are_accepted_without_a_class():
    @endpoint("urn:py:t")
    def t(plain, weird: dict[str, int], union: Union[int, str]):  # noqa: UP007
        pass

    s = specs(t)
    assert s["plain"].cls is None and s["plain"].required
    assert s["weird"].cls is None
    assert s["union"].cls is None  # a many-typed union names no one class


def test_literal_becomes_one_of_with_the_member_type():
    @endpoint("urn:py:t")
    def t(mode: Literal["fast", "slow"], level: Literal[1, 2, 3]):
        pass

    s = specs(t)
    assert s["mode"].one_of == ["fast", "slow"]
    assert s["mode"].cls == XSD + "string"
    assert s["level"].one_of == ["1", "2", "3"]
    assert s["level"].cls == XSD + "integer"


def test_defaults_make_arguments_optional_with_the_repl_rendering():
    @endpoint("urn:py:t")
    def t(greeting: str = "Hello", times: int = 2, loud: bool = False, rate: float = 1.5):
        pass

    s = specs(t)
    assert not s["greeting"].required and s["greeting"].default == "Hello"
    assert s["times"].default == "2"
    assert s["loud"].default == "false"  # bools render true/false, not True/False
    assert s["rate"].default == "1.5"


def test_optional_annotations_are_optional():
    @endpoint("urn:py:t")
    def t(a: Optional[str], b: str | None, c: Annotated[str | None, "maybe"]):  # noqa: UP045
        pass

    s = specs(t)
    for name in ("a", "b", "c"):
        assert not s[name].required, name
        assert s[name].cls == XSD + "string", name
        assert s[name].default is None, name


def test_annotated_metadata_becomes_the_summary():
    @endpoint("urn:py:t")
    def t(who: Annotated[str, "the name to greet"], n: Annotated[int, "how many"] = 1):
        pass

    s = specs(t)
    assert s["who"].summary == "the name to greet"
    assert s["who"].cls == XSD + "string"
    assert s["n"].summary == "how many"
    assert not s["n"].required and s["n"].default == "1"


def test_trailing_underscore_maps_to_the_reserved_wire_name():
    @endpoint("urn:py:t")
    def t(in_: str, class_: str = "x", dunder__: str = "y"):
        pass

    s = specs(t)
    assert set(s) == {"in", "class", "dunder__"}  # __ is not the convention
    assert s["in"].required


def test_var_positional_and_var_keyword_declare_nothing():
    @endpoint("urn:py:t")
    def t(a: str, *rest, **kw):
        pass

    assert set(specs(t)) == {"a"}


def test_positional_only_parameters_fail_loud():
    with pytest.raises(TypeError, match="positional-only"):

        @endpoint("urn:py:t")
        def t(a: str, /):
            pass


def test_derived_face_matches_the_explicit_equivalent():
    # The derived describe face is byte-for-byte what the hand-written spec
    # dict produced — the host engine sees no difference.
    @endpoint("urn:py:hello", summary="Greet someone")
    def derived(
        who: Annotated[str, "the name to greet"],
        greeting: Annotated[str, "the salutation"] = "Hello",
    ) -> str:
        return f"{greeting}, {who}!"

    @endpoint(
        "urn:py:hello",
        id="derived",
        summary="Greet someone",
        args=[
            {
                "name": "who",
                "required": True,
                "summary": "the name to greet",
                "class": XSD + "string",
            },
            {
                "name": "greeting",
                "default": "Hello",
                "summary": "the salutation",
                "class": XSD + "string",
            },
        ],
    )
    def explicit(who: str, greeting: str = "Hello") -> str:
        return f"{greeting}, {who}!"

    assert derived.ikigai_endpoint.description_json() == explicit.ikigai_endpoint.description_json()
    assert (
        derived.ikigai_endpoint.description_turtle()
        == explicit.ikigai_endpoint.description_turtle()
    )


# ---------------------------------------------------------------------------
# Explicit args= stays the override
# ---------------------------------------------------------------------------


def test_explicit_args_win_over_the_signature():
    # Explicit wins WHOLESALE: no merging of derived facts (the declared
    # class stays, the annotation's summary does not appear).
    @endpoint(
        "urn:py:t",
        args=[{"name": "who", "required": True, "class": XSD + "token"}],
    )
    def t(who: Annotated[str, "ignored"] = "unused"):
        pass

    s = specs(t)
    assert s["who"].cls == XSD + "token"
    assert s["who"].summary == ""
    assert s["who"].required


def test_explicit_name_mismatch_fails_loud():
    with pytest.raises(TypeError, match="declared arg `whom` matches no parameter"):

        @endpoint("urn:py:t", args=["whom"])
        def t(who: str):
            pass


def test_uncovered_required_parameter_fails_loud():
    with pytest.raises(TypeError, match="required parameter `who` .* is not declared"):

        @endpoint("urn:py:t", args=[])
        def t(who: str):
            pass


def test_kwargs_handler_accepts_any_explicit_names():
    # The pre-L2 reserved-word workaround keeps working unchanged.
    @endpoint("urn:py:t", args=["in"])
    def t(**kwargs):
        return kwargs["in"]

    assert specs(t)["in"].required


def test_explicit_args_route_to_a_trailing_underscore_parameter():
    # The convention documented where `reverse` used to need **kwargs: an
    # explicit `in` spec may deliver to an `in_` parameter directly.
    @endpoint("urn:py:t", args=["in"])
    def t(in_: str):
        return in_

    assert specs(t)["in"].py_name == "in_"


# ---------------------------------------------------------------------------
# Served round trips (the derived contract at runtime)
# ---------------------------------------------------------------------------


@endpoint("urn:py:greet", summary="Greet someone")
def greet(who: Annotated[str, "the name to greet"], greeting: str = "Hello") -> str:
    return f"{greeting}, {who}!"


@endpoint("urn:py:rev", summary="Reverse a string", cacheable=True)
def rev(in_: str) -> str:
    return in_[::-1]


@endpoint("urn:py:repeat")
def repeat(text: str, times: int = 2, sep: str | None = None) -> str:
    assert isinstance(times, int)  # the signature's type, not wire text
    return (sep if sep is not None else "").join([text] * times)


@endpoint("urn:py:shout")
def shout(text: str, loud: bool = False) -> str:
    assert isinstance(loud, bool)
    return text.upper() if loud else text


@endpoint("urn:py:pace")
def pace(mode: Literal["fast", "slow"] = "slow") -> str:
    return f"going {mode}"


@pytest.fixture
def served(socket_dir):
    path = socket_dir / "derived.sock"
    server = Server([greet, rev, repeat, shout, pace], path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield path
    server.shutdown()
    thread.join(timeout=5)


def test_derived_endpoint_routes_named_args(served):
    with ikigai.connect(served) as k:
        assert k.source("urn:py:greet", who="Ada").text == "Hello, Ada!"
        assert k.source("urn:py:greet", who="Ada", greeting="Hi").text == "Hi, Ada!"
        with pytest.raises(ikigai.EndpointError, match="missing required argument `who`"):
            k.source("urn:py:greet")


def test_derived_describe_face_is_served(served):
    with ikigai.connect(served) as k:
        description = k.describe("urn:py:greet")
        inputs = {i["name"]: i for i in description["inputs"]}
        assert inputs["who"]["required"] is True
        assert inputs["who"]["summary"] == "the name to greet"
        assert inputs["who"]["class"] == XSD + "string"
        assert inputs["greeting"]["required"] is False
        assert inputs["greeting"]["default"] == "Hello"


def test_reserved_word_argument_without_kwargs(served):
    with ikigai.connect(served) as k:
        assert k.source("urn:py:rev", **{"in": "abc"}).text == "cba"
        description = k.describe("urn:py:rev")
        assert [i["name"] for i in description["inputs"]] == ["in"]


def test_wire_text_is_coerced_to_the_annotated_types(served):
    with ikigai.connect(served) as k:
        assert k.source("urn:py:repeat", text="ab", times="3").text == "ababab"
        assert k.source("urn:py:repeat", text="ab").text == "abab"  # typed default, int 2
        assert k.source("urn:py:shout", text="hi", loud="true").text == "HI"
        assert k.source("urn:py:shout", text="hi", loud="false").text == "hi"


def test_optional_without_a_default_arrives_as_none(served):
    with ikigai.connect(served) as k:
        assert k.source("urn:py:repeat", text="a", times="2", sep="-").text == "a-a"
        assert k.source("urn:py:repeat", text="a", times="2").text == "aa"  # sep=None


def test_coercion_failures_are_typed_invalid_arguments(served):
    with ikigai.connect(served) as k:
        with pytest.raises(
            ikigai.InvalidArgumentError, match="invalid argument `times`: must be an int"
        ) as e:
            k.source("urn:py:repeat", text="ab", times="lots")
        assert (e.value.name, e.value.detail) == ("times", "must be an int (got 'lots')")
        with pytest.raises(
            ikigai.InvalidArgumentError, match="invalid argument `loud`: must be `true` or `false`"
        ):
            k.source("urn:py:shout", text="hi", loud="yes")


def test_literal_membership_is_enforced(served):
    with ikigai.connect(served) as k:
        assert k.source("urn:py:pace").text == "going slow"
        assert k.source("urn:py:pace", mode="fast").text == "going fast"
        with pytest.raises(
            ikigai.InvalidArgumentError, match="invalid argument `mode`: must be one of fast, slow"
        ):
            k.source("urn:py:pace", mode="warp")


def test_unresolvable_annotations_degrade_to_untyped():
    # A forward reference to a name that never resolves must not break the
    # decorator — gradual typing, never an error.
    def t(x):
        pass

    t.__annotations__ = {"x": "NoSuchThing"}
    decorated = endpoint("urn:py:t")(t)
    with pytest.raises(NameError):
        typing.get_type_hints(t)  # proof the hints really are unresolvable
    assert specs(decorated)["x"].cls is None
    assert specs(decorated)["x"].required
