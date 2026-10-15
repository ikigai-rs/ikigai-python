"""The served door's declared-capability floor (ledger #1580) and the coverage
predicate it runs on, pinned with ikigai-core's own vectors (ledger #1583).

Every coverage, principal, floor and denial expectation comes from
``tests/vectors/capability_vectors.json``, a VERBATIM copy of ikigai-vocab's
``tests/vectors/capability_vectors.json``: core computes that file (its denials by issuing
through a kernel) and its own test refuses to let it drift, and devtools' ``vector-drift``
compares this copy with it. So a divergence from core fails here rather than as a host that
admits what this peer refuses, or the reverse. To pick up new vectors, copy the file again;
never edit or reformat it.

The face-local checks stay: the doctests, the ``denied:`` display of the text, and the floor
as ``Space.dispatch`` and the socket enforce it."""

import doctest
import json
import threading
from pathlib import Path

import pytest

import ikigai
import ikigai.capability
from ikigai.capability import allows, denial_message, is_deny_scope, principal, satisfies
from ikigai.serve import Server, Space, endpoint, family, required_scopes, unsatisfied_scopes
from ikigai.wire import (
    Capability,
    DeniedError,
    ErrorTypedReply,
    Inline,
    Issue,
    IssueAs,
    IssueTraced,
    Request,
    Resolved,
    ResolvedTraced,
    TraceContext,
    UnresolvedError,
    Verb,
)

ROOT = Capability.root()

VECTORS_FILE = Path(__file__).parent / "vectors" / "capability_vectors.json"
VECTORS = json.loads(VECTORS_FILE.read_text(encoding="utf-8"))


def scoped(*scopes):
    return Capability.scoped(scopes)


def capability(held):
    """A vector's capability: ``"root"`` or the list of scopes it holds."""
    return ROOT if held == "root" else Capability.scoped(held)


def verb(name):
    return Verb[name.upper()]


def ids(rows, *fields):
    return [" ".join(str(row[f]) for f in fields)[:120] for row in rows]


def test_the_capability_doctests_hold():
    failed, attempted = doctest.testmod(ikigai.capability)
    assert attempted > 0 and failed == 0


def test_the_floor_doctests_hold():
    finder = doctest.DocTestFinder()
    runner = doctest.DocTestRunner()
    for test in finder.find(required_scopes, "required_scopes", globs=required_scopes.__globals__):
        runner.run(test)
    result = runner.summarize(verbose=False)
    assert result.attempted > 0 and result.failed == 0


def test_the_vectors_are_the_form_this_face_reads():
    assert VECTORS["form"] == "ikigai:capability:v1"
    for section in ("deny_scopes", "coverage", "principals", "floors", "denials"):
        assert VECTORS[section], f"capability_vectors.json has no {section}"


# -- is_deny_scope ---------------------------------------------------------------------


@pytest.mark.parametrize("row", VECTORS["deny_scopes"], ids=ids(VECTORS["deny_scopes"], "scope"))
def test_is_deny_scope_is_cores(row):
    assert is_deny_scope(row["scope"]) is row["is_deny_scope"]


# -- allows (the exact primitive) and satisfies (the floor's predicate) ----------------


@pytest.mark.parametrize(
    "row", VECTORS["coverage"], ids=ids(VECTORS["coverage"], "capability", "scope")
)
def test_coverage_is_cores(row):
    cap, scope = capability(row["capability"]), row["scope"]
    assert allows(cap, scope) is row["allows"]
    assert cap.allows(scope) is row["allows"]
    assert satisfies(cap, scope) is row["satisfies"]
    assert cap.satisfies(scope) is row["satisfies"]


@pytest.mark.parametrize("row", VECTORS["principals"], ids=ids(VECTORS["principals"], "capability"))
def test_principal_is_cores(row):
    assert principal(capability(row["capability"])) == row["principal"]


# -- required_scopes: core's Floor table, from a description ----------------------------


# Rows where this face and core DISAGREE, held as strict xfails so the rest of the vectors
# land while the hub decides which side has the bug (ledger #1583). Strict: a fix on either
# side turns the row into an XPASS, which fails, so the entry cannot outlive the disagreement.
DISAGREES = {
    # Core's Description::action_specs takes the FIRST action declared for a verb
    # (`find`); `ikigai.serve.action_specs` builds a dict, so the LAST one wins.
    "first-action-wins": "action_specs: core takes the first action for a verb, this face the last",
    # Core deduplicates every scope of the union; `required_scopes` here checks a scope
    # against the union only BEFORE adding a whole `requires` list, so a scope repeated
    # inside one list is kept twice.
    "duplicate-verbs": "required_scopes: a scope repeated within one requires list is not deduped",
}


