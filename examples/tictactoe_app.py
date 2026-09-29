"""The tic-tac-toe board, served by a Python app that renders it from the game's resources.

The game lives in ``ttt-host`` (the ikigai tutorial's ``crates/ttt-host``): a Rust kernel
holding the rules, the lines, the board, whose turn it is — everything above the stored
cell. This app is a CLIENT of it over the IPC socket, and a web server for a browser::

    ttt-host --socket /tmp/ttt/host.sock --game a --game b &
    python -m examples.tictactoe_app --socket /tmp/ttt/host.sock
    open http://127.0.0.1:8072/game/a/

(``--socket`` defaults to ``ttt-host``'s own default, ``ttt-host.sock`` in the temp dir;
``--http`` to port 8072, beside ``ttt-host``'s 8070 and the Deno app's 8071, so all three can
serve one host at once.)

The markup is the game's own: its views are ikigai-fn templates (``template:{name}`` at the
host), and this app fills them HERE, in Python — it implements the template language the
tutorial's ``crates/tic-tac-toe/README.md`` states ("The template language": the ``$h``,
``$r`` and ``$a`` markers, ``{x}`` arguments and ``urn:iki:fn:conditional``) and composes each
view itself from that README's table (``view:square:{x}:{y}`` is ``template:square`` with
those arguments), asking the host only for templates and raw resources — ``cell:{x}:{y}``,
``winner``, ``turn``. Which template a view shows is not code here: the templates choose,
through ``conditional``. The output is byte-for-byte what the host's own ``view:board``,
``view:status``, ``view:reply`` and page answer for the same game (``tests/test_tictactoe_app.py``
checks that against a running host), so the same htmx and the same stylesheet drive it. The
filler is :func:`fill` and :func:`conditional`, and it keeps no state: the host's kernel caches
every template, cell and rule it reads, and a move cuts exactly what it changed, so each
render is a handful of cache hits and nothing here knows it.

Routes (``/`` is the root game, ``/game/{id}/`` game ``id``, ``/game/root/`` the root game
again; the page's ``<base>`` is that prefix, so the markup's relative paths arrive under it):

* ``GET /`` and ``GET /game/{id}/`` — the page: ``view:game:{id}`` in a document that loads
  ``/static/htmx-2.0.4.min.js``, ``/static/host.css`` and ``/static/ttt.css``.
* ``GET …/iki/tutorial/ttt/view/board`` and ``…/view/status`` — composed here.
* ``POST …/iki/tutorial/ttt/view/play/{x}/{y}`` and ``…/view/reset`` — a ``Sink`` of the
  host's ``move:{x}:{y}`` / ``reset``, answered with ``view:reply``. A refused move
  is answered, not failed, in the error's own words — the text the Rust view shows.

Every other request to a view is answered as ``ttt-host`` answers it, status and body: the
host's HTTP face is ikigai-web's generic edge, and :func:`target` and :func:`respond` are its
rules, as cli 0.1.30 hardened them — the target split on ``/`` first and each segment
percent-decoded on its own (``%2F`` is data, ``+`` is ``+``; a malformed escape or bytes that
are not UTF-8 a ``400``, in the path and the query alike), the empty segments dropped, then
joined by ``:`` after ``urn:``; a name that is not an IRI a ``400``;
a method a view does not take a ``405``, ``PUT`` a play as ``POST`` is; an unknown game a
``404`` that names the name; a coordinate spelled any way but its one plain way a ``400`` in
the kernel's words. A path that names no view is ``404 not found``: the app is not a proxy
for the host's other names. The game is where the page is — never the request body, a query
or a header — and reaches the host as the name prefix its IPC gateway answers,
``urn:game:{id}:iki:tutorial:ttt:…``; the root game's names are plain.

Two honest limits, both about the kernel in the middle:

* **Every write goes through the host.** The host cuts a stored cell's golden thread when
  ITS kernel issues the Sink; a write straight to a store behind its back would leave the
  host serving the old board. So this app Sinks the host's ``move``, never a store.
* **It renders; it never computes the game.** Reading ``winner`` is asking the host, whose
  kernel records that the answer depends on eight lines of three cells each. A winner
  computed here from the cells would be a traccessor: an answer the host cannot see the
  dependencies of, and so could never cut.

One board per page, so the squares keep the template's game-free ids (htmx restores focus by
id after a swap). A page with two boards would have to prefix each board's ids with its game,
as the book's in-page shim does.
"""

