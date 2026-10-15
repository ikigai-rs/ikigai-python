"""The served door's declared-capability floor (ledger #1580) and the coverage
predicate it runs on, pinned with ikigai-core's own cases.

Each table below is taken from a named test or doctest in
``ikigai-core/crates/ikigai-core/src/capability.rs``, ``describe.rs`` or ``kernel.rs``
(core 0.1.10x, main at 61ecdee), so a divergence from core fails here rather than as a
host that admits what this peer refuses, or the reverse."""

import doctest
import threading

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


def scoped(*scopes):
    return Capability.scoped(scopes)


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


# -- is_deny_scope: `deny_shape_pins_every_real_form` and the `is_deny_scope` doctest ----

DENIES = [
    "urn:cap:fs:read:-/root/secret",
    "urn:cap:fs:write:-/root/secret",
    "urn:cap:fs:delete:-/root/secret",
    "urn:cap:fs:list:-/root/secret",
    "urn:cap:fs:read:-secret",
    "urn:cap:fs:read:-/Users/brian/secrets",
    "urn:cap:net:-example.com",
    "urn:cap:net:-example.com:443",
    "urn:cap:net:-example.com/admin",
    "urn:cap:net:-example.com:443/admin",
    "urn:cap:net:-[::1]:8080",
    "urn:cap:net:-",
]

GRANTS = [
    "urn:cap:fs:read:/root",
    "urn:cap:fs:read:/a-b",
    "urn:cap:fs:read:a-b",
    "urn:cap:fs:read:notes:-x",
    "urn:cap:fs:read:/a:-b",
    "urn:cap:fs:read:*",
    "urn:cap:fs:read",
    "urn:cap:net:example.com",
    "urn:cap:net:example.com:-1",
    "urn:cap:net:[::1]:8080",
    "urn:cap:net:*",
    "urn:cap:store:read:graph:urn:x:-y",
    "urn:cap:ledger:read:my-ledger",
    "urn:cap:dev-server:read",
    "urn:cap:personal:calendar:read:freebusy",
    "urn:cap:Fs:read:-x",
    "urn:cap:-fs:read",
    "urn:cap:",
    "urn:cap",
    "urn:other:fs:read:-x",
    "",
]


@pytest.mark.parametrize("scope", DENIES)
def test_every_real_deny_form_is_an_exclusion(scope):
    assert is_deny_scope(scope)


@pytest.mark.parametrize("scope", GRANTS)
def test_grants_that_look_like_exclusions_are_not(scope):
    assert not is_deny_scope(scope)


# -- allows: the exact primitive -------------------------------------------------------

ALICE = "urn:example:person:alice"
BOB = "urn:example:person:bob"


def p(iri):
    return "urn:cap:principal:" + iri


ALLOWS = [
    # root_is_root_and_allows_everything
    (ROOT, "urn:cap:anything:read", True),
    # scoped_allows_only_its_scopes
    (
        scoped("urn:cap:personal:calendar:read:freebusy"),
        "urn:cap:personal:calendar:read:freebusy",
        True,
    ),
    (
        scoped("urn:cap:personal:calendar:read:freebusy"),
        "urn:cap:personal:calendar:read:detail",
        False,
    ),
    # an_exclusion_is_never_granted_by_exact_match
    (scoped("urn:cap:fs:read:/root", "urn:cap:secret:read:-x"), "urn:cap:fs:read:/root", True),
    (scoped("urn:cap:fs:read:/root", "urn:cap:secret:read:-x"), "urn:cap:secret:read:-x", False),
    (scoped("urn:cap:fs:read:/root", "urn:cap:secret:read:-x"), "urn:cap:fs:read:-/root", False),
    (ROOT, "urn:cap:secret:read:-x", True),
    # a_minted_principal_reads_back_and_is_granted
    (scoped("urn:cap:fs:read:/r", p(ALICE)), p(ALICE), True),
    (scoped("urn:cap:fs:read:/r", p(ALICE)), p(BOB), False),
    (scoped("urn:cap:fs:read:/r", p(ALICE)), "urn:cap:fs:read:/r", True),
    # several_principals_are_none: only the names are void
    (scoped(p(ALICE), p(BOB), "urn:cap:x"), p(ALICE), False),
    (scoped(p(ALICE), p(BOB), "urn:cap:x"), p(BOB), False),
    (scoped(p(ALICE), p(BOB), "urn:cap:x"), "urn:cap:x", True),
    # a_wildcard_is_never_an_identity
    (scoped("urn:cap:*"), p(ALICE), False),
    (scoped("urn:cap:principal:*"), p(ALICE), False),
    (scoped("urn:cap:principal:urn:*"), p(ALICE), False),
    # ...nor does a wildcard beside a real principal make it ambiguous
    (scoped("urn:cap:principal:*", p(ALICE)), p(ALICE), True),
    # root_holds_every_principal_and_names_none
    (ROOT, p(ALICE), True),
    # satisfies_is_the_kernels_predicate_and_allows_stays_exact
    (scoped("urn:cap:browse:read:ikigai-core"), "urn:cap:browse:read:*", False),
]


