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

The markup is the game's own: ``template:game``, ``template:board``, the square and status
templates and ``reply``, sourced from the host and filled HERE, in Python, from the host's
raw resources — ``cell:{x}:{y}``, ``winner``, ``turn``. The output is byte-for-byte what
the host's Rust ``view:board`` / ``view:status`` answer for the same game
(``tests/test_tictactoe_app.py`` checks that against a running host), so the same htmx and
the same stylesheet drive it. The renderer is :func:`fill` plus the three views under it —
the template format's tokenizer (a malformed slot is refused, as the Rust filler refuses
it), an escape table and three functions of a :class:`Game` — and it keeps no state: the
host's kernel caches every template, cell and rule it reads, and a move cuts exactly what it
changed, so each render is a handful of cache hits and nothing here knows it.

Routes (``/`` is the root game, ``/game/{id}/`` game ``id``, ``/game/root/`` the root game
again; the page's ``<base>`` is that prefix, so the markup's relative paths arrive under it):

* ``GET /`` and ``GET /game/{id}/`` — the page: ``template:game`` in a document that loads
  ``/static/htmx-2.0.4.min.js``, ``/static/host.css`` and ``/static/ttt.css``.
* ``GET …/iki/tutorial/ttt/view/board`` and ``…/view/status`` — rendered here.
* ``POST …/iki/tutorial/ttt/view/play/{x}/{y}`` and ``…/view/reset`` — a ``Sink`` of the
  host's ``move:{x}:{y}`` / ``reset``, answered with the ``reply`` template. A refused move
  is answered, not failed, in the error's own words — the text the Rust view shows.

Every other request to a view is answered as ``ttt-host`` answers it, status and body: the
host's HTTP face is ikigai-web's generic edge, and :func:`target` and :func:`respond` are its
rules — the path percent-decoded (``+`` included, as a space) and split with the empty
segments dropped, then joined by ``:`` after ``urn:``; a name that is not an IRI a ``400``;
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

#: The vendored htmx and stylesheets, next to this file, as ikigai-tutorial commit ``4d9440a``
#: has them — the commit the ``ttt-host`` this app is checked against was built from, which
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

# -- the renderer -------------------------------------------------------------------------


class Html(str):
    """A value that goes into a slot as it is: another template's output."""


#: A slot's name: a lower-case letter, then lower-case letters and `-`.
NAME = re.compile(r"[a-z][a-z-]*")

#: An integer in its one plain spelling, as the host's coordinates and slot arguments are.
PLAIN = re.compile(r"0|-?[1-9][0-9]*")

