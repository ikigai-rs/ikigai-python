"""The core 0.3.5 failure classes against the shared vectors (ledger #1896, #1546).

``tests/vectors/failure_class_vectors.json`` is a VERBATIM copy of ikigai-vocab's
``tests/vectors/failure_class_vectors.json`` (core main). To pick up new
rows, copy that file again, unedited; it stays ``eol=lf`` through ``.gitattributes``.

Every case pins the error faces and taxonomy here: ``kind``, ``class`` (with
``transient``), ``display``,
the wire tag (RECEIVE side: bytes built below by hand, never by this face's encoder) and
the edge status with its ``Retry-After`` (``examples.error_status``), JSON round trips,
and Turtle counts.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import ikigai
from examples import error_status, retry_after_header
from ikigai import wire
from ikigai.postcard import encode_varint

VECTORS = json.loads(
    (Path(__file__).parent / "vectors" / "failure_class_vectors.json").read_text(encoding="utf-8")
)
CASES = VECTORS["cases"]
CLASSES = {
    "transient",
    "retry-after",
    "over-budget",
    "denied",
    "refused",
    "permanent",
    "unknown",
}


def s(text: str) -> bytes:
    raw = text.encode()
    return encode_varint(len(raw)) + raw


def opt(payload: bytes | None) -> bytes:
    return b"\x00" if payload is None else b"\x01" + payload


def the_error(case: dict) -> ikigai.EndpointError:
    """The case's error as this face spells it (not through the wire)."""
    e = case["error"]
    match e["kind"]:
        case "unresolved":
            return ikigai.UnresolvedError(e["iri"])
        case "missing-argument":
            return ikigai.MissingArgumentError(e["name"])
        case "invalid-argument":
            return ikigai.InvalidArgumentError(e["name"], e["detail"])
        case "endpoint":
            return ikigai.EndpointError(e["message"])
        case "denied":
            return ikigai.DeniedError(e["message"])
        case "not-found":
            return ikigai.NotFoundError(e["message"])
        case "conflict":
            return ikigai.ConflictError(e["message"])
        case "timeout":
            return ikigai.TimeoutError(e["message"])
        case "unavailable":
            return ikigai.UnavailableError(e["message"])
        case "depth-exceeded":
            return ikigai.DepthExceededError(e["depth"], e["target"])
        case "retry-after":
            return ikigai.RetryAfterError(case["retry_after_ms"], e["detail"])
        case "gave-up":
            return ikigai.GaveUpError(e["attempts"], e["elapsed_us"] // 1000, e["detail"])
        case "over-budget":
            return ikigai.OverBudgetError(e["detail"])
        case "refused":
            return ikigai.RefusedError(e["message"])
        case "unsupported":
            verb = ikigai.Verb[e["verb"].upper()] if "verb" in e else None
            return ikigai.UnsupportedError(e["detail"], verb)
    raise AssertionError(e)


def the_payload(case: dict) -> bytes:
    """The case's WireError payload after its tag, written by hand (layouts: core's field
    order, strings and varints as postcard has them)."""
    e = case["error"]
    match e["kind"]:
        case "unresolved":
            return s(e["iri"])
        case "missing-argument":
            return s(e["name"])
        case "invalid-argument":
            return s(e["name"]) + s(e["detail"])
        case "depth-exceeded":
            return encode_varint(e["depth"]) + s(e["target"])
        case "retry-after":
            ms = case["retry_after_ms"]
            return opt(None if ms is None else encode_varint(ms)) + s(e["detail"])
        case "gave-up":
            return encode_varint(e["attempts"]) + encode_varint(case["elapsed_ms"]) + s(e["detail"])
        case "over-budget":
            return s(e["detail"])
        case "unsupported":
            verb = ikigai.Verb[e["verb"].upper()] if "verb" in e else None
            return opt(None if verb is None else encode_varint(int(verb))) + s(e["detail"])
        case _:
            return s(e["message"])


def test_the_vectors_have_rows_and_every_class():
    assert len(CASES) == 22
    assert {c["word"] for c in VECTORS["classes"]} == CLASSES
    assert {c["class"] for c in CASES} == CLASSES


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["name"])
def test_kind_class_transient_and_display(case):
    error = the_error(case)
    assert error.kind == case["kind"]
    assert error.failure_class() == case["class"]
    assert error.transient is case["transient"]
    assert error.display() == case["display"]


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["name"])
def test_the_wire_tag_decodes_to_the_same_error(case):
    payload = encode_varint(case["wire_tag"]) + the_payload(case)
    got = wire.decode_reply(b"\x05" + payload).error
    assert got == the_error(case)
    assert type(got) is type(the_error(case))
    assert (got.kind, got.failure_class(), got.display()) == (
        case["kind"],
        case["class"],
        case["display"],
    )


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["name"])
def test_the_edge_status_and_retry_after(case):
    error = the_error(case)
    assert error_status(error) == case["http"]["status"]
    header = retry_after_header(error)
    expected = case["http"]["retry_after"]
    assert header == ({} if expected is None else {"Retry-After": str(expected)})


def test_retry_after_is_not_transient_and_in_its_own_class():
    error = ikigai.RetryAfterError(1000, "rate-limited")
    assert error.transient is False
    assert error.failure_class() == "retry-after"
    assert ikigai.TimeoutError("x").transient is True


def test_an_unknown_tag_still_decodes_as_endpoint():
    got = wire.decode_reply(b"\x05" + encode_varint(15) + s("whatever")).error
    assert type(got) is ikigai.EndpointError
    assert "unknown wire error variant 15" in got.message


