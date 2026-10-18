"""The contract shape of core 0.1.105 through the authoring API (ledger #1547): input
groups, lists, value notes, argument sets and ``output_from`` in the description, the
Meta faces and the served pre-flight."""

from __future__ import annotations

import pytest

from ikigai import wire
from ikigai.contract import contract_iri
from ikigai.serve import ArgSpec, InputGroup, Space, endpoint, family
from ikigai.wire import Inline, InvalidArgumentError, Issue, Request, Resolved, Verb


@endpoint(
    "urn:py:cut",
    args=[
        ArgSpec("thread", required=False, needs=["verb"]),
        ArgSpec("content", required=False),
        ArgSpec("verb", required=False),
    ],
    groups=[InputGroup.exactly_one("thread", "content", pipe="content")],
)
def cut(**kwargs) -> str:
    return "cut"


@endpoint(
    "urn:py:finding",
    args=[
        ArgSpec(
            "decision",
            one_of=["publish", "retract"],
            values={"retract": {"summary": "withdraw", "tags": ["revision"], "needs": ["revises"]}},
        ),
        ArgSpec("revises", required=False),
        ArgSpec("proposals", required=False, one_of=["minor", "info"], list="comma"),
        ArgSpec("provider", required=False, one_of=[]),
    ],
    output_from="origin",
)
def finding(**kwargs) -> str:
    return "ok"


@endpoint(
    "urn:py:intake",
    args=[ArgSpec("content", required=False), ArgSpec("email", required=False)],
    arguments={"envelope": "content"},
    output_from="email",
)
def intake(**kwargs) -> str:
    return "ok"


def issue(door, **args) -> object:
    served = door.ikigai_endpoint
    request = Request(Verb.SOURCE, served.iri, {k: Inline(v.encode()) for k, v in args.items()})
    return Space([served]).dispatch(Issue(request))


def refusal(reply) -> InvalidArgumentError:
    assert isinstance(reply, wire.ErrorTypedReply), reply
    assert isinstance(reply.error, InvalidArgumentError), reply.error
    return reply.error


def test_the_description_carries_the_new_shape_in_the_serde_form():
    d = cut.ikigai_endpoint.description_json()
    assert d["groups"] == [
        {"kind": "exactly-one", "members": ["content", "thread"], "pipe": "content"}
    ]
    assert d["inputs"][0]["needs"] == ["verb"]
    d = finding.ikigai_endpoint.description_json()
    assert d["output_from"] == "origin"
    by_name = {i["name"]: i for i in d["inputs"]}
    assert by_name["provider"]["one_of"] == []
    assert by_name["proposals"]["list"] == "comma"
    assert by_name["decision"]["values"]["retract"]["needs"] == ["revises"]
    d = intake.ikigai_endpoint.description_json()
    assert d["arguments"] == {"envelope": "content"}
    assert d["output_from"] == {"input": "email"}


def test_a_contract_with_the_new_shape_is_digested_under_v2_and_named_in_the_turtle():
    door = cut.ikigai_endpoint
    ttl = door.description_turtle()
    iri = contract_iri(door.id, door.actions[Verb.SOURCE].spec_json())
    assert f"<{iri}> a ik:Action" in ttl
    assert 'ik:groupKind "exactly-one"' in ttl
    assert "ik:pipe <urn:ikigai:endpoint:cut:input:content>" in ttl
    assert door.action_matches()[0]["contract"] == iri
    assert "exactly-one of: content, thread (a pipe fills content)" in door.description_text()


def test_a_group_refuses_none_and_both_but_not_one():
    assert isinstance(issue(cut, content="x"), Resolved)
    none = refusal(issue(cut))
    assert none.name == "content,thread"
    assert "exactly one of `content`, `thread`" in none.detail
    assert refusal(issue(cut, content="x", thread="t", verb="v")).name == "content,thread"


def test_a_blank_member_is_not_given():
    assert refusal(issue(cut, content="  ")).name == "content,thread"


def test_a_dependency_is_refused_when_its_input_is_not_given():
    assert refusal(issue(cut, thread="t")).name == "thread"
    assert isinstance(issue(cut, thread="t", verb="v"), Resolved)


def test_a_value_note_needs_and_a_list_items_one_of_are_enforced():
    needs = refusal(issue(finding, decision="retract"))
    assert (needs.name, "`decision=retract` needs `revises`" in needs.detail) == ("decision", True)
    assert isinstance(issue(finding, decision="retract", revises="d1"), Resolved)
    assert isinstance(issue(finding, decision="publish", proposals="minor, info"), Resolved)
    assert refusal(issue(finding, decision="publish", proposals="minor,major")).name == "proposals"


def test_an_empty_enumeration_accepts_nothing():
    assert "accepts none" in refusal(issue(finding, decision="publish", provider="x")).detail


def test_an_envelope_given_as_carrier_and_fields_is_refused():
    assert isinstance(issue(intake, content="{}"), Resolved)
    assert isinstance(issue(intake, email="a@b"), Resolved)
    assert refusal(issue(intake, content="{}", email="a@b")).name == "content"


def test_a_contract_naming_an_input_it_does_not_have_is_refused_at_declaration():
    with pytest.raises(ValueError, match="`nope`, which is not an input"):

        @endpoint("urn:py:bad", args=["a"], groups=[InputGroup.exactly_one("a", "nope")])
        def bad(a: str) -> str:
            return a


def test_a_family_verb_declares_the_shape_per_action():
    door = family("urn:py:cell", id="cell")

    @door.sink(
        args=[ArgSpec("content", required=False), ArgSpec("note", required=False)],
        groups=[InputGroup.at_least_one("content", "note")],
    )
    def put(**kwargs) -> str:
        return "ok"

    sink = door.description_json()["actions"][0]
    assert sink["groups"] == [{"kind": "at-least-one", "members": ["content", "note"]}]


def test_an_arguments_value_must_be_open_or_envelope():
    with pytest.raises(ValueError, match="open"):
        endpoint("urn:py:x", arguments={"closed": "no"})(lambda: "x")