#: The escape the Rust ``escape`` and the book's JS apply (``html.escape`` differs: ``&#x27;``).
ESCAPE = str.maketrans({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"})


def plain(word: str) -> bool:
    """Whether ``word`` is an integer in its one spelling, within a signed 64-bit range."""
    return bool(PLAIN.fullmatch(word)) and -(2**63) <= int(word) < 2**63


def pieces(template: str) -> list[str | tuple[str, tuple[int, ...]]]:
    """``template`` as text and slots, read as the Rust ``fill`` reads it (the template format
    in the tutorial's ``crates/tic-tac-toe/README.md``): every ``{{`` opens a slot that ends at
    the first ``}}`` after it, and an inside that is not ``name`` then plain integers, one
    space before each, is REFUSED — never passed through, since a slot nobody fills would
    reach the page as ``{{…}}``."""

    def refused(detail: str) -> EndpointError:
        return EndpointError(f"a template: {detail}")

    out: list[str | tuple[str, tuple[int, ...]]] = []
    rest = template
    while (start := rest.find("{{")) >= 0:
        out.append(rest[:start])
        after = rest[start + 2 :]
        end = after.find("}}")
        if end < 0:
            raise refused("a `{{` is never closed")
        inside = after[:end]
        name, *words = inside.split(" ")
        if not NAME.fullmatch(name):
            raise refused(f"`{{{{{inside}}}}}` is not a slot")
        for word in words:
            if not plain(word):
                raise refused(f"`{{{{{inside}}}}}` has an argument `{word}`")
        out.append((name, tuple(map(int, words))))
        rest = after[end + 2 :]
    out.append(rest)
    return out


def slots(template: str) -> list[tuple[str, tuple[int, ...]]]:
    """The slots of ``template``, in order: each its name and its integers."""
    return [piece for piece in pieces(template) if isinstance(piece, tuple)]


def fill(template: str, value: Callable[..., str]) -> str:
    """``template`` with every slot replaced by ``value(name, *integers)``: text is escaped
    as it goes in, :class:`Html` is not."""

    def one(piece: str | tuple[str, tuple[int, ...]]) -> str:
        if isinstance(piece, str):
            return piece
        filled = value(piece[0], *piece[1])
        return filled if isinstance(filled, Html) else filled.translate(ESCAPE)

    return "".join(map(one, pieces(template)))


def board(game: Game) -> str:
    """``view:board``: the board template, each ``{{square x y}}`` the square the cell calls
    for — taken if played, else open while the game is on, else closed."""
    over = game.text("winner") != "-"

    def square(_: str, x: int, y: int) -> Html:
        mark = game.text(f"cell:{x}:{y}")
        kind = "square-taken" if mark != "-" else "square-closed" if over else "square-open"
        slots = {"x": str(x), "y": str(y), "mark": mark}
        return Html(fill(game.text(f"template:{kind}"), lambda name: slots[name]))

    return fill(game.text("template:board"), square)


def status(game: Game) -> str:
    """``view:status``: ``X to play.``, ``O has won.`` or ``A draw.``."""
    won = game.text("winner")
    if won == "-":
        kind, mark = "status-turn", game.text("turn")
    elif won == "draw":
        kind, mark = "status-draw", ""
    else:
        kind, mark = "status-won", won
    return fill(game.text(f"template:{kind}"), lambda _: mark)


def reply(game: Game, write: str) -> str:
    """A play's or a reset's answer: Sink ``write`` at the host, then the ``reply`` template —
    what the write said, or its refusal in the kernel's words, and the status."""
    try:
        message = game.sink(write)
    except EndpointError as refused:
        message = said(refused)
    slots = {"message": message, "status": Html(status(game))}
    return fill(game.text("template:reply"), lambda name: slots[name])


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

#: The two characters after a `%`, as Rust's ``u8::from_str_radix(_, 16)`` takes them — a
#: leading `+` included.
HEX_PAIR = re.compile(rb"[0-9A-Fa-f]{2}|\+[0-9A-Fa-f]")


def decoded(raw: str) -> str:
    """A request path as ikigai-web decodes it: ``%XX`` to its byte, ``+`` to a space (in the
    path too), then UTF-8 with the undecodable replaced. ``raw`` is the request line's path,
    which ``http.server`` read as Latin-1."""
    data, out, i = raw.encode("latin-1"), bytearray(), 0
    while i < len(data):
        if data[i] == ord("%") and HEX_PAIR.fullmatch(data[i + 1 : i + 3]):
            out.append(int(data[i + 1 : i + 3], 16))
            i += 3
        else:
            out.append(ord(" ") if data[i] == ord("+") else data[i])
            i += 1
    return out.decode("utf-8", "replace")


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
#: path of ``ipchar``, then an optional query and fragment. A path here never holds a `/`.
URN = re.compile(rf"urn:{IPCHAR}*(?:\?(?:{IPCHAR}|[/?{IPRIVATE}])*)?(?:#(?:{IPCHAR}|[/?])*)?")

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
    """The view ``path`` names, or ``None`` for a path that is not one of this app's views."""
    segments = [s for s in decoded(path).split("/") if s]
    match segments:
        case []:
            return Target("page", None, "urn:ttt-host:page:root")
        case ["game", game]:
            return Target("page", game, f"urn:ttt-host:page:game:{game}")
        case ["static", name] if name in STATIC_FILES:
            return Target("static", None, f"urn:ttt-host:static:{name}", name)
    iri = "urn:" + ":".join(segments)
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
    shell = fill(game.text("template:game"), lambda _: label)
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
        verb, view = self.command, target(self.path.split("?")[0])
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