def test_a_malformed_v9_payload_is_a_protocol_error():
    with pytest.raises(wire.ProtocolError):
        wire.decode_reply(b"\x05" + encode_varint(13) + b"\x01\x09" + s("x"))  # verb 9
    with pytest.raises(wire.ProtocolError):
        wire.decode_reply(b"\x05" + encode_varint(10))  # truncated


@pytest.mark.parametrize("case", [c for c in CASES if c["wire_tag"] >= 9], ids=lambda c: c["name"])
def test_a_v8_peer_gets_endpoint_display_for_a_v9_tag(case):
    # A variant a v8 peer has no tag for crosses as Endpoint(display), what it has always
    # received for it (the vectors' own `display` note).
    error = the_error(case)
    for peer in (7, 8):
        sent = wire.decode_reply(
            wire.encode_reply(wire.ErrorTypedReply(error), peer_version=peer)
        ).error
        assert sent == ikigai.EndpointError(case["display"])


@pytest.mark.parametrize("case", [c for c in CASES if c["wire_tag"] >= 9], ids=lambda c: c["name"])
def test_a_v9_peer_gets_the_typed_tag_and_it_round_trips(case):
    error = the_error(case)
    raw = wire.encode_reply(wire.ErrorTypedReply(error), peer_version=9)
    assert raw[:2] == b"\x05" + bytes([case["wire_tag"]])
    assert wire.decode_reply(raw).error == error


def test_errors_with_different_payloads_are_not_equal():
    assert ikigai.RetryAfterError(1, "x") != ikigai.RetryAfterError(2, "x")
    assert ikigai.UnsupportedError("x") != ikigai.UnsupportedError("x", ikigai.Verb.SINK)
    assert len({ikigai.RetryAfterError(1, "x"), ikigai.RetryAfterError(1, "x")}) == 1


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["name"])
def test_json_face_matches_core_and_reads_back(case):
    error = the_error(case)
    assert error.to_json() == case["json"]
    assert ikigai.EndpointError.from_json(case["json"]) == error
    assert getattr(error, "attempts", None) == case["attempts"]
    assert getattr(error, "elapsed_ms", None) == case["elapsed_ms"]


def test_gave_up_turtle_matches_core_documented_example():
    error = ikigai.GaveUpError(9, 120000, "unavailable: down")
    assert error.to_turtle() == (
        "@prefix ik: <https://ikigai-rs.dev/ns#> .\n"
        "@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .\n\n"
        "[] a ik:Failure ;\n    "
        'ik:failureKind "gave-up" ;\n    '
        "ik:failureClass ik:TransientFailure ;\n    "
        'ik:failureMessage "gave up after 9 attempts over 120000 ms: unavailable: down" ;\n    '
        'ik:attempts "9"^^xsd:nonNegativeInteger ;\n    '
        'ik:elapsedMillis "120000"^^xsd:nonNegativeInteger .\n'
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("attempts", -1),
        ("attempts", 2**32),
        ("attempts", True),
        ("attempts", "nine"),
        ("elapsed_ms", -1),
        ("elapsed_ms", 2**64),
        ("elapsed_ms", False),
        ("elapsed_ms", 1.5),
        ("detail", None),
    ],
)
def test_gave_up_json_refuses_invalid_fields(field, value):
    face = {"kind": "gave-up", "attempts": 1, "elapsed_ms": 1, "detail": "down"}
    face[field] = value
    with pytest.raises(ikigai.InvalidArgumentError) as caught:
        ikigai.EndpointError.from_json(face)
    assert caught.value.name == field


@pytest.mark.parametrize("field", ["attempts", "elapsed_ms", "detail"])
def test_gave_up_json_requires_its_fields(field):
    face = {"kind": "gave-up", "attempts": 1, "elapsed_ms": 1, "detail": "down"}
    del face[field]
    with pytest.raises(ikigai.InvalidArgumentError) as caught:
        ikigai.EndpointError.from_json(face)
    assert caught.value.name == field


def test_gave_up_counts_survive_json_wire_and_equality():
    error = ikigai.GaveUpError(2**32 - 1, 2**64 - 1, 'down "now"\\\n\r\t')
    assert wire.decode_reply(wire.encode_reply(wire.ErrorTypedReply(error))).error == error
    assert ikigai.EndpointError.from_json(error.to_json()) == error
    assert error != ikigai.GaveUpError(1, error.elapsed_ms, error.detail)
    assert error != ikigai.GaveUpError(error.attempts, 1, error.detail)
    assert len({error, ikigai.EndpointError.from_json(error.to_json())}) == 1
    assert 'down \\"now\\"\\\\\\n\\r\\t' in error.to_turtle()


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["name"])
def test_turtle_describes_each_failure_class_and_its_counts(case):
    error = the_error(case)
    turtle = error.to_turtle()
    class_name = "".join(part.capitalize() for part in case["class"].split("-"))
    assert f"ik:failureClass ik:{class_name}Failure" in turtle
    assert f'ik:failureKind "{case["kind"]}"' in turtle
    for key, predicate in [
        ("attempts", "attempts"),
        ("elapsed_ms", "elapsedMillis"),
        ("retry_after_ms", "retryAfterMillis"),
    ]:
        count = case[key]
        if count is None:
            assert f"ik:{predicate} " not in turtle
        else:
            assert f'ik:{predicate} "{count}"^^xsd:nonNegativeInteger' in turtle
