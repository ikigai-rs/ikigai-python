"""Escaping and URL rules for HTML built from data: ``ikigai_fn::html``, stdlib only.

An HTML page built by string concatenation has three places a value can break out of:
element text, an attribute value, and a URL. Each gets one function here, pinned by the
shared vectors in ``tests/vectors/html_vectors.json`` (ledger #1813).

==============================================  ===================================
Where the value goes                            Function
==============================================  ===================================
element text: ``<p>{}</p>``                     :func:`html_text`
a quoted attribute: ``title="{}"``              :func:`attr`
one path segment or query value                 :func:`url_segment`
a whole link target from data: ``href="{}"``    :func:`safe_href`, then :func:`attr`
htmx's ``hx-vals='{}'``                         :func:`json_attr`
==============================================  ===================================

Percent escapes decode strictly through :func:`percent_decode` (text) or
:func:`percent_decode_bytes`, which refuse what ``urllib.parse.unquote`` lets through.

Nothing here makes an unquoted attribute, an event-handler attribute, or the inside of
``<script>`` or ``<style>`` safe.
"""

from __future__ import annotations

from collections.abc import Iterable

__all__ = [
    "PercentDecodeError",
    "attr",
    "html_text",
    "json_attr",
    "percent_decode",
    "percent_decode_bytes",
    "safe_href",
    "url_segment",
]

_TEXT = {
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
}
# A C0 control other than tab, LF and CR becomes U+FFFD: XML 1.0 cannot carry them.
_TEXT.update({chr(c): "�" for c in range(0x20) if chr(c) not in "\t\n\r"})
_TEXT_TABLE = str.maketrans(_TEXT)

_UNRESERVED = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")

#: What ``safe_href`` trims at both ends: every C0 control and the space (``<= U+0020``).
_TRIM = "".join(map(chr, range(0x21)))


def html_text(text: str) -> str:
    """``text``, safe as HTML (or XML) element text.

    ``&``, ``<``, ``>``, ``"`` and ``'`` become ``&amp;``, ``&lt;``, ``&gt;``, ``&quot;``
    and ``&#39;`` (not ``&apos;``, and not ``html.escape``'s ``&#x27;``); a C0 control
    other than tab, LF and CR becomes U+FFFD. Everything else passes through. Not
    idempotent, by design.
    """
    return text.translate(_TEXT_TABLE)


def attr(value: str) -> str:
    """``value``, safe inside a quoted attribute, whichever quote the markup uses.

    The same output as :func:`html_text`. An attribute must still be quoted, and a
    ``href`` or ``src`` from data goes through :func:`safe_href` first.
    """
    return html_text(value)


def url_segment(value: str) -> str:
    """``value`` percent-encoded as ONE path segment or query value.

    Every UTF-8 byte outside RFC 3986's unreserved set (``A-Z a-z 0-9 - . _ ~``) becomes
    ``%XX`` in uppercase hex, so ``/ ? # & = + %`` cannot change where the value ends.
    """
    return "".join(chr(b) if b in _UNRESERVED else f"%{b:02X}" for b in value.encode("utf-8"))


class PercentDecodeError(ValueError):
    """Why strict percent decoding refused an input.

    :attr:`kind` is ``"malformed_escape"`` (a ``%`` at input byte :attr:`offset` is not
    followed by two ASCII hex digits; both decoders refuse) or ``"invalid_utf8"`` (the
    decoded bytes are not UTF-8; only :func:`percent_decode` refuses, :attr:`offset` is
    ``None`` and the :class:`UnicodeDecodeError` is the ``__cause__``).
    """

    def __init__(self, kind: str, offset: int | None = None) -> None:
        """A refusal of ``kind``; ``offset`` only for ``"malformed_escape"``."""
        self.kind = kind
        self.offset = offset
        if kind == "malformed_escape":
            message = f"percent_decode: malformed percent-escape at byte {offset}"
        else:
            message = "percent_decode: decoded bytes are not UTF-8"
        super().__init__(message)


def _hex(b: int) -> int:
    if 0x30 <= b <= 0x39:
        return b - 0x30
    if 0x41 <= b <= 0x46:
        return b - 0x41 + 10
    if 0x61 <= b <= 0x66:
        return b - 0x61 + 10
    return -1


