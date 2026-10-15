"""Capability coverage, ported from ``ikigai_core::capability`` (ledger #1580).

A served door enforces the ``requires`` it declares on every call it answers (see
:class:`ikigai.serve.Space`), and it decides "does this capability cover that declared
scope?" exactly the way a Rust kernel does, so a host that mounts this peer and a
caller that reaches it directly see one floor, not two. Each function below names the
core function it mirrors; ``tests/test_capability.py`` pins them with core's own test
cases.

- **Root** satisfies every scope.
- **A plain scope** (no trailing ``*``) must be HELD, exactly (core's
  ``Capability::allows``). An EXCLUSION (:func:`is_deny_scope`: a ``-`` leading the
  rule, ``urn:cap:fs:read:-/root/secret``) is never granted by being held, so a
  ``requires`` that names one is satisfiable only by root. A PRINCIPAL scope
  (``urn:cap:principal:<iri>``) is granted only as the ONE principal the capability
  carries: held beside a second principal, neither is granted.
- **A scope ending in ``*``** is the parameterized family form, a PRESENCE test:
  "holds ANY scope under this prefix" (core's ``cap_satisfies``). A held exclusion
  under the prefix is present and so satisfies it (ledger #858: the floor says "may
  hold a grant here"; the endpoint's own rule decides which). There is no infix
  wildcard: ``urn:cap:x:*:read`` is the literal text, not a pattern.

>>> from ikigai.wire import Capability
>>> per_root = Capability.scoped(["urn:cap:browse:read:ikigai-core"])
>>> satisfies(per_root, "urn:cap:browse:read:*"), allows(per_root, "urn:cap:browse:read:*")
(True, False)
>>> satisfies(per_root, "urn:cap:browse:write:*"), satisfies(Capability.root(), "urn:cap:net:*")
(False, True)
>>> denial_message("urn:cap:script:run:open-*", "urn:script:open-door")
'capability does not grant `urn:cap:script:run:open-*` (declared by `urn:script:open-door`; \
any grant under `urn:cap:script:run:open-` satisfies it)'
"""

from __future__ import annotations

from .contract import is_iri
from .wire import Capability

#: The prefix of a principal scope: ``urn:cap:principal:<iri>`` (core's
#: ``PRINCIPAL_SCOPE_PREFIX``).
PRINCIPAL_SCOPE_PREFIX = "urn:cap:principal:"

#: The longest principal IRI a capability carries, in UTF-8 bytes (core's
#: ``MAX_PRINCIPAL_LEN``).
MAX_PRINCIPAL_LEN = 512


def _is_word(segment: str) -> bool:
    """A family or qualifier segment: ``[a-z0-9][a-z0-9_-]*``, ASCII only."""
    lead = "abcdefghijklmnopqrstuvwxyz0123456789"
    return bool(segment) and segment[0] in lead and all(c in lead or c in "_-" for c in segment)


def is_deny_scope(scope: str) -> bool:
    """Whether ``scope`` is an EXCLUSION (core's ``is_deny_scope``): a ``-`` leading the
    rule in ``urn:cap:<family>:-<rule>`` or ``urn:cap:<family>:<qualifier>:-<rule>``,
    where family and qualifier are words (``[a-z0-9][a-z0-9_-]*``). A ``-`` deeper in,
    or inside a segment, is just a character.

    >>> is_deny_scope("urn:cap:fs:read:-/root/secret"), is_deny_scope("urn:cap:net:-")
    (True, True)
    >>> is_deny_scope("urn:cap:fs:read:notes:-x"), is_deny_scope("urn:cap:-fs:read")
    (False, False)
    """
    if not scope.startswith("urn:cap:"):
        return False
    segments = scope[len("urn:cap:") :].split(":", 2)
    if not _is_word(segments[0]):
        return False
    if len(segments) < 2:
        return False
    second = segments[1]
    if second.startswith("-"):
        return True
    if _is_word(second):
        return len(segments) == 3 and segments[2].startswith("-")
    return False


def _carriable_principal(iri: str) -> bool:
    """Core's ``check_principal`` as a yes/no: an absolute IRI of at most
    :data:`MAX_PRINCIPAL_LEN` bytes, with no ``*``, whose scope is not deny-shaped."""
    return (
        len(iri.encode("utf-8")) <= MAX_PRINCIPAL_LEN
        and is_iri(iri)
        and "*" not in iri
        and not is_deny_scope(PRINCIPAL_SCOPE_PREFIX + iri)
    )


def _principal_in(scope: str) -> str | None:
    """The principal ``scope`` carries when it is a well-formed principal scope."""
    if not scope.startswith(PRINCIPAL_SCOPE_PREFIX):
        return None
    iri = scope[len(PRINCIPAL_SCOPE_PREFIX) :]
    return iri if _carriable_principal(iri) else None


def principal(capability: Capability) -> str | None:
    """The ONE principal ``capability`` carries (core's ``Capability::principal``):
    ``None`` for root, for none, and for several. A wildcard is never one."""
    if capability.scopes is None:
        return None
    found = [p for p in (_principal_in(s) for s in capability.scopes) if p is not None]
    return found[0] if len(found) == 1 else None


def allows(capability: Capability, scope: str) -> bool:
    """Whether ``capability`` grants ``scope`` matched EXACTLY (core's
    ``Capability::allows``): root grants everything; otherwise the scope is held, is
    not an exclusion, and, when it is a principal scope, names the capability's one
    principal. This is the primitive; a declared ``requires`` is checked with
    :func:`satisfies`, which reads a trailing ``*`` as a family."""
    held = capability.scopes
    if held is None:
        return True
    if scope not in held or is_deny_scope(scope):
        return False
    named = _principal_in(scope)
    return named is None or principal(capability) == named


def satisfies(capability: Capability, scope: str) -> bool:
    """Whether ``capability`` satisfies one DECLARED scope, as the kernel's floor
    decides it (core's ``cap_satisfies``): a scope ending in ``*`` is satisfied by
    holding ANY scope that starts with the text before the ``*`` (root always); any
    other scope is :func:`allows`."""
    if scope.endswith("*"):
        if capability.scopes is None:
            return True
        prefix = scope[:-1]
        return any(held.startswith(prefix) for held in capability.scopes)
    return allows(capability, scope)


def denial_message(scope: str, target: str) -> str:
    """The text a refused floor carries in its ``Denied``, word for word core's
    ``Kernel::denial_message`` (without an alias hop, which this peer never sees): a
    family requirement also names what would satisfy it."""
    if not scope.endswith("*"):
        return f"capability does not grant `{scope}` (declared by `{target}`)"
    prefix = scope[:-1]
    if not prefix:
        return (
            f"capability does not grant `{scope}` (declared by `{target}`; any grant at "
            "all satisfies it)"
        )
    return (
        f"capability does not grant `{scope}` (declared by `{target}`; any grant "
        f"under `{prefix}` satisfies it)"
    )
