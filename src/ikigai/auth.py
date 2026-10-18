"""``Authorization`` header values: ``ikigai_http::auth``, stdlib only.

Build ``Basic`` (RFC 7617) and ``Bearer`` (RFC 6750) credentials, and recognize the
scheme of an incoming value (RFC 9110 section 11). A module that sends or reads an
``Authorization`` header calls these rather than formatting ``"Basic "`` and base64 by
hand. Pinned by ``tests/vectors/auth_vectors.json`` (ledger #1861); the refusal codes
(:attr:`AuthError.code`) are part of the contract.

No credential leaves through a side door: an :class:`Authorization` shows only its
scheme in ``repr`` and ``str``, and its value is reached through :meth:`~Authorization.as_str`
at the one place it goes into a header. An :class:`AuthError` names the rule that was
broken and never carries the user id, the password or the token.
"""

from __future__ import annotations

import re
from typing import NamedTuple

from . import base64

__all__ = ["AuthError", "Authorization", "Scheme", "basic", "bearer", "scheme"]

_CONTROL = re.compile("[\x00-\x1f\x7f-\x9f]")
_B64TOKEN = re.compile(r"[A-Za-z0-9\-._~+/]+=*")
_TCHAR = re.compile(r"[A-Za-z0-9!#$%&'*+\-.^_`|~]+")


class AuthError(ValueError):
    """A credential was refused. ``code`` is stable; the text never holds the credential."""

    CODES = (
        "basic-user-id-colon",
        "basic-user-id-control",
        "basic-password-control",
        "bearer-token-syntax",
    )

    def __init__(self, code: str) -> None:
        """Name the broken rule by its stable ``code``."""
        super().__init__(code)
        self.code = code


class Authorization:
    """A built ``Authorization`` header value, such as ``Basic QWxhZGRpbjpvcGVuIHNlc2FtZQ==``.

    It is a credential: ``repr`` and ``str`` show only the scheme.
    """

    __slots__ = ("_name", "_value")

    def __init__(self, name: str, credentials: str) -> None:
        """Hold ``name`` and ``credentials``; callers use :func:`basic` or :func:`bearer`."""
        self._name = name
        self._value = f"{name} {credentials}"

    def as_str(self) -> str:
        """The whole header value, scheme included: what goes after ``Authorization: ``."""
        return self._value

    def credentials(self) -> str:
        """The part after the scheme and its space (the password in another encoding)."""
        return self._value[len(self._name) + 1 :]

    @property
    def scheme(self) -> str:
        """The scheme this value was built in: ``"Basic"`` or ``"Bearer"``."""
        return self._name

    def __repr__(self) -> str:
        """The scheme alone; never the credentials."""
        return f"Authorization({self._name} <redacted>)"

    __str__ = __repr__

    def __eq__(self, other: object) -> bool:
        """Equal when the whole header values are."""
        return isinstance(other, Authorization) and self._value == other._value

    def __hash__(self) -> int:
        """Hash of the header value."""
        return hash(self._value)

    def __reduce__(self):
        """Refuse pickling: a credential is not written out by accident."""
        raise TypeError("an Authorization is a credential and is not serialized")


def basic(user: str, password: str) -> Authorization:
    """Build a ``Basic`` credential (RFC 7617 section 2): base64 of ``user ":" password``, UTF-8.

    Refused in this order, so every face reports the same code for the same input: a
    colon in the user id (``basic-user-id-colon``); a control character (C0, DEL or C1)
    in the user id (``basic-user-id-control``), then in the password
    (``basic-password-control``). Empty parts are legal. No normalization is applied.
    """
    if ":" in user:
        raise AuthError("basic-user-id-colon")
    if _CONTROL.search(user):
        raise AuthError("basic-user-id-control")
    if _CONTROL.search(password):
        raise AuthError("basic-password-control")
    return Authorization("Basic", base64.encode(f"{user}:{password}".encode()))


def bearer(token: str) -> Authorization:
    """Build a ``Bearer`` credential (RFC 6750 section 2.1): ``b64token = 1*(...) *"="``."""
    if _B64TOKEN.fullmatch(token) is None:
        raise AuthError("bearer-token-syntax")
    return Authorization("Bearer", token)


class Scheme(NamedTuple):
    """A recognized scheme: ``kind`` is ``"basic"``, ``"bearer"`` or ``"other"``; ``name``
    is the registered spelling for a known scheme and the spelling that arrived otherwise."""

    kind: str
    name: str


def scheme(value: str) -> tuple[Scheme, str] | None:
    """Split an incoming ``Authorization`` value into its scheme and the credentials after it.

    Spaces and tabs around the value and between scheme and credentials are skipped. The
    credentials come back unvalidated and possibly empty. ``None`` when there is no
    scheme or it is not an RFC 9110 ``token``.
    """
    value = value.strip(" \t")
    cut = re.search("[ \t]", value)
    end = cut.start() if cut else len(value)
    name, rest = value[:end], value[end:]
    if _TCHAR.fullmatch(name) is None:
        return None
    lower = name.lower()
    if lower == "basic":
        found = Scheme("basic", "Basic")
    elif lower == "bearer":
        found = Scheme("bearer", "Bearer")
    else:
        found = Scheme("other", name)
    return found, rest.lstrip(" \t")
