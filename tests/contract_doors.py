"""This face's own doors, as its API builds them (ledger #1034, #1106).

The shared vectors (``tests/vectors/contract_vectors.json``, ikigai-vocab's file copied
verbatim) carry each of these doors as ``python/<name>``: its JSON Meta face, the
description a Rust host deserializes when it mounts this peer, and what core computes
from it. ``tests/test_contract.py`` asserts the API still builds exactly that
description, which is the one thing the shared file cannot pin for this face.

A door changed here, or a new one, needs the same change in ikigai-core's
``crates/ikigai-vocab/tests/vectors/contract_cases.json`` (the README beside it says how
to regenerate), then a fresh verbatim copy of the vectors.
"""

from __future__ import annotations

from typing import Annotated, Literal

from examples import endpoints as example_endpoints
from examples.tictactoe_store import CellStore, stored_cell
from ikigai import demo, endpoint, family

# -- doors -------------------------------------------------------------------


@endpoint("urn:py:echo/{message}", summary="Echo the name back")
def _echo(message: Annotated[str, "what to echo"], suffix: str = "") -> str:
    return message + suffix


def _notes():
    f = family("urn:py:note:{key}", id="note", title="Notes", summary="A key-value note")

    @f.source(cacheable=True, summary="read a note")
    def read(key: str) -> str:
        return key

    @f.sink(summary="write a note")
    def write(key: str, content: str) -> None:
        return None

    @f.delete(summary="forget a note")
    def forget(key: str):
        return None

    return f


@endpoint(
    "urn:py:guarded",
    id="guard>ed %41 naïve",
    title='A "quoted"\ttitle',
    summary="line one\nline two\\",
    requires=["urn:cap:py:read", "legacy label", "urn:cap:py:a"],
    output="text/turtle",
)
def _guarded(
    mode: Literal["b", "a", "c"] = "a",
    count: Annotated[int, "how many"] = 3,
    when: str = "now",
) -> str:
    return mode


def _shuffled():
    """A family whose contracts are declared in an unusual order: explicit args,
    out of name order, per-verb requires, a declared Exists."""
    f = family("urn:py:shuffled:{a}:{b}", id="urn:py:shuffled")

    @f.source(
        args=[
            {"name": "zeta", "required": False, "one_of": ["y", "x", "y"]},
            {"name": "alpha", "summary": "first", "default": "1"},
        ],
        requires=["urn:cap:py:z", "urn:cap:py:a", "urn:cap:py:z"],
    )
    def read(a: str, b: str, **kwargs) -> str:
        return a

    @f.exists(cacheable=True, requires=["urn:cap:py:a"])
    def present(a: str, b: str) -> bool:
        return True

    return f


def doors() -> dict:
    """Every door this face carries in the shared vectors, by name (there, `python/<name>`)."""
    return {
        "examples.hello": example_endpoints.hello.ikigai_endpoint,
        "examples.upper": example_endpoints.upper.ikigai_endpoint,
        "examples.reverse": example_endpoints.reverse.ikigai_endpoint,
        "demo.hello": demo.hello.ikigai_endpoint,
        "demo.reverse": demo.reverse.ikigai_endpoint,
        "echo": _echo.ikigai_endpoint,
        "note": _notes(),
        "ttt-stored": stored_cell(CellStore()),
        "guarded": _guarded.ikigai_endpoint,
        "shuffled": _shuffled(),
    }