def floor_params():
    for floor in VECTORS["floors"]:
        reason = DISAGREES.get(floor["name"])
        marks = [pytest.mark.xfail(strict=True, reason=reason)] if reason else []
        yield pytest.param(floor, id=floor["name"], marks=marks)


def test_every_disagreement_names_a_row():
    assert set(DISAGREES) <= {floor["name"] for floor in VECTORS["floors"]}


@pytest.mark.parametrize("floor", list(floor_params()))
def test_required_scopes_is_cores(floor):
    for row in floor["verbs"]:
        assert required_scopes(floor["description"], verb(row["verb"])) == row["required_scopes"], (
            row["verb"]
        )


# -- denials: the kernel's text for the first unsatisfied scope -------------------------


@pytest.mark.parametrize("row", VECTORS["denials"], ids=ids(VECTORS["denials"], "name"))
def test_the_floor_refuses_with_cores_text(row):
    lacking = unsatisfied_scopes(
        row["description"], verb(row["verb"]), capability(row["capability"])
    )
    refused = denial_message(lacking[0], row["target"]) if lacking else None
    assert refused == row["denied"]
    if refused is not None:
        # This face's typed error says it the way core's Display does.
        assert DeniedError(refused).display() == "denied: " + refused


# -- the served floor: Space.dispatch ---------------------------------------------------

entered: list[str] = []


@endpoint("urn:py:cal", requires=["urn:cap:cal:read"])
def cal() -> str:
    entered.append("cal")
    return "agenda"


@endpoint("urn:py:fetch", requires=["urn:cap:net:*"])
def fetch() -> str:
    entered.append("fetch")
    return "fetched"


@endpoint("urn:py:open")
def public() -> str:
    entered.append("open")
    return "anyone"


diary = family("urn:py:diary:{day}", id="diary")


@diary.source(requires=["urn:cap:diary:read"])
def read_day(day: str) -> str:
    entered.append("diary.source")
    return f"day {day}"


@diary.sink(requires=["urn:cap:diary:write"])
def write_day(day: str, content: str) -> None:
    entered.append("diary.sink")


SPACE = Space([cal, fetch, public, diary])


def call(verb, target, capability=None, **args):
    request = Request(verb, target, {k: Inline(v.encode()) for k, v in args.items()})
    if capability is None:
        return SPACE.dispatch(Issue(request))
    return SPACE.dispatch(IssueAs(request, capability))


def denied(reply, scope, target):
    assert isinstance(reply, ErrorTypedReply), reply
    assert isinstance(reply.error, DeniedError), reply.error
    assert reply.error.message == denial_message(scope, target)
    return True


@pytest.fixture(autouse=True)
def fresh_log():
    entered.clear()
    yield


def test_an_under_capable_issue_as_is_refused_typed_and_never_entered():
    reply = call(Verb.SOURCE, "urn:py:cal", scoped("urn:cap:other"))
    assert denied(reply, "urn:cap:cal:read", "urn:py:cal")
    assert entered == []


def test_a_covering_issue_as_runs():
    reply = call(Verb.SOURCE, "urn:py:cal", scoped("urn:cap:cal:read", "urn:cap:other"))
    assert isinstance(reply, Resolved) and reply.representation.data == b"agenda"


def test_the_alias_stripped_target_is_floored_too():
    reply = call(Verb.SOURCE, "urn:cal", scoped())
    assert denied(reply, "urn:cap:cal:read", "urn:cal")


def test_a_held_exclusion_never_satisfies_an_exact_requirement():
    reply = call(Verb.SOURCE, "urn:py:cal", scoped("urn:cap:cal:read:-x", "urn:cap:cal:-read"))
    assert denied(reply, "urn:cap:cal:read", "urn:py:cal")


def test_a_wildcard_requirement_takes_any_grant_under_its_prefix():
    assert isinstance(
        call(Verb.SOURCE, "urn:py:fetch", scoped("urn:cap:net:example.com")), Resolved
    )
    reply = call(Verb.SOURCE, "urn:py:fetch", scoped("urn:cap:fs:read:/tmp"))
    assert denied(reply, "urn:cap:net:*", "urn:py:fetch")
    assert entered == ["fetch"]


