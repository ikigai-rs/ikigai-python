"""Parity with core's content-addressed contracts and match IRIs (ledger #948, #1034, #1106).

Every expectation here comes from ``tests/vectors/contract_vectors.json``, a VERBATIM copy
of ikigai-vocab's ``tests/vectors/contract_vectors.json``: core computes that file and its
own test refuses to let it drift, and devtools' ``vector-drift`` compares this copy with
it. Nothing is asserted from this face's own output, so a mirror that drifts from core
fails here. To pick up new vectors, copy the file again; never edit or reformat it.

The one face-local check: this face's API still builds the descriptions the shared file
carries as its ``python/…`` doors (``tests/contract_doors.py``).
"""

from __future__ import annotations

import doctest
import json
from pathlib import Path

import pytest

import ikigai.contract
from ikigai import Verb
from ikigai.contract import (
    canonical_contract_iri,
    canonical_match_iri,
    contract_id,
    contract_iri,
    match_iri,
    parse_contract_iri,
    parse_match_iri,
)
from ikigai.serve import action_matches, to_turtle
from tests.contract_doors import doors

VECTORS_FILE = Path(__file__).parent / "vectors" / "contract_vectors.json"
VECTORS = json.loads(VECTORS_FILE.read_text(encoding="utf-8"))
DOORS = VECTORS["doors"]
CONTRACTS = VECTORS["contracts"]
MATCHES = VECTORS["matches"]
PARSES = VECTORS["parses"]
DOOR = {d["name"]: d for d in DOORS}
CONTRACT = {c["name"]: c for c in CONTRACTS}

PYTHON = "python/"


def _verb(name):
    return None if name is None else Verb[name.upper()]


def test_the_vectors_are_for_the_canonical_form_this_face_computes():
    # Core bumps the form tag if the canonical form changes; a stale mirror should fail
    # here, by name, rather than as sixty digest mismatches below.
    assert VECTORS["form"] == ikigai.contract._CONTRACT_FORM


def test_the_vectors_name_the_v2_form_this_face_computes():
    assert VECTORS["form_v2"] == ikigai.contract._CONTRACT_FORM_V2


def test_a_contract_is_digested_under_the_form_its_content_chooses():
    # The v2 rows are the proof: each uses a construct v1 cannot carry. And the one v1
    # row beside them (`to-upper`) proves a contract that uses none is untouched.
    v2 = [c for c in CONTRACTS if c["name"].startswith("v2/")]
    assert len(v2) >= 12
    assert all(ikigai.contract._uses_v2(c["action"]) for c in v2)
    assert not ikigai.contract._uses_v2(CONTRACT["to-upper (the renamed builtin id)"]["action"])


def test_the_vectors_carry_every_door_this_face_builds():
    carried = {d["name"][len(PYTHON) :] for d in DOORS if d["name"].startswith(PYTHON)}
    assert carried == set(doors())


@pytest.mark.parametrize("name", sorted(doors()))
def test_this_faces_api_still_builds_the_description_the_vectors_carry(name):
    door, vector = doors()[name], DOOR[PYTHON + name]
    assert door.iri == vector["pattern"]
    assert door.description_json() == vector["description"], (
        f"{name}: the door changed since core computed the vectors; change it in core's "
        "contract_cases.json too, regenerate there, and copy the file again"
    )
    # The methods a served door uses are the functions below, over that description.
    assert door.description_turtle() == vector["turtle"]
    assert door.action_matches() == vector["actions"]


@pytest.mark.parametrize("case", DOORS, ids=lambda c: c["name"])
def test_a_door_renders_its_meta_turtle_exactly_as_core_does(case):
    assert to_turtle(case["description"]) == case["turtle"]


@pytest.mark.parametrize("case", DOORS, ids=lambda c: c["name"])
def test_a_door_names_its_matches_and_contracts_as_core_does(case):
    assert action_matches(case["pattern"], case["description"]) == case["actions"]


@pytest.mark.parametrize("case", DOORS, ids=lambda c: c["name"])
def test_no_door_mints_the_retired_action_iri(case):
    # urn:ikigai:endpoint:{id}:action:{verb} MOVED in core 0.1.91 (not aliased: an
    # alias would re-merge different contracts under one subject).
    assert ":action:" not in to_turtle(case["description"])


def test_identical_contracts_share_a_node_and_different_ones_do_not():
    # examples.hello and demo.hello declare the same contract (derived vs explicit
    # args); the two `reverse`s do not (`text` vs `in`).
    def source(name):
        return DOOR[PYTHON + name]["actions"][0]["contract"]

    assert source("examples.hello") == source("demo.hello")
    assert source("examples.reverse") != source("demo.reverse")


@pytest.mark.parametrize("case", CONTRACTS, ids=lambda c: c["name"])
def test_a_contract_iri_is_cores(case):
    iri = contract_iri(case["id"], case["action"])
    assert iri == case["contract"]
    assert iri.endswith(":" + contract_id(case["id"], case["action"]))