@pytest.mark.parametrize(("cap", "scope", "expected"), ALLOWS)
def test_allows_matches_core(cap, scope, expected):
    assert allows(cap, scope) is expected
    assert cap.allows(scope) is expected


def test_principal_matches_core():
    assert principal(ROOT) is None
    assert principal(scoped("urn:cap:fs:read:/r")) is None
    assert principal(scoped()) is None
    assert principal(scoped("urn:cap:fs:read:/r", p(ALICE))) == ALICE
    assert principal(scoped(p(ALICE), p(BOB), "urn:cap:x")) is None
    for wild in ("urn:cap:*", "urn:cap:principal:*", "urn:cap:principal:urn:*"):
        assert principal(scoped(wild)) is None
        assert principal(scoped(wild, p(ALICE))) == ALICE
    # A scope under the prefix that names no carriable principal is not one
    # (`what_cannot_be_carried_verbatim_is_refused_not_escaped`).
    for bad in ("", "alice", "two words", "urn:x:<a>", "urn:x:*", "x:-y", "urn:-x"):
        assert principal(scoped(p(bad))) is None, bad
    assert principal(scoped(p("urn:x:" + "a" * 512))) is None
    longest = "urn:x:" + "a" * (512 - 6)
    assert principal(scoped(p(longest))) == longest


# -- satisfies: the floor's predicate ----------------------------------------------------

PER_ROOT = scoped("urn:cap:browse:read:ikigai-core")

SATISFIES = [
    # satisfies_is_the_kernels_predicate_and_allows_stays_exact
    (PER_ROOT, "urn:cap:browse:read:*", True),
    (PER_ROOT, "urn:cap:browse:read:ikigai-core", True),
    (PER_ROOT, "urn:cap:browse:read:ikigai-gonk", False),
    (PER_ROOT, "urn:cap:browse:read", False),
    (PER_ROOT, "urn:cap:browse:write:*", False),
    (PER_ROOT, "urn:cap:net:*", False),
    (PER_ROOT, "urn:cap:*", True),
    (scoped("urn:cap:fs:read:-/secret"), "urn:cap:fs:read:-/secret", False),
    (scoped("urn:cap:fs:read:-/secret"), "urn:cap:fs:read:*", True),
    (scoped("urn:cap:x", p(ALICE)), p(ALICE), True),
    (ROOT, "urn:cap:net:*", True),
    (scoped(), "urn:cap:net:*", False),
    # a_wildcard_is_never_an_identity: the presence test is satisfied by a held wildcard
    (scoped("urn:cap:principal:*"), "urn:cap:principal:*", True),
    # the kernel's wildcard floor test: a host-scoped grant under the family
    (scoped("urn:cap:net:localhost:11434"), "urn:cap:net:*", True),
    (scoped("urn:cap:fs:read:/tmp"), "urn:cap:net:*", False),
    # a_family_floor_refusal_names_what_satisfies_it
    (scoped("urn:cap:script:run:close-door"), "urn:cap:script:run:open-*", False),
    (scoped("urn:cap:script:run:open-door"), "urn:cap:script:run:open-*", True),
    # `*` alone: any grant at all
    (scoped("urn:cap:anything"), "*", True),
    (scoped(), "*", False),
]


