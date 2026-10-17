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

Nothing here makes an unquoted attribute, an event-handler attribute, or the inside of
``<script>`` or ``<style>`` safe.
"""

from __future__ import annotations

from collections.abc import Iterable

__all__ = ["attr", "html_text", "json_attr", "safe_href", "url_segment"]

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