@pytest.mark.parametrize("case", CONTRACTS, ids=lambda c: c["name"])
def test_a_contract_iri_parses_back_to_what_made_it(case):
    assert parse_contract_iri(case["contract"]) == (
        case["id"],
        _verb(case["action"]["verb"]),
        contract_id(case["id"], case["action"]),
    )


def test_declaration_order_and_duplicates_never_move_the_digest():
    a = CONTRACT["set-order-a"]["contract"]
    assert CONTRACT["set-order-b (same digest as a)"]["contract"] == a
    # The id is part of the contract.
    assert CONTRACT["empty-source"]["contract"] != CONTRACT["empty-source-other-id"]["contract"]


@pytest.mark.parametrize("case", MATCHES, ids=lambda c: c["pattern"])
def test_a_match_iri_is_cores(case):
    for row in case["iris"]:
        verb = _verb(row["verb"])
        assert match_iri(verb, case["pattern"]) == row["match"]
        assert parse_match_iri(row["match"]) == (verb, case["pattern"])


@pytest.mark.parametrize("case", PARSES, ids=lambda c: c["iri"][:60])
def test_parsing_an_iri_agrees_with_core(case):
    want = case["match"]
    assert parse_match_iri(case["iri"]) == (None if want is None else (_verb(want[0]), want[1]))
    want = case["contract"]
    assert parse_contract_iri(case["iri"]) == (
        None if want is None else (want[0], _verb(want[1]), want[2])
    )


@pytest.mark.parametrize("case", PARSES, ids=lambda c: c["iri"][:60])
def test_what_this_face_says_back_is_cores_canonical_spelling(case):
    # Lenient in, canonical out (ledger #1107): any spelling that parses is said back
    # as the one core emits, and that spelling is a fixed point.
    said = canonical_match_iri(case["iri"]) or canonical_contract_iri(case["iri"])
    assert said == case["canonical"]
    if said is not None:
        assert (canonical_match_iri(said) or canonical_contract_iri(said)) == said
        assert parse_match_iri(said) == parse_match_iri(case["iri"])
        assert parse_contract_iri(said) == parse_contract_iri(case["iri"])


@pytest.mark.parametrize("escape", ["%+1", "%+F", "%-1", "a%+1b", "% 1", "%_1"])
def test_a_signed_or_spaced_escape_is_refused(escape):
    # ledger #1096: `%+1` used to decode to the byte `%01` does, so two IRIs named one
    # door. An escape is `%` and exactly two hex digits.
    assert parse_match_iri(f"urn:ikigai:match:source:{escape}") is None
    assert parse_contract_iri(f"urn:ikigai:contract:{escape}:source:b3:{'ab' * 32}") is None


@pytest.mark.parametrize(
    "name",
    [
        "http://[::1]/x",  # ledger #1361: a name's brackets stay in the path
        "a\u0085b",  # NEL, a C1 control: outside ucschar
        "del\x7f nel\x85 pua\ue000 nonchar\ufdd0\uffff",
        "a#b?c#d",  # ledger #1390: a name is one path segment
        "100%",  # `%` is a character in a name, never an escape
    ],
)
def test_a_name_round_trips_through_encode_and_parse(name):
    # The encoder is injective (`%` itself is encoded), so a match IRI and a contract
    # IRI parse back to exactly the name that made them, whatever the name holds.
    from ikigai.contract import _decode_segment, encode_name

    assert _decode_segment(encode_name(name)) == name
    match = match_iri(Verb.SOURCE, name)
    assert parse_match_iri(match) == (Verb.SOURCE, name)
    assert canonical_match_iri(match) == match
    contract = contract_iri(name, {"verb": "Source"})
    assert parse_contract_iri(contract) == (
        name,
        Verb.SOURCE,
        contract_id(name, {"verb": "Source"}),
    )
    assert canonical_contract_iri(contract) == contract


def test_the_name_encoder_encodes_percent_and_the_new_classes():
    # The rule core's `encode_name` follows (ledger #1345, #1361, #1390): `%`, what a
    # Turtle IRIREF cannot carry, `[`/`]`, `#`/`?`, and every non-`ucschar` code point.
    from ikigai.contract import encode_name

    assert encode_name("toUpper") == "toUpper"
    assert encode_name("naïve") == "naïve"  # ordinary Unicode is written as it is
    assert encode_name("50% off") == "50%25%20off"
    assert encode_name("http://[::1]/x") == "http://%5B::1%5D/x"
    assert encode_name("a\x7fb\x85c") == "a%7Fb%C2%85c"
    assert encode_name("\ue000\uffff") == "%EE%80%80%EF%BF%BF"
    assert encode_name("a#b?c") == "a%23b%3Fc"


def test_the_contract_module_doctests_hold():
    # A doc comment that states a shape leaving the process is only trusted if it runs.
    failed, attempted = doctest.testmod(ikigai.contract)
    assert attempted and not failed