@pytest.mark.parametrize(("cap", "scope", "expected"), SATISFIES)
def test_satisfies_matches_core(cap, scope, expected):
    assert satisfies(cap, scope) is expected
    assert cap.satisfies(scope) is expected


def test_denial_text_is_cores_word_for_word():
    # an_exact_floor_refusal_text_is_unchanged
    assert DeniedError(denial_message("urn:cap:cal:read", "urn:cal")).display() == (
        "denied: capability does not grant `urn:cap:cal:read` (declared by `urn:cal`)"
    )
    # a_family_floor_refusal_names_what_satisfies_it
    assert DeniedError(
        denial_message("urn:cap:script:run:open-*", "urn:script:open-door")
    ).display() == (
        "denied: capability does not grant `urn:cap:script:run:open-*` "
        "(declared by `urn:script:open-door`; any grant under "
        "`urn:cap:script:run:open-` satisfies it)"
    )
    assert denial_message("*", "urn:x") == (
        "capability does not grant `*` (declared by `urn:x`; any grant at all satisfies it)"
    )


# -- required_scopes: core's Floor table, from a description ----------------------------

CALENDAR = {
    "id": "calendar",
    "verbs": ["Source", "Sink", "Meta"],
    "inputs": [],
    "outputs": [],
    "actions": [
        {"verb": "Source", "requires": ["urn:cap:personal:calendar:read"]},
        {"verb": "Sink", "requires": ["urn:cap:personal:calendar:write"]},
    ],
}


def test_required_scopes_is_cores_table():
    # Description::required_scopes's doctest.
    assert required_scopes(CALENDAR, Verb.SINK) == ["urn:cap:personal:calendar:write"]
    assert required_scopes(CALENDAR, Verb.EXISTS) == ["urn:cap:personal:calendar:read"]
    assert required_scopes(CALENDAR, Verb.META) == []
    assert len(required_scopes(CALENDAR, Verb.DELETE)) == 2
    # An undeclared Exists with no Source needs everything declared.
    sink_only = {"verbs": ["Sink"], "inputs": [], "outputs": [], "requires": ["urn:cap:w"]}
    assert required_scopes(sink_only, Verb.EXISTS) == ["urn:cap:w"]
    # A description declaring `requires` and no verb enforces it on every verb.
    no_verb = {"verbs": [], "inputs": [], "outputs": [], "requires": ["urn:cap:a"]}
    for verb in (Verb.SOURCE, Verb.SINK, Verb.EXISTS, Verb.DELETE):
        assert required_scopes(no_verb, verb) == ["urn:cap:a"], verb
    # An explicit per-verb `requires` REPLACES the flat one (ledger #5).
    replaced = {
        "verbs": ["Source", "Meta"],
        "inputs": [],
        "outputs": [],
        "requires": ["urn:cap:flat"],
        "actions": [{"verb": "Source", "requires": ["urn:cap:own"]}],
    }
    assert required_scopes(replaced, Verb.SOURCE) == ["urn:cap:own"]


def test_unsatisfied_scopes_is_cores():
    # Description::unsatisfied_scopes's doctest.
    fetch = {
        "verbs": ["Source"],
        "inputs": [],
        "outputs": [],
        "requires": ["urn:cap:net:*", "urn:cap:fetch:read"],
    }
    holder = scoped("urn:cap:net:example.com")
    assert unsatisfied_scopes(fetch, Verb.SOURCE, holder) == ["urn:cap:fetch:read"]
    assert unsatisfied_scopes(fetch, Verb.SOURCE, ROOT) == []


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