from __future__ import annotations

import argparse
import contextlib
import re
import sys
import tempfile
import urllib.parse
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import NamedTuple

import ikigai
from ikigai import (
    ConflictError,
    ConnectionLost,
    DeniedError,
    EndpointError,
    InvalidArgumentError,
    MissingArgumentError,
    NotFoundError,
    TimeoutError,
    UnavailableError,
    UnresolvedError,
)

#: The game's names at the host, as the root game has them.
GAME = "urn:iki:tutorial:ttt:"

#: Where a game other than the root has its names, at the host's edge: ``urn:game:{id}:…``.
EDGE_GAMES = "urn:game:"

#: The vendored htmx and stylesheets, next to this file, as ikigai-tutorial commit ``89677bc``
#: has them (unchanged since ``4d9440a``) — the commit the ``ttt-host`` this app is checked
#: against was built from, which
#: serves the same three files. The digests are pinned by a test: htmx is the book's
#: ``src/vendor/htmx-2.0.4.min.js`` (0BSD, https://github.com/bigskysoftware/htmx),
#: ``ttt.css`` the book's ``css/ttt.css`` (the ONE stylesheet for this markup), ``host.css``
#: ``ttt-host``'s ``static/host.css`` (the color variables ``ttt.css`` reads, which the
#: book's pages get from mdbook). Re-vendor them from the commit a new host is built from.
STATIC = Path(__file__).parent / "static"
STATIC_FILES = {
    "htmx-2.0.4.min.js": (
        "text/javascript",
        "e209dda5c8235479f3166defc7750e1dbcd5a5c1808b7792fc2e6733768fb447",
    ),
    "ttt.css": ("text/css", "f93bde4b6dacb82b085d88c8cf33c899eb3dd435dd64acd5e1e19c63be04f09b"),
    "host.css": ("text/css", "79437443cd22e56d183ebf5b4a6de625e72354d38e8a39e54894bf0dc19f27ac"),
}

#: ``ttt-host``'s page policy: ``base-uri 'self'``, because the page's ``<base>`` is what
#: makes the markup's relative paths land under the game.
PAGE_CSP = "default-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"

HTML = "text/html;charset=utf-8"  # as ttt-host spells it: no space

#: Beside ``ttt-host``'s 8070 and the Deno app's 8071.
DEFAULT_HTTP = "127.0.0.1:8072"

# -- the filler: the template language ----------------------------------------------------
#
# ikigai-fn's compose, the subset these templates use, as the tutorial's README states it
# ("The template language"; its `template-cases` block is tests/ttt_template_cases.txt). What is
# here is the language: the scan, the three splices, the arguments and `conditional`. Which
# template a view shows is not here: that is in the templates.

