"""One base64 for the ecosystem: ``ikigai_http::base64``, RFC 4648 section 4, stdlib only.

The standard alphabet (``+`` and ``/``), padding required on decode, canonical only
(RFC 4648 section 3.5: nonzero pad bits are refused), and no whitespace or other
non-alphabet character (section 3.3: "MUST reject"). URL-safe is not offered.

Python's ``base64.b64decode(validate=True)`` accepts non-canonical pad bits (``"Zh=="``
decodes to ``b"f"``), so :func:`decode` re-encodes what it decoded and compares it with
the input. Pinned by ``tests/vectors/auth_vectors.json`` (ledger #1861).
"""

from __future__ import annotations

import base64 as _std
import binascii

__all__ = ["Base64Error", "decode", "encode"]


class Base64Error(ValueError):
    """The text is not canonical padded standard base64. It never carries the input."""

    code = "base64-invalid"

    def __init__(self) -> None:
        """Carry only the code."""
        super().__init__(self.code)


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
