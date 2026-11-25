"""A served handler and the caller's capability (ledger #1584,
``docs/design/handler-capability.md``).

Two parts:

- **The gap, reproduced.** Today a handler receives its declared inputs and
  :func:`ikigai.serve.current_run` (retries, deadline), and nothing else, so a rule finer
  than the declared floor (a path ACL) cannot be written. ``test_the_gap_*`` pins that.
  The strict ``xfail`` turns into a failure the day the proposed ``current_caller`` lands,
  so whoever lands it removes the marker and keeps the test.
- **The proof.** The design needs no wire change and no change to the floor. A ``Space``
  that puts the capability it already resolves under into a context variable before
  routing is enough for a handler to apply a path ACL. That ``Space`` lives HERE, in the
  test, not in ``src/``. Default behavior is unchanged.
"""

import contextvars
import dataclasses

import pytest

import ikigai.serve
from ikigai.capability import denial_message
from ikigai.serve import Space, current_run, endpoint
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
    # And no public accessor hands one over.
    public = [n for n in dir(ikigai.serve) if not n.startswith("_")]
    assert not any("caller" in n.lower() for n in public)


@pytest.mark.xfail(
    strict=True,
    reason="ledger #1584: a served handler cannot read the caller's capability yet",
)
def test_the_gap_a_path_acl_cannot_be_written_with_the_public_api():
    @endpoint("urn:py:acl", requires=["urn:cap:acl:read:*"])
    def guarded(path: str) -> str:
        caller = ikigai.serve.current_caller()  # the proposed accessor
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

#: The capability of the call being served, as the proof's Space sets it.
_CALLER: contextvars.ContextVar[Capability | None] = contextvars.ContextVar(
    "proof_caller", default=None
)


def current_caller() -> Capability | None:
    """The proof's accessor: the capability the served call resolves under, ``None``
    outside a served call."""
    return _CALLER.get()


class CallerSpace(Space):
    """A ``Space`` that exposes the capability it ALREADY resolves under (root for a plain
    ``Issue``, the carried one otherwise) for the length of one resolution. The floor runs
    before the handler, as it always has."""

    def _resolve(self, request, alias_first=False, capability=None):
        token = _CALLER.set(Capability.root() if capability is None else capability)
        try:
            return super()._resolve(request, alias_first, capability)
        finally:
            _CALLER.reset(token)


ACL = "urn:cap:docs:read:"


def acl_allows(capability: Capability, path: str) -> bool:
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


SPACE = CallerSpace([read_doc])


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
    assert current_caller() is None


def test_the_stock_space_is_unchanged():
    # The same handler under the stock Space: the floor admits, and the handler has no
    # capability to read, which is the gap this note is about.
    reply = Space([read_doc]).dispatch(
        IssueAs(
            Request(Verb.SOURCE, "urn:py:docs", {"path": Inline(b"/home/a")}),
            scoped(ACL + "/home"),
        )
    )
    assert isinstance(reply, ErrorTypedReply)
    assert "NoneType" in reply.error.message or "is_root" in reply.error.message
