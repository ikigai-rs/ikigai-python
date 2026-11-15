"""One base64 for the ecosystem: ``ikigai_http::base64``, RFC 4648 sections 4 and 5, stdlib only.

The standard alphabet (``+`` and ``/``), padding required on decode, canonical only
(RFC 4648 section 3.5: nonzero pad bits are refused), and no whitespace or other
non-alphabet character (section 3.3: "MUST reject"). URL-safe uses ``-`` and ``_``
without padding, also canonical only.

Python's ``base64.b64decode(validate=True)`` accepts non-canonical pad bits (``"Zh=="``
decodes to ``b"f"``), so :func:`decode` re-encodes what it decoded and compares it with
the input. Pinned by ``tests/vectors/auth_vectors.json`` (ledger #1861).
"""

from __future__ import annotations

import base64 as _std
import binascii

__all__ = ["Base64Error", "decode", "decode_url", "encode", "encode_url"]


class Base64Error(ValueError):
    """The text is not canonical base64 for the requested profile. Never carries input."""

    code = "base64-invalid"

    def __init__(self, *, kind: str | None = None) -> None:
        """Carry the code and an optional URL-safe refusal reason, never input."""
        self.kind = kind
        if kind is None:
            super().__init__(self.code)
        else:
            why = {
                "symbol": "a character outside the URL-safe alphabet, or misplaced padding",
                "length": "a length that no unpadded encoding has",
                "pad-bits": "nonzero unused bits in the last symbol (RFC 4648 §3.5)",
                "padding": "padding is not allowed",
            }[kind]
            super().__init__(
                "not canonical base64url (RFC 4648 §5, URL-safe alphabet, unpadded): " + why
            )


def encode(data: bytes | bytearray | memoryview) -> str:
    """Standard-alphabet base64 of ``data``, padded."""
    return _std.b64encode(bytes(data)).decode("ascii")


def decode(text: str | bytes) -> bytes:
    """The bytes ``text`` encodes, or :class:`Base64Error` (code ``base64-invalid``)."""
    try:
        raw = text.encode("ascii") if isinstance(text, str) else bytes(text)
        out = _std.b64decode(raw, validate=True)
    except (ValueError, binascii.Error):
        raise Base64Error from None
    if _std.b64encode(out) != raw:
        raise Base64Error
    return out


_URL_ALPHABET = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


def encode_url(data: bytes | bytearray | memoryview) -> str:
    """RFC 4648 section 5 base64url of ``data``, without padding."""
    return _std.urlsafe_b64encode(bytes(data)).rstrip(b"=").decode("ascii")


def decode_url(text: str | bytes) -> bytes:
    """Decode canonical unpadded base64url, or raise input-free :class:`Base64Error`."""
    try:
        raw = text.encode("ascii") if isinstance(text, str) else bytes(text)
    except ValueError:
        raise Base64Error(kind="symbol") from None
    if any(symbol not in _URL_ALPHABET and symbol != ord("=") for symbol in raw):
        raise Base64Error(kind="symbol")
    if b"=" in raw:
        kind = "padding" if raw.rstrip(b"=").find(b"=") == -1 else "symbol"
        raise Base64Error(kind=kind)
    if len(raw) % 4 == 1:
        raise Base64Error(kind="length")
    out = _std.urlsafe_b64decode(raw + b"=" * (-len(raw) % 4))
    if encode_url(out).encode("ascii") != raw:
        raise Base64Error(kind="pad-bits")
    return out