def percent_decode_bytes(data: bytes | bytearray | memoryview, plus_is_space: bool) -> bytes:
    """Decode ``%XX`` escapes in ``data`` into bytes, refusing every malformed escape.

    Each ``%XX`` (upper or lower hex) is decoded exactly once, so ``%252B`` gives
    ``%2B``. ``+`` becomes a space only when ``plus_is_space`` is true (form and query
    values; false for paths), and an escaped plus (``%2B``) stays a plus either way.
    Every other byte passes through, and no UTF-8 check is made. A ``%`` not followed
    by two hex digits raises :class:`PercentDecodeError` (``"malformed_escape"``) with
    its byte offset in ``data``.
    """
    data = bytes(data)
    if b"%" not in data and not (plus_is_space and b"+" in data):
        return data
    out = bytearray()
    i, n = 0, len(data)
    while i < n:
        b = data[i]
        if b == 0x25:  # %
            hi = _hex(data[i + 1]) if i + 1 < n else -1
            lo = _hex(data[i + 2]) if i + 2 < n else -1
            if hi < 0 or lo < 0:
                raise PercentDecodeError("malformed_escape", i)
            out.append(hi << 4 | lo)
            i += 3
        elif b == 0x2B and plus_is_space:  # +
            out.append(0x20)
            i += 1
        else:
            out.append(b)
            i += 1
    return bytes(out)


def percent_decode(text: str, plus_is_space: bool) -> str:
    """Decode ``%XX`` escapes in ``text`` exactly once, refusing malformed escapes and
    invalid UTF-8.

    The rules are :func:`percent_decode_bytes`'s, applied to ``text``'s UTF-8 bytes (so a
    ``"malformed_escape"`` offset counts UTF-8 bytes, not characters); then the result
    must be strict UTF-8 (no overlong form, surrogate or truncated sequence), or
    :class:`PercentDecodeError` (``"invalid_utf8"``) is raised. Unlike
    ``urllib.parse.unquote``, nothing malformed is kept and nothing is replaced. Decoding
    does not make the value safe for HTML: it still goes through :func:`html_text`,
    :func:`attr` or :func:`safe_href` for its destination.
    """
    decoded = percent_decode_bytes(text.encode("utf-8"), plus_is_space)
    try:
        return decoded.decode("utf-8")
    except UnicodeDecodeError as e:
        raise PercentDecodeError("invalid_utf8") from e


def safe_href(href: str) -> str | None:
    """The link target to put in ``href`` when it came from data, or ``None``.

    ``None`` means: not a link, render the label as text. Otherwise the answer is the
    target trimmed of leading and trailing spaces and C0 controls, still unescaped (put it
    in a quoted attribute through :func:`attr`). The trimmed target must hold no C0
    control or DEL, and start with ``http://`` or ``https://`` in any ASCII case, or with
    a single ``/`` (not ``//`` or ``/\\``).
    """
    href = href.strip(_TRIM)
    if any(c < " " or c == "\x7f" for c in href):
        return None
    lower = href[:8].lower()
    if lower.startswith(("https://", "http://")):
        return href
    if href.startswith("/") and not href.startswith(("//", "/\\")):
        return href
    return None


def _json_string(s: str) -> str:
    out = ['"']
    for c in s:
        if c == '"':
            out.append('\\"')
        elif c == "\\":
            out.append("\\\\")
        elif c in _JSON_ESC:
            out.append(_JSON_ESC[c])
        elif c < " ":
            out.append(f"\\u{ord(c):04x}")
        else:
            out.append(c)
    out.append('"')
    return "".join(out)


_JSON_ESC = {"\n": "\\n", "\r": "\\r", "\t": "\\t", "\b": "\\b", "\f": "\\f"}


def json_attr(pairs: Iterable[tuple[str, str]]) -> str:
    """``pairs`` as a JSON object of strings, escaped for a quoted attribute.

    The value of htmx's ``hx-vals``. Members keep the order given, with no whitespace; a
    duplicate key is written as given. The object then goes through :func:`attr`.
    """
    body = ",".join(f"{_json_string(k)}:{_json_string(v)}" for k, v in pairs)
    return attr("{" + body + "}")