#: The escape ``$h`` applies (``html.escape`` differs: it writes ``&#x27;``).
ESCAPE = str.maketrans({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"})

#: What Rust's ``str::trim`` trims: Unicode ``White_Space``, which is what ``str.isspace``
#: accepts less U+001C to U+001F (so ``str.strip()`` with no argument would strip too much).
WHITESPACE = "".join(
    c for c in map(chr, range(0x3001)) if c.isspace() and not "\x1c" <= c <= "\x1f"
)

#: A template argument's name: a letter or `_`, then letters, digits, `_` and `-`.
ARG = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")

#: The one function the templates call. ``ttt-host``'s gateway forwards only the game's names,
#: so the filler answers it.
CONDITIONAL = "urn:iki:fn:conditional"

#: How deep ``$a`` may nest before it is a cycle (ikigai-fn's backstop).
DEPTH = 32

#: A marker's request: its IRI and its arguments, answered as text.
Source = Callable[[str, dict[str, str]], str]


def refused(detail: str) -> EndpointError:
    return EndpointError(f"compose: {detail}")


def unquoted(text: str, i: int, sep: str | None = None) -> int:
    """The index in ``text``, from ``i``, of the first `}` that closes nothing opened after
    ``i`` (or of ``sep``, if given) outside a `"…"` span, where `\\"` and `\\\\` are escapes;
    ``-1`` if there is none."""
    depth, quoted = 0, False
    while i < len(text):
        c = text[i]
        if quoted and c == "\\":
            i += 1
        elif c == '"':
            quoted = not quoted
        elif quoted:
            pass
        elif sep is not None:
            if text.startswith(sep, i):
                return i
        elif c == "{":
            depth += 1
        elif c == "}":
            if depth == 0:
                return i
            depth -= 1
        i += 1
    return -1


def split(text: str, sep: str) -> list[str]:
    """``text`` split on every ``sep`` outside a `"…"` span."""
    parts = []
    while (at := unquoted(text, 0, sep)) >= 0:
        parts.append(text[:at])
        text = text[at + len(sep) :]
    return [*parts, text]


def scan(template: str) -> list[str | tuple[str, str]]:
    """``template`` as literal text and markers, each its letter and its trimmed body. `$$` is
    a literal `$`; a marker that never closes is literal text, `$` included."""
    out: list[str | tuple[str, str]] = []
    text, i = [], 0
    while i < len(template):
        if template.startswith("$$", i):
            text.append("$")
            i += 2
            continue
        if template[i] == "$" and template[i + 1 : i + 2] in ("a", "r", "h"):
            end = unquoted(template, i + 3) if template[i + 2 : i + 3] == "{" else -1
            if end >= 0:
                out += ["".join(text), (template[i + 1], template[i + 3 : end].strip(WHITESPACE))]
                text, i = [], end + 1
                continue
        text.append(template[i])
        i += 1
    return [*out, "".join(text)]


def argument(args: dict[str, str], name: str) -> str:
    if name not in args:
        raise MissingArgumentError(name)
    return args[name]


def placeholders(text: str) -> str | None:
    """Why ``text`` (an IRI, or an unquoted argument value) is malformed: a `{` that does not
    open a `{name}` argument. ``None`` if it is not."""
    rest = text
    while (start := rest.find("{")) >= 0:
        end = rest.find("}", start)
        if end < 0:
            return "a `{` is never closed"
        if not ARG.fullmatch(name := rest[start + 1 : end]):
            return f"`{{{name}}}` is not an argument name"
        rest = rest[end + 1 :]
    return None


def substitute(template: str, args: dict[str, str], encode: bool) -> str:
    """``template`` with each `{name}` replaced by that argument: percent-encoded as RFC 6570
    expands a simple variable (in an IRI), or verbatim (in an argument value). Its
    placeholders were checked when its marker was parsed."""
    out, rest = [], template
    while (start := rest.find("{")) >= 0:
        end = rest.find("}", start)
        value = argument(args, rest[start + 1 : end])
        out += [rest[:start], urllib.parse.quote(value, safe="") if encode else value]
        rest = rest[end + 1 :]
    return "".join(out) + rest


def unquote(word: str) -> str | None:
    """A `"quoted"` argument value's text, with `\\"` and `\\\\` unescaped; ``None`` if the
    value is not quoted (then its arguments are substituted verbatim, and it is one value)."""
    if len(word) >= 2 and word[0] == word[-1] == '"':
        return re.sub(r"\\(.)", r"\1", word[1:-1], flags=re.S)
    return None


class Marker(NamedTuple):
    """A parsed marker: how it splices (``a``, ``r`` or ``h``), its body as written, and what
    it names — a template ``argument``, or a request for ``iri`` with ``query``, each pair
    ``(key, text, literal)``."""

    mode: str
    body: str
    argument: str | None
    iri: str = ""
    query: tuple[tuple[str, str, bool], ...] = ()


def parse(mode: str, body: str) -> Marker:
    """One marker's body, or the template's refusal. A marker that cannot be parsed is the
    template's fault, so it is found before anything resolves."""

    def malformed(detail: str) -> EndpointError:
        return refused(f"marker `{body}`: {detail}")

    if not body:
        raise malformed("an empty alternative")
    if len(split(body, "||")) > 1:
        raise malformed("this filler has no `||` fallbacks")
    name = body[1:-1] if body[:1] == "{" and body[-1:] == "}" else ""
    if ARG.fullmatch(name):
        if mode == "a":
            raise malformed(
                f"`{{{name}}}` is an argument, a value and never a template — splice it with "
                "`$h` or `$r`, not `$a`"
            )
        return Marker(mode, body, name)
    iri, _, query = body.partition("?")
    iri = iri.strip(WHITESPACE)
    if detail := placeholders(iri):
        raise malformed(detail)
    pairs = []
    for pair in filter(None, (pair.strip(WHITESPACE) for pair in split(query, "&"))):
        key, eq, word = pair.partition("=")
        if not eq:
            raise refused(f"marker argument `{pair}` is not key=value")
        word = word.strip(WHITESPACE)
        if (text := unquote(word)) is None and (detail := placeholders(word)):
            raise malformed(detail)
        pairs.append((key.strip(WHITESPACE), word if text is None else text, text is not None))
    return Marker(mode, body, None, iri, tuple(pairs))


def fill(template: str, args: dict[str, str], source: Source, depth: int = 0) -> str:
    """``template`` with every marker spliced: ``args`` are the template arguments, and
    ``source`` answers every request but ``conditional``, which is answered here.

    ikigai-fn's order, level by level: EVERY marker is parsed before any resolves (so a
    malformed marker is refused as malformed, whatever an earlier marker's request would have
    done); then every marker's request is answered, in document order; then each is spliced in
    document order, a ``$a`` filling what it named one level down. A marker that fails fails the
    whole fill, and the first failure in document order is the one raised."""
    if depth >= DEPTH:
        raise refused(f"recursion limit ({DEPTH}) exceeded — cyclic transclusion?")
    parsed = [p if isinstance(p, str) else parse(*p) for p in scan(template)]
    answers = [p if isinstance(p, str) else answer(p, args, source) for p in parsed]
    return "".join(
        p if isinstance(p, str) else splice(p, a, args, source, depth)
        for p, a in zip(parsed, answers, strict=True)
    )


def answer(marker: Marker, args: dict[str, str], source: Source) -> str | EndpointError:
    """What ``marker`` names — an argument's value or a request's answer — or why it could not
    be had, kept (not raised) until the marker's turn to splice."""
    try:
        if marker.argument is not None:
            return argument(args, marker.argument)
        request = substitute(marker.iri, args, encode=True)
        given = {k: t if lit else substitute(t, args, encode=False) for k, t, lit in marker.query}
        return conditional(given, source) if request == CONDITIONAL else source(request, given)
    except EndpointError as failed:
        return failed


def splice(
    marker: Marker, value: str | EndpointError, args: dict[str, str], source: Source, depth: int
) -> str:
    """One marker: `$h` escapes what it names, `$r` splices it as it is, `$a` fills it."""
    if isinstance(value, EndpointError):
        raise value
    if marker.mode == "a":
        return fill(value, args, source, depth + 1)
    return value.translate(ESCAPE) if marker.mode == "h" else value


#: ``conditional``'s reading of ``if`` when there is no ``equals``.
BOOLEAN = {"true": True, "1": True, "yes": True, "on": True}
BOOLEAN |= {"false": False, "0": False, "no": False, "off": False, "": False}


def conditional(given: dict[str, str], source: Source) -> str:
    """``urn:iki:fn:conditional``: source ``if``; if its trimmed text is ``equals`` (or, with
    no ``equals``, a true boolean), source and answer ``then``, else ``else`` (or nothing).
    Only the chosen side is sourced."""
    test, then = argument(given, "if"), argument(given, "then")
    verdict = source(test, {}).strip(WHITESPACE)
    if "equals" in given:
        taken = verdict == given["equals"]
    elif (taken := BOOLEAN.get(verdict.lower())) is None:
        # ikigai-fn's own words, and its class: a failed request, not compose's refusal.
        shown = '"' + verdict.lower().replace("\\", "\\\\").replace('"', '\\"') + '"'
        raise EndpointError(
            f"conditional: `{test}` returned {shown}, not a boolean (true/false/1/0/yes/no)"
        )
    chosen = then if taken else given.get("else")
    return "" if chosen is None else source(chosen, {})


# -- the views: composed here, by the README's table ----------------------------------------

#: Each view the app composes itself (its name after ``urn:iki:tutorial:ttt:``) and the
#: template it fills, with what the name captures as arguments.
VIEWS = [
    (re.compile(r"view:board"), "template:board"),
    (re.compile(r"view:status"), "template:status"),
    (re.compile(r"view:reply"), "template:reply"),
    (re.compile(r"view:square:(?P<x>[^:]+):(?P<y>.+)"), "template:square"),
    (re.compile(r"view:game:(?P<game>.+)"), "template:game"),
]


def board(game: Game) -> str:
    """``view:board``."""
    return game.view("view:board")


def status(game: Game) -> str:
    """``view:status``: ``X to play.``, ``O has won.`` or ``A draw.``."""
    return game.view("view:status")


def reply(game: Game, write: str) -> str:
    """A play's or a reset's answer: Sink ``write`` at the host, then ``view:reply`` with
    ``message`` what the write said, or its refusal in the kernel's words."""
    try:
        message = game.sink(write)
    except EndpointError as refused:
        message = said(refused)
    return game.view("view:reply", message=message)


# -- the game at the host -----------------------------------------------------------------

#: How ``ikigai_core::Error`` displays each variant, which is what the Rust view renders a
#: refusal as. The wire's typed errors carry the endpoint's own message and the variant as a
#: TYPE, so a Python ``str()`` omits these prefixes; the other variants' ``str()`` is
#: already the Rust display.
DISPLAYED = [
    (DeniedError, "denied: "),
    (NotFoundError, "not found: "),
    (TimeoutError, "timeout: "),
    (UnavailableError, "unavailable: "),
    (ConflictError, "conflict: "),
]


def said(error: EndpointError) -> str:
    """``error`` as the Rust kernel's ``Display`` spells it."""
    if type(error) is EndpointError:
        return f"endpoint error: {error.message}"
    prefix = next((p for kind, p in DISPLAYED if isinstance(error, kind)), "")
    return prefix + str(error)


class Game:
    """One game's names at the host, over one connection. ``prefix`` is where the host
    answers the game's ``urn:iki:tutorial:ttt:`` names: as they are for the root game,
    ``urn:game:{id}:iki:tutorial:ttt:`` for game ``id`` (``ttt-host``'s IPC gateway)."""

    def __init__(self, kernel: ikigai.Client, game_id: str | None):
        self.kernel = kernel
        self.game_id = game_id
        self.prefix = GAME if game_id is None else f"urn:game:{game_id}:{GAME[len('urn:') :]}"

    def text(self, name: str) -> str:
        return self.kernel.source(self.prefix + name).text

    def sink(self, name: str) -> str:
        return self.kernel.sink(self.prefix + name).text

    def source(self, iri: str, given: dict[str, str]) -> str:
        """A template's request, in this game: a view is composed HERE, its template filled
        with what its name captures, else ``given``; any other name is asked of the host. A
        template spells every name as the root game has it, so a name of the game is asked
        at the game's prefix."""
        name = iri.removeprefix(GAME)
        for pattern, template in VIEWS if iri.startswith(GAME) else []:
            if captured := pattern.fullmatch(name):
                return fill(self.text(template), given | captured.groupdict(), self.source)
        where = self.prefix + name if iri.startswith(GAME) else iri
        return self.kernel.source(where, **given).text

    def view(self, name: str, **given: str) -> str:
        """The view ``name`` (after ``urn:iki:tutorial:ttt:``) composed here, with ``given``."""
        return self.source(GAME + name, given)

    def exists(self) -> bool:
        """Whether the host has this game — asked of the game itself, since the catalog does
        not list every name a game answers at (the root game's ``urn:game:root:…``)."""
        try:
            self.text("turn")
        except UnresolvedError:
            return False
        return True

    def games(self) -> list[str]:
        """Every game the host has but the root, from its catalog's gateway names."""
        pattern = re.compile(r"urn:game:([A-Za-z0-9-]+):iki:tutorial:ttt:")
        found = (pattern.match(entry.pattern) for entry in self.kernel.entries() or [])
        return sorted({m[1] for m in found if m})


# -- the edge: ttt-host's HTTP rules, which this app answers by -------------------------
#
# The host's HTTP face is ikigai-web: a path is percent-decoded, split on `/` with the empty
# segments dropped, and matched against the host's routes (`/`, `/game/{id}`, the three
# static files); anything else is joined by `:` after `urn:` and resolved. Mirroring those
# rules is what makes this app answer every VIEW path as the host does — the status and the
# body — down to a trailing slash, an unknown game, a wrong method and a coordinate spelled
# the wrong way. A path that is not a view is not this app's business: it is not a proxy for
# the host's other names, so it answers `404 not found`, whatever the method.

#: A `%` that is not followed by exactly two hex digits.
MALFORMED = re.compile(rb"%(?![0-9A-Fa-f]{2})")


def unescaped(raw: bytes, form: bool = False) -> str:
    """``raw`` percent-decoded as ikigai-web (cli 0.1.30) decodes it: as bytes, then UTF-8. A
    `%` not followed by two hex digits is refused, never guessed at, and so are bytes that
    are not UTF-8; ``+`` is a space only in ``form`` encoding, which only a query is. A
    refusal is a :class:`ValueError` in the host's words."""
    if MALFORMED.search(raw):
        raise ValueError("malformed percent-escape")
    try:
        return urllib.parse.unquote_to_bytes(raw.replace(b"+", b" ") if form else raw).decode()
    except UnicodeDecodeError:
        raise ValueError("not UTF-8 once decoded") from None


def segments(raw: str) -> list[str]:
    """A request target's path as ikigai-web reads it: split on `/` FIRST, each segment
    decoded on its own (so `%2F` is data in its segment, never a separator), and the empty
    ones dropped. The query is decoded too, for its refusals only: no view reads it. ``raw``
    is the request line's target, which ``http.server`` read as Latin-1."""
    path, _, query = raw.encode("latin-1").partition(b"?")
    for pair in filter(None, query.split(b"&")):
        for part in pair.partition(b"=")[::2]:  # the key and the value
            unescaped(part, form=True)
    return [s for s in [unescaped(segment) for segment in path.split(b"/")] if s]


def _ranges(*spans: tuple[int, int]) -> str:
    return "".join(f"{chr(lo)}-{chr(hi)}" for lo, hi in spans)


#: RFC 3987's ``ucschar`` and ``iprivate``, as regular-expression character ranges.
UCSCHAR = _ranges(
    (0xA0, 0xD7FF),
    (0xF900, 0xFDCF),
    (0xFDF0, 0xFFEF),
    *((plane, plane + 0xFFFD) for plane in range(0x10000, 0xE0000, 0x10000)),
    (0xE1000, 0xEFFFD),
)
IPRIVATE = _ranges((0xE000, 0xF8FF), (0xF0000, 0xFFFFD), (0x100000, 0x10FFFD))
IPCHAR = rf"(?:[A-Za-z0-9._~!$&'()*+,;=:@{UCSCHAR}-]|%[0-9A-Fa-f]{{2}})"

#: A ``urn:`` name the host's kernel can parse (RFC 3987, which ``oxiri`` checks for it): a
#: path of ``ipchar`` and `/` (which only a segment's `%2F` puts there), then an optional
#: query and fragment.
URN = re.compile(rf"urn:(?:{IPCHAR}|/)*(?:\?(?:{IPCHAR}|[/?{IPRIVATE}])*)?(?:#(?:{IPCHAR}|[/?])*)?")

#: ``view:play:{x}:{y}`` as the kernel's template captures it: ``x`` up to the next `:`,
#: ``y`` the rest, neither empty.
PLAY = re.compile(r"view:play:([^:]+):(.+)")

#: The methods a view offers, as ikigai-web lists them from its declared verbs.
READ, WRITE = ("GET", "HEAD"), ("POST", "PUT", "PATCH")
ALLOW = {READ: "GET, HEAD, OPTIONS", WRITE: "POST, PUT, PATCH, OPTIONS"}


class Target(NamedTuple):
    """A view a request path names: its kind, its game (``None`` for the root), the name the
    host resolves it by (what its not-found message quotes), and a play's coordinates as
    spelled — or, for a static file, the file."""

    kind: str
    game: str | None
    iri: str
    detail: tuple[str, str] | str | None = None

    @property
    def methods(self) -> tuple[str, ...]:
        return WRITE if self.kind in ("play", "reset") else READ


def target(path: str) -> Target | None:
    """The view ``path`` names, or ``None`` for a path that is not one of this app's views;
    a :class:`ValueError` for a target the host refuses before it routes anything."""
    match names := segments(path):
        case []:
            return Target("page", None, "urn:ttt-host:page:root")
        case ["game", game]:
            return Target("page", game, f"urn:ttt-host:page:game:{game}")
        case ["static", name] if name in STATIC_FILES:
            return Target("static", None, f"urn:ttt-host:static:{name}", name)
    iri = "urn:" + ":".join(names)
    game, _, rest = iri.removeprefix(EDGE_GAMES).partition(":")
    if iri.startswith(EDGE_GAMES) and f"urn:{rest}".startswith(GAME):
        local = f"urn:{rest}".removeprefix(GAME)
    elif iri.startswith(GAME):
        game, local = None, iri.removeprefix(GAME)
    else:
        return None
    if local in ("view:board", "view:status", "view:reset"):
        return Target(local.removeprefix("view:"), game, iri)
    play = PLAY.fullmatch(local)
    return Target("play", game, iri, play.groups()) if play else None


def page(game: Game) -> str:
    """A game's page: the document around the game's ``game`` template, its ``<base>`` at
    the game's path so the markup's relative paths arrive under it — ``ttt-host``'s page,
    byte for byte, down to the list of the host's games (read from its catalog)."""
    game_id = game.game_id
    label = game_id or "root"
    shell = game.view(f"view:game:{label}")
    title = (f"game {label}" if game_id else "the root game").translate(ESCAPE)
    base = f"/game/{game_id}/" if game_id else "/"
    return (
        '<!DOCTYPE html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '<meta name="htmx-config" content=\''
        '{"allowEval":false,"includeIndicatorStyles":false,"historyEnabled":false}\'>\n'
        f'<title>Tic-tac-toe: {title}</title>\n<base href="{base}">\n'
        '<link rel="stylesheet" href="/static/host.css">\n'
        '<link rel="stylesheet" href="/static/ttt.css">\n'
        '<script src="/static/htmx-2.0.4.min.js"></script>\n</head>\n<body>\n<main>\n'
        f"<h1>Tic-tac-toe: {title}</h1>\n"
        f'<div class="ttt-boards"><div class="ttt-play">\n{shell}\n</div></div>\n'
        '<nav aria-label="Games"><h2>Games on this host</h2><ul>'
        '<li><a href="/">the root game</a></li>'
        + "".join(f'<li><a href="/game/{g}/">game {g}</a></li>' for g in game.games())
        + "</ul></nav>\n</main>\n</body>\n</html>\n"
    )


class Refusal(NamedTuple):
    """An answer that is not a view: a status, its plain-text body, and an ``Allow`` list."""

    status: HTTPStatus
    body: str
    allow: str | None = None


#: The methods ikigai-web maps to a verb; any other is its 405 (OPTIONS is answered apart).
METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE")


def respond(
    verb: str, view: Target, described: Callable[[], bool]
) -> Refusal | Callable[[Game], str] | None:
    """How ttt-host answers ``verb`` on ``view``: a refusal, in the order ikigai-web makes
    them, or the renderer to call with the :class:`Game` (``None`` for a static file, which
    needs no game). ``described`` says whether the host
    has the view's game — a game it lacks describes nothing, so no method is refused there
    and the request fails to resolve instead. It is asked only when a refusal depends on it."""
    methods = view.methods
    if verb == "OPTIONS" or verb not in METHODS:
        allow = ALLOW[methods] if described() else ALLOW[READ]
        if verb == "OPTIONS":
            return Refusal(HTTPStatus.NO_CONTENT, "", allow)
        return Refusal(HTTPStatus.METHOD_NOT_ALLOWED, "method not allowed", allow)
    if verb not in methods:
        if described():
            return Refusal(HTTPStatus.METHOD_NOT_ALLOWED, "method not allowed", ALLOW[methods])
        if verb != "PATCH":
            return unresolved(view)
    if verb == "PATCH":  # ikigai-web's read-modify-write, which no view here takes
        return Refusal(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "no patch strategy for this Content-Type")
    if view.kind == "play":
        return lambda game: play(game, *view.detail)
    if view.kind == "reset":
        return lambda game: reply(game, "reset")
    return {"page": page, "board": board, "status": status}.get(view.kind)  # None: a file


def unresolved(view: Target) -> Refusal:
    """The host's answer for a view of a game it does not have: its name did not resolve."""
    return Refusal(HTTPStatus.NOT_FOUND, f"no endpoint resolved for {view.iri}")


def play(game: Game, x: str, y: str) -> str:
    """A play: its coordinates checked by the host's own rule — reading the cell refuses a
    spelling it does not take, as the Rust view does before it moves — then the move."""
    game.text(f"cell:{x}:{y}")
    return reply(game, f"move:{x}:{y}")


#: ikigai-web's status for each typed error (its ``error_resp``); any other is a 500.
STATUS_OF = [
    (DeniedError, HTTPStatus.FORBIDDEN),
    ((NotFoundError, UnresolvedError), HTTPStatus.NOT_FOUND),
    (ConflictError, HTTPStatus.CONFLICT),
    ((MissingArgumentError, InvalidArgumentError), HTTPStatus.BAD_REQUEST),
    ((TimeoutError, UnavailableError), HTTPStatus.SERVICE_UNAVAILABLE),
]


class Handler(BaseHTTPRequestHandler):
    """One request: find its view, open a connection to the host if it needs one, answer."""

    socket_path: Path  # set on the subclass `make_server` builds
    server_version = "ikigai-python-ttt"

    def __getattr__(self, name: str):
        # Every method reaches `handle`, not only those with a `do_` method: ttt-host answers
        # any method on a view, and a wrong one is its 405, not `http.server`'s 501.
        if name.startswith("do_"):
            return self.handle_request
        raise AttributeError(name)

    def handle_request(self) -> None:
        try:
            verb, view = self.command, target(self.path)
        except ValueError as malformed:
            return self.answer(HTTPStatus.BAD_REQUEST, str(malformed))
        if view is None:
            return self.answer(HTTPStatus.NOT_FOUND, "not found")
        if not URN.fullmatch(view.iri):
            return self.answer(HTTPStatus.BAD_REQUEST, "not a resource path")
        with contextlib.ExitStack() as stack:
            kernel: list[ikigai.Client] = []  # connected on first use: a refusal may need none

            def game() -> Game:
                if not kernel:
                    kernel.append(stack.enter_context(ikigai.connect(self.socket_path)))
                return Game(kernel[0], view.game)

            def described() -> bool:
                return view.game is None or game().exists()

            try:
                answer = respond(verb, view, described)
                if isinstance(answer, Refusal):
                    return self.refuse(answer)
                if answer is None:
                    media, _ = STATIC_FILES[view.detail]
                    return self.answer(HTTPStatus.OK, (STATIC / view.detail).read_bytes(), media)
                body = answer(game())
            except ConnectionLost as e:
                return self.answer(HTTPStatus.SERVICE_UNAVAILABLE, f"{e} — is ttt-host running?")
            except EndpointError as e:
                if isinstance(e, UnresolvedError) and not described():
                    return self.refuse(unresolved(view))
                code = next((c for kind, c in STATUS_OF if isinstance(e, kind)), 500)
                return self.answer(code, said(e))
        headers = {"Cache-Control": "no-store"}
        if view.kind == "page":
            headers["Content-Security-Policy"] = PAGE_CSP
        self.answer(HTTPStatus.OK, body, HTML, headers)

    def refuse(self, refusal: Refusal) -> None:
        allow = {} if refusal.allow is None else {"Allow": refusal.allow}
        self.answer(refusal.status, refusal.body, headers=allow)

    def answer(self, code, body, media="text/plain; charset=utf-8", headers=None) -> None:
        """Send ``body`` (text or bytes) as ``media`` with status ``code`` — without the body
        for a HEAD, as for a GET otherwise."""
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        if data or code != HTTPStatus.NO_CONTENT:
            self.send_header("Content-Type", media)
            self.send_header("Content-Length", str(len(data)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def log_message(self, fmt: str, *args) -> None:
        print(f"examples.tictactoe_app: {fmt % args}", file=sys.stderr)


def make_server(socket_path: Path, address: tuple[str, int]) -> ThreadingHTTPServer:
    """The app, bound to ``address``, talking to the host at ``socket_path``."""
    handler = type("TttHandler", (Handler,), {"socket_path": Path(socket_path)})
    return ThreadingHTTPServer(address, handler)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m examples.tictactoe_app")
    parser.add_argument(
        "--socket",
        type=Path,
        default=Path(tempfile.gettempdir()) / "ttt-host.sock",
        help="ttt-host's IPC socket (default: ttt-host's own, ttt-host.sock in the temp dir)",
    )
    parser.add_argument("--http", default=DEFAULT_HTTP, help=f"where to serve ({DEFAULT_HTTP})")
    options = parser.parse_args(argv)
    host, _, port = options.http.rpartition(":")
    server = make_server(options.socket, (host, int(port)))
    print(
        f"examples.tictactoe_app: http://{options.http}/ (the root game), games at "
        f"/game/<id>/; the game is kept by ttt-host at {options.socket}",
        file=sys.stderr,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
