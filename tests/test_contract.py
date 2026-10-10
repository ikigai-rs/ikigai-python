"""Parity with core's content-addressed contracts and match IRIs (ledger #948, #1034).

Every expectation here comes from ``tests/fixtures/contract_vectors.json``, which
ikigai-core 0.1.92 computed (``tests/fixtures/contract_vectors.rs`` is the generator,
``tests/contract_cases.py`` the input; their headers say how to regenerate). Nothing is
asserted from this face's own output, so a mirror that drifts from core fails here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ikigai import Verb
from ikigai.contract import (
    contract_id,
    contract_iri,
    match_iri,
    parse_contract_iri,
    parse_match_iri,
)
from tests.contract_cases import CONTRACTS, doors, generator_input

FIXTURE = Path(__file__).parent / "fixtures" / "contract_vectors.json"
VECTORS = json.loads(FIXTURE.read_text(encoding="utf-8"))
BY_NAME = {case["name"]: case for case in VECTORS}

DOOR_CASES = [c for c in VECTORS if not (c.get("contract") or c.get("match") or c.get("parse"))]
CONTRACT_CASES = [c for c in VECTORS if c.get("contract")]
MATCH_CASES = [c for c in VECTORS if c.get("match")]
PARSE_CASES = [c for c in VECTORS if c.get("parse")]


def test_the_fixture_was_computed_from_these_cases():
    # A changed door or case without a regenerated fixture fails HERE, by name, rather
    # than as a confusing digest mismatch below.
    inputs = generator_input()
    assert [c["name"] for c in inputs] == [c["name"] for c in VECTORS]
    for given, vector in zip(inputs, VECTORS, strict=True):
        assert given["pattern"] == vector["pattern"], given["name"]
        if "description" in given:
            assert given["description"] == vector["description"], (
                f"{given['name']}: the case changed since core computed the fixture; "
                "regenerate it (tests/contract_cases.py says how)"
            )


@pytest.mark.parametrize("case", DOOR_CASES, ids=lambda c: c["name"])
def test_a_door_renders_its_meta_turtle_exactly_as_core_does(case):
    door = doors()[case["name"]]
    assert door.description_turtle() == case["turtle"]


@pytest.mark.parametrize("case", DOOR_CASES, ids=lambda c: c["name"])
def test_a_door_names_its_matches_and_contracts_as_core_does(case):
    door = doors()[case["name"]]
    assert door.action_matches() == case["actions"]


@pytest.mark.parametrize("case", DOOR_CASES, ids=lambda c: c["name"])
def test_no_door_mints_the_retired_action_iri(case):
    # urn:ikigai:endpoint:{id}:action:{verb} MOVED in core 0.1.91 (not aliased: an
    # alias would re-merge different contracts under one subject).
    assert ":action:" not in doors()[case["name"]].description_turtle()


def test_identical_contracts_share_a_node_and_different_ones_do_not():
    # examples.hello and demo.hello declare the same contract (derived vs explicit
    # args); the two `reverse`s do not (`text` vs `in`).
    def source(name):
        return BY_NAME[name]["actions"][0]["contract"]

    assert source("examples.hello") == source("demo.hello")
    assert source("examples.reverse") != source("demo.reverse")


@pytest.mark.parametrize("case", CONTRACT_CASES, ids=lambda c: c["name"])
def test_a_contract_iri_is_cores(case):
    endpoint_id, spec = CONTRACTS[case["name"]]
    assert contract_iri(endpoint_id, spec) == case["contract_iri"]
    assert contract_iri(endpoint_id, spec).endswith(":" + contract_id(endpoint_id, spec))


def test_declaration_order_and_duplicates_never_move_the_digest():
    a = BY_NAME["set-order-a"]["contract_iri"]
    assert BY_NAME["set-order-b (same digest as a)"]["contract_iri"] == a
    assert (
        BY_NAME["empty-source"]["contract_iri"] != BY_NAME["empty-source-other-id"]["contract_iri"]
    )  # the id is part of the contract


@pytest.mark.parametrize("case", MATCH_CASES, ids=lambda c: c["name"])
def test_a_match_iri_is_cores(case):
    for row in case["matches"]:
        verb = Verb[row["verb"].upper()]
        assert match_iri(verb, case["pattern"]) == row["match"]
        assert parse_match_iri(row["match"]) == (verb, case["pattern"])


def _verb_or_none(name):
    return None if name is None else Verb[name.upper()]


@pytest.mark.parametrize("case", PARSE_CASES, ids=lambda c: c["pattern"][:60])
def test_parsing_an_iri_agrees_with_core(case):
    want = case["parsed_match"]
    assert parse_match_iri(case["pattern"]) == (
        None if want is None else (_verb_or_none(want[0]), want[1])
    )
    want = case["parsed_contract"]
    assert parse_contract_iri(case["pattern"]) == (
        None if want is None else (want[0], _verb_or_none(want[1]), want[2])
    )


@pytest.mark.parametrize("case", CONTRACT_CASES, ids=lambda c: c["name"])
def test_a_contract_iri_parses_back_to_what_made_it(case):
    endpoint_id, spec = CONTRACTS[case["name"]]
    assert parse_contract_iri(case["contract_iri"]) == (
        endpoint_id,
        Verb[spec["verb"].upper()],
        contract_id(endpoint_id, spec),
    )
