"""The cases ``tests/fixtures/contract_vectors.json`` was computed from (ledger #1034).

Each DOOR case is a real door's description as this face serves it (its JSON Meta
face, the one a Rust host deserializes when it mounts this peer); each CONTRACT case
is a bare ``ikigai_core::ActionSpec`` aimed at a corner of the canonical form. The
fixture holds what CORE computed from them: ``ikigai_vocab::to_turtle`` of the
description, and per action ``ikigai_core::match_iri`` and
``ActionSpec::contract_iri``. ``tests/test_contract.py`` asserts this face computes
the same bytes.

To regenerate (after a change to a door below, or a new core contract form), print
the cases and feed them to the generator in ``tests/fixtures/contract_vectors.rs``::

    python -m tests.contract_cases > cases.json
    # in a scratch cargo project holding that generator (its header says how):
    cargo run -q --release < cases.json > tests/fixtures/contract_vectors.json
"""

from __future__ import annotations

import json
import sys
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
    """Every door case, by name."""
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


# -- bare contracts ----------------------------------------------------------

_IN = {"name": "in", "summary": "", "required": True, "source": "argument"}

#: (id, ActionSpec) pairs aimed at the canonical form's corners. Pairs meant to share
#: a digest sit next to each other and say so; the fixture records what core says.
CONTRACTS: dict[str, tuple[str, dict]] = {
    "empty-source": ("toUpper", {"verb": "Source"}),
    "empty-source-other-id": ("toLower", {"verb": "Source"}),
    "empty-sink": ("toUpper", {"verb": "Sink"}),
    "empty-meta": ("toUpper", {"verb": "Meta"}),
    "set-order-a": (
        "e",
        {
            "verb": "Source",
            "inputs": [
                {**_IN, "name": "m", "one_of": ["x", "y"]},
                {**_IN, "name": "locale", "required": False},
            ],
            "outputs": ["a/b", "c/d"],
            "requires": ["urn:cap:a", "urn:cap:b"],
        },
    ),
    "set-order-b (same digest as a)": (
        "e",
        {
            "verb": "Source",
            "inputs": [
                {**_IN, "name": "locale", "required": False},
                {**_IN, "name": "m", "one_of": ["y", "x", "y"]},
                {**_IN, "name": "locale", "required": False},
            ],
            "outputs": ["c/d", "a/b", "a/b"],
            "requires": ["urn:cap:b", "urn:cap:a", "urn:cap:b"],
        },
    ),
    "same-name-inputs": (
        "e",
        {
            "verb": "Delete",
            "inputs": [
                {**_IN, "class": "urn:c"},
                {**_IN},
                {**_IN, "source": "binding"},
                {**_IN, "default": "", "required": False},
            ],
        },
    ),
    "every-field": (
        "urn:cms:graph",
        {
            "verb": "Exists",
            "summary": "s",
            "inputs": [
                {
                    "name": "k",
                    "summary": "a key",
                    "required": False,
                    "source": "binding",
                    "class": "http://www.w3.org/2001/XMLSchema#integer",
                    "default": "0",
                    "one_of": ["0", "1"],
                }
            ],
            "outputs": ["text/plain;charset=utf-8"],
            "requires": ["urn:cap:cms:read", "legacy"],
        },
    ),
    "hostile-id": ("ev>il %25 a:b é", {"verb": "Sink", "summary": "ünïcode ✓"}),
    "empty-id": ("", {"verb": "Source"}),
}

#: Door patterns whose match IRIs the fixture pins beyond the doors' own.
PATTERNS = [
    "urn:iki:fn:toUpper",
    "urn:file:{path}",
    "urn:a:%7B",
    "urn:a:{",
    'urn:sp ace:<>"|^`\\:naïve',
]

_HEX = "ab" * 32

#: IRIs the fixture pins core's PARSE of (``parse_match_iri``/``parse_contract_iri``),
#: valid and not: the round trips, and the edges of the percent and hex decoding.
PARSES = [
    "urn:ikigai:match:source:urn:file:%7Bpath%7D",
    "urn:ikigai:match:meta:urn:a:%25",
    "urn:ikigai:match:Source:urn:x",
    "urn:ikigai:match:frobnicate:urn:x",
    "urn:ikigai:match:source",
    "urn:ikigai:match:source:%G1",
    "urn:ikigai:match:source:%4",
    "urn:ikigai:match:source:%+1",
    "urn:ikigai:match:source:%C3%A9",
    "urn:ikigai:match:source:%C3",
    f"urn:ikigai:contract:urn:cms:graph:source:b3:{_HEX}",
    f"urn:ikigai:contract:ev%3Eil:delete:b3:{_HEX.upper()}",
    f"urn:ikigai:contract:x:source:b3:{_HEX[:-2]}",
    f"urn:ikigai:contract:x:source:b3:+{_HEX[1:]}",
    f"urn:ikigai:contract::sink:b3:{_HEX}",
    f"urn:ikigai:contract:sink:b3:{_HEX}",
    "urn:ikigai:endpoint:x:action:source",
]


def generator_input() -> list[dict]:
    """The JSON array the Rust generator reads (``{"name", "pattern",
    "description"}`` per case)."""
    out = []
    for name, door in doors().items():
        out.append({"name": name, "pattern": door.iri, "description": door.description_json()})
    for name, (endpoint_id, spec) in CONTRACTS.items():
        description = {
            "id": endpoint_id,
            "title": "",
            "summary": "",
            "verbs": [spec["verb"]],
            "inputs": [],
            "outputs": [],
            "actions": [spec],
        }
        out.append({"name": name, "pattern": "", "description": description, "contract": True})
    for i, pattern in enumerate(PATTERNS):
        description = {
            "id": f"pattern-{i}",
            "title": "",
            "summary": "",
            "verbs": ["Source", "Sink", "Exists", "Delete", "Meta"],
            "inputs": [],
            "outputs": [],
        }
        out.append(
            {"name": f"pattern-{i}", "pattern": pattern, "description": description, "match": True}
        )
    for i, iri in enumerate(PARSES):
        out.append({"name": f"parse-{i}", "pattern": iri, "parse": True})
    return out


if __name__ == "__main__":
    json.dump(generator_input(), sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
