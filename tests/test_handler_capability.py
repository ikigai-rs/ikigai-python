"""Served handler access to the caller capability and a path ACL."""

import dataclasses

import pytest

from ikigai.capability import denial_message
from ikigai.serve import Caller, Space, current_caller, current_run, endpoint, with_caller
from ikigai.wire import (
    Capability,
    DeniedError,
    ErrorTypedReply,
    Inline,
    Issue,
    IssueAs,
    IssueTraced,
    IssueWith,
    IssueWithCall,
    Request,
    Resolved,
    ResolvedTraced,
    TraceContext,
    Verb,
)

# -- the gap ---------------------------------------------------------------------------


def test_the_gap_a_handler_sees_only_its_inputs_and_the_run():
    seen = {}

    @endpoint("urn:py:probe", requires=["urn:cap:probe:read"])
    def probe(path: str) -> str:
        seen["run"] = current_run()
        return path

    reply = Space([probe]).dispatch(
        IssueAs(
            Request(Verb.SOURCE, "urn:py:probe", {"path": Inline(b"/a")}),
            Capability.scoped(["urn:cap:probe:read", "urn:cap:probe:path:/a"]),
        )
    )
    assert isinstance(reply, Resolved)
    # The run carries the retry pool and the deadline, and no capability.
    assert [f.name for f in dataclasses.fields(seen["run"])] == ["retries", "deadline"]


def test_the_gap_a_path_acl_cannot_be_written_with_the_public_api():
    @endpoint("urn:py:acl", requires=["urn:cap:acl:read:*"])
    def guarded(path: str) -> str:
        caller = current_caller()
        if not caller.allows(f"urn:cap:acl:read:{path}"):
            raise DeniedError(f"capability does not grant `read` on `{path}`")
        return path

    reply = Space([guarded]).dispatch(
        IssueAs(
            Request(Verb.SOURCE, "urn:py:acl", {"path": Inline(b"/b")}),
            Capability.scoped(["urn:cap:acl:read:/a"]),
        )
    )
    assert isinstance(reply, ErrorTypedReply) and isinstance(reply.error, DeniedError)


# -- the proof -------------------------------------------------------------------------

ACL = "urn:cap:docs:read:"


def acl_allows(capability: Caller, path: str) -> bool:
    """The reference path ACL from the design note: among the caller's
    ``urn:cap:docs:read:[-]<dir>`` scopes that contain ``path``, the deepest decides, a deny
    wins a tie, and no match denies. Root allows everything."""
    if capability.is_root:
        return True
    names = [n for n in path.split("/") if n]
    best, allowed = -1, False
    for scope in sorted(capability.scopes or ()):
        if not scope.startswith(ACL):
            continue
        rest = scope[len(ACL) :]
        allow = not rest.startswith("-")
        rule = [n for n in rest.lstrip("-").split("/") if n]
        if names[: len(rule)] != rule:
            continue
        if len(rule) > best:
            best, allowed = len(rule), allow
        elif len(rule) == best:
            allowed = allowed and allow
    return best >= 0 and allowed


entered: list[str] = []


@endpoint("urn:py:docs", requires=[ACL + "*"])
def read_doc(path: str) -> str:
    entered.append(path)
    # The ACL runs before the path is looked at, so a refusal says nothing about whether
    # the file exists.
    if not acl_allows(current_caller(), path):
        raise DeniedError(f"capability does not grant `read` on `{path}`")
    return f"contents of {path}"


SPACE = Space([read_doc])


def scoped(*scopes):
    return Capability.scoped(scopes)


def ask(path, capability=None):
    request = Request(Verb.SOURCE, "urn:py:docs", {"path": Inline(path.encode())})
    if capability is None:
        return SPACE.dispatch(Issue(request))
    return SPACE.dispatch(IssueAs(request, capability))


@pytest.fixture(autouse=True)
def fresh_log():
    entered.clear()
    yield


def refused(reply, path):
    assert isinstance(reply, ErrorTypedReply), reply
    assert isinstance(reply.error, DeniedError), reply.error
    assert reply.error.message == f"capability does not grant `read` on `{path}`"
    return True


def test_a_grant_for_a_directory_reads_inside_it():
    reply = ask("/home/notes/a.txt", scoped(ACL + "/home/notes"))
    assert isinstance(reply, Resolved)
    assert reply.representation.data == b"contents of /home/notes/a.txt"


def test_a_grant_for_one_directory_does_not_read_its_sibling():
    assert refused(ask("/home/mail/a.txt", scoped(ACL + "/home/notes")), "/home/mail/a.txt")


def test_the_deeper_exclusion_wins():
    cap = scoped(ACL + "/home", ACL + "-/home/secret")
    assert isinstance(ask("/home/notes/a.txt", cap), Resolved)
    assert refused(ask("/home/secret/key", cap), "/home/secret/key")


def test_a_tie_on_depth_is_a_denial():
    assert refused(ask("/home/x", scoped(ACL + "/home", ACL + "-/home")), "/home/x")


def test_root_and_a_plain_issue_read_anything():
    assert isinstance(ask("/anything", Capability.root()), Resolved)
    # A plain Issue resolves under ROOT (the owner's entitlement), so the handler sees root.
    assert isinstance(ask("/anything"), Resolved)


def test_the_floor_still_refuses_first_and_the_handler_is_never_entered():
    reply = ask("/home/a", scoped("urn:cap:other:read"))
    assert isinstance(reply, ErrorTypedReply) and isinstance(reply.error, DeniedError)
    assert reply.error.message == denial_message(ACL + "*", "urn:py:docs")
    assert entered == []


def test_every_call_form_hands_the_handler_the_capability_it_resolves_under():
    cap = scoped(ACL + "/home/notes")
    request = Request(Verb.SOURCE, "urn:py:docs", {"path": Inline(b"/home/mail/x")})
    traced = SPACE.dispatch(IssueTraced(request, cap, TraceContext(1)))
    assert refused(traced, "/home/mail/x")
    with_run = SPACE.dispatch(IssueWithCall(request, cap, IssueWith(retries=2, time_left_ms=5000)))
    assert refused(with_run, "/home/mail/x")
    ok = Request(Verb.SOURCE, "urn:py:docs", {"path": Inline(b"/home/notes/x")})
    assert isinstance(SPACE.dispatch(IssueTraced(ok, cap, TraceContext(1))), ResolvedTraced)


def test_the_capability_is_scoped_to_one_call():
    ask("/home/notes/a", scoped(ACL + "/home/notes"))
    with pytest.raises(LookupError):
        current_caller()


def test_caller_view_and_nested_contexts():
    cap = scoped(ACL + "/home", ACL + "-/home/secret", "urn:cap:principal:urn:alice")
    with with_caller(cap) as caller:
        assert current_caller() is caller
        assert caller.principal() == "urn:alice"
        assert caller.scopes == cap.scopes
        assert caller.under(ACL) == (ACL + "-/home/secret", ACL + "/home")
        assert caller.allows(ACL + "/home")
        assert not caller.allows(ACL + "-/home/secret")
        assert caller.satisfies(ACL + "*")
        with pytest.raises(dataclasses.FrozenInstanceError):
            caller.capability = Capability.root()
        with pytest.raises(RuntimeError), with_caller(Capability.root()) as root:
            assert root.is_root and root.scopes is None and root.under(ACL) == ()
            raise RuntimeError("test restoration")
        assert current_caller() is caller
        ask("/anything")
        assert current_caller() is caller
    with pytest.raises(LookupError):
        current_caller()
