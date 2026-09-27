"""URI templates: the grammar a FAMILY of resources is declared over.

A line-for-line mirror of ``ikigai_core::UriTemplate``
(``ikigai-core/crates/ikigai-core/src/grammar.rs``, ``UriTemplate::parse`` and
``match_str``), because the host and this peer must agree on which IRIs a
pattern names: the host replays a peer's catalog patterns to label what it
forwards, and a family whose two sides disagreed would answer names the host
never routes here (or the reverse).

The rule, exactly as core states it:

* **Level 1 only** — ``{var}``. RFC 6570 operators (``{+var}``, ``{?q}``,
  ``{/p}``) are out of scope, as in core. A variable name is one or more ASCII
  letters, digits or ``_``; anything else between the braces is an error.
* **Literal text matches verbatim.** A ``}`` with no ``{`` before it is literal.
* **A variable followed by a literal captures up to the LEFTMOST occurrence of
  that literal** — lazy, not greedy. So ``urn:r:{x}:{y}`` against
  ``urn:r:1:2:3`` binds ``x = "1"`` and ``y = "2:3"``.
* **A trailing variable captures the whole remainder**, separators included.
* **Every capture is non-empty.** ``urn:r:{x}`` does not match ``urn:r:``.
* **No other character class is imposed.** A capture may hold ``:``, ``/``,
  ``{``, anything — a template constrains shape, never content; content is the
  handler's to judge (the tic-tac-toe store refuses ``01`` for itself).
* **Adjacent variables** (``{a}{b}``) are refused at parse time as ambiguous;
  so are an unclosed ``{`` and an empty ``{}``.

A template with no variables is an exact IRI, which is how this face binds a
plain ``@endpoint`` too: one matcher for both, the way the host's name map does
it. And a template matches its OWN source text (each ``{x}`` captures the
literal ``{x}``), which is what lets a Meta request for the catalog pattern
itself be answered with the family's description.

Ordering between two templates that could both match is NOT this module's
business: the served space tries them in declaration order and the first match
wins, like core's ``EndpointSpace``.
"""

from __future__ import annotations


class TemplateError(ValueError):
    """A malformed or ambiguous template (``ikigai_core::TemplateError``)."""

    def __init__(self, detail: str):
        self.detail = detail
        super().__init__(f"invalid URI template: {detail}")


def _is_var_name(name: str) -> bool:
    # Core: `c.is_ascii_alphanumeric() || c == '_'`. `str.isalnum` alone is
    # Unicode-wide (it accepts `é` and `٣`), so the ASCII test comes first.
    return bool(name) and all(c.isascii() and (c.isalnum() or c == "_") for c in name)


class UriTemplate:
    """A parsed Level 1 URI template. See the module docstring for the rule."""

    __slots__ = ("source", "_parts")

    def __init__(self, source: str):
        parts: list[tuple[bool, str]] = []  # (is_var, text)
        rest = source
        while (rel := rest.find("{")) != -1:
            if rel > 0:
                parts.append((False, rest[:rel]))
            close_rel = rest.find("}", rel)
            if close_rel == -1:
                raise TemplateError(f"unclosed '{{' in `{source}`")
            name = rest[rel + 1 : close_rel]
            if not _is_var_name(name):
                raise TemplateError(f"invalid variable `{{{name}}}` in `{source}`")
            parts.append((True, name))
            rest = rest[close_rel + 1 :]
        if rest:
            parts.append((False, rest))
        for (a_var, _), (b_var, _) in zip(parts, parts[1:], strict=False):
            if a_var and b_var:
                raise TemplateError(f"adjacent variables are ambiguous in `{source}`")
        self.source = source
        self._parts = tuple(parts)

    @property
    def variables(self) -> tuple[str, ...]:
        """The variable names, in order of appearance."""
        return tuple(text for is_var, text in self._parts if is_var)

    @property
    def is_exact(self) -> bool:
        """No variables: the template names exactly one IRI."""
        return not self.variables

    def match(self, iri: str) -> dict[str, str] | None:
        """The bindings if ``iri`` matches, else ``None``."""
        bindings: dict[str, str] = {}
        pos = 0
        parts = self._parts
        for i, (is_var, text) in enumerate(parts):
            if not is_var:
                if not iri.startswith(text, pos):
                    return None
                pos += len(text)
                continue
            following = parts[i + 1] if i + 1 < len(parts) else None
            if following is not None and not following[0]:
                idx = iri.find(following[1], pos)
                if idx == -1 or idx == pos:
                    return None  # no next literal, or an empty capture
                bindings[text] = iri[pos:idx]
                pos = idx
            else:
                if pos == len(iri):
                    return None  # empty capture
                bindings[text] = iri[pos:]
                pos = len(iri)
        return bindings if pos == len(iri) else None

    def expand(self, bindings: dict[str, str]) -> str | None:
        """Fill the variables in; ``None`` if one is missing."""
        out = []
        for is_var, text in self._parts:
            if is_var:
                if text not in bindings:
                    return None
                out.append(bindings[text])
            else:
                out.append(text)
        return "".join(out)

    def __repr__(self) -> str:
        return f"UriTemplate({self.source!r})"

    def __eq__(self, other) -> bool:
        return isinstance(other, UriTemplate) and other.source == self.source

    def __hash__(self) -> int:
        return hash(self.source)