def test_per_verb_requires_enforce_independently():
    reader = scoped("urn:cap:diary:read")
    writer = scoped("urn:cap:diary:write")
    assert isinstance(call(Verb.SOURCE, "urn:py:diary:mon", reader), Resolved)
    assert denied(
        call(Verb.SINK, "urn:py:diary:mon", reader, content="x"),
        "urn:cap:diary:write",
        "urn:py:diary:mon",
    )
    assert isinstance(call(Verb.SINK, "urn:py:diary:mon", writer, content="x"), Resolved)
    assert denied(
        call(Verb.SOURCE, "urn:py:diary:mon", writer), "urn:cap:diary:read", "urn:py:diary:mon"
    )
    assert entered == ["diary.source", "diary.sink"]


def test_an_undeclared_exists_needs_what_source_needs():
    # The family's default Exists runs the Source handler: a writer may not probe it.
    writer = scoped("urn:cap:diary:write")
    assert denied(
        call(Verb.EXISTS, "urn:py:diary:mon", writer), "urn:cap:diary:read", "urn:py:diary:mon"
    )
    reply = call(Verb.EXISTS, "urn:py:diary:mon", scoped("urn:cap:diary:read"))
    assert isinstance(reply, Resolved) and reply.representation.data == b"true"
    # A flat door's Exists likewise.
    assert denied(call(Verb.EXISTS, "urn:py:cal", scoped()), "urn:cap:cal:read", "urn:py:cal")


def test_an_undeclared_mutating_verb_needs_everything_declared():
    # Before the endpoint refuses the verb, the floor asks for every declared scope.
    reader = scoped("urn:cap:diary:read")
    assert denied(
        call(Verb.DELETE, "urn:py:diary:mon", reader), "urn:cap:diary:write", "urn:py:diary:mon"
    )
    both = scoped("urn:cap:diary:read", "urn:cap:diary:write")
    reply = call(Verb.DELETE, "urn:py:diary:mon", both)
    assert isinstance(reply, ErrorTypedReply) and not isinstance(reply.error, DeniedError)
    assert "not supported" in reply.error.message


def test_meta_is_never_gated_and_an_unbound_name_is_unresolved():
    assert isinstance(call(Verb.META, "urn:py:cal", scoped()), Resolved)
    reply = call(Verb.SOURCE, "urn:py:nowhere", scoped())
    assert isinstance(reply, ErrorTypedReply) and isinstance(reply.error, UnresolvedError)


def test_a_door_declaring_nothing_is_open_to_every_capability():
    assert isinstance(call(Verb.SOURCE, "urn:py:open", scoped()), Resolved)


def test_a_plain_issue_resolves_under_root_as_the_rust_ipc_door_does():
    reply = call(Verb.SOURCE, "urn:py:cal")
    assert isinstance(reply, Resolved) and reply.representation.data == b"agenda"
    assert isinstance(call(Verb.SOURCE, "urn:py:cal", ROOT), Resolved)


def test_a_traced_issue_is_floored_on_its_capability():
    request = Request(Verb.SOURCE, "urn:py:cal", {})
    context = TraceContext(1, None)
    reply = SPACE.dispatch(IssueTraced(request, scoped("urn:cap:other"), context))
    assert denied(reply, "urn:cap:cal:read", "urn:py:cal")
    reply = SPACE.dispatch(IssueTraced(request, scoped("urn:cap:cal:read"), context))
    assert isinstance(reply, ResolvedTraced)
    assert entered == ["cal"]


def test_the_floor_crosses_the_socket_typed(socket_dir):
    server = Server([cal], socket_dir / "f.sock")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with ikigai.connect(server.path, capability=scoped("urn:cap:other")) as k:
            with pytest.raises(ikigai.DeniedError, match="does not grant `urn:cap:cal:read`"):
                k.source("urn:py:cal")
        with ikigai.connect(server.path, capability=scoped("urn:cap:cal:read")) as k:
            assert k.source("urn:py:cal").text == "agenda"
        with ikigai.connect(server.path) as k:
            assert k.source("urn:py:cal").text == "agenda"
    finally:
        server.shutdown()
        thread.join(timeout=5)
