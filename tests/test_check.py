"""The pre-flight check against core's ``ActionSpec::check`` (ledger #1547).

``tests/vectors/validation_vectors.json`` is a VERBATIM copy of ikigai-vocab's
``tests/vectors/validation_vectors.json``, which core computes and refuses to let drift.
Every row's violations, kind and path, in order, must be this face's. To pick up new rows,
copy the file again; never edit or reformat it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ikigai.check import check, join_repeats, split_list

FILE = Path(__file__).parent / "vectors" / "validation_vectors.json"
CASES = json.loads(FILE.read_text(encoding="utf-8"))["cases"]


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["name"])
def test_the_check_agrees_with_core(case):
    found = check(case["action"], [tuple(pair) for pair in case["args"]])
    assert [{"kind": v.kind, "path": v.path} for v in found] == case["violations"]
    assert (not found) == case["conforms"]


def test_the_vectors_have_rows():
    assert len(CASES) >= 39


@pytest.mark.parametrize(
    ("separator", "value", "items"),
    [
        ("comma", "minor, info,,", ["minor", "info"]),
        ("comma-or-whitespace", "urn:a, urn:b  urn:c", ["urn:a", "urn:b", "urn:c"]),
        ("newline", "a\r\nb\n\n", ["a", "b"]),
        ("whitespace", "a \x1c b", ["a", "\x1c", "b"]),  # U+001C is not Unicode White_Space
    ],
)
def test_list_splitting_is_cores(separator, value, items):
    assert split_list(separator, value) == items


def test_repeats_of_a_list_are_joined_with_its_canonical_character():
    action = {"verb": "Source", "inputs": [{"name": "graph", "list": "whitespace"}]}
    joined, repeated = join_repeats(action, [("graph", "a"), ("graph", "b")])
    assert joined == {"graph": "a b"} and repeated == []
