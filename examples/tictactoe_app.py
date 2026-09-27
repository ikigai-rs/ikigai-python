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
a regular expression, an escape table and three functions of a :class:`Game` — and it keeps
no state: the host's kernel caches every template, cell and rule it reads, and a move cuts
exactly what it changed, so each render is a handful of cache hits and nothing here knows
it.

Routes (``/`` is the root game, ``/game/{id}/`` game ``id``; the page's ``<base>`` is that
prefix, so the markup's relative paths arrive under it):

* ``GET /`` and ``GET /game/{id}/`` — the page: ``template:game`` in a document that loads
  ``/static/htmx-2.0.4.min.js``, ``/static/host.css`` and ``/static/ttt.css``.
* ``GET …/iki/tutorial/ttt/view/board`` and ``…/view/status`` — rendered here.
* ``POST …/iki/tutorial/ttt/view/play/{x}/{y}`` and ``…/view/reset`` — a ``Sink`` of the
  host's ``move:{x}:{y}`` / ``reset``, answered with the ``reply`` template. A refused move
  is answered, not failed, in the error's own words — the text the Rust view shows.

The path ↔ IRI rule is the markup's (the tutorial README states it): a relative path's
segments joined by ``:`` after ``urn:``. The game is where the page is — never the request
body, a query or a header — and reaches the host as the name prefix its IPC gateway
answers, ``urn:game:{id}:iki:tutorial:ttt:…``; the root game's names are plain.

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
import re
import sys
import tempfile
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import ikigai
from ikigai import (
    ConnectionLost,
    DeniedError,
    EndpointError,
    NotFoundError,
    TimeoutError,
    UnavailableError,
    UnresolvedError,
)

#: The game's names at the host, as the root game has them.
GAME = "urn:iki:tutorial:ttt:"

#: The vendored htmx and stylesheets, next to this file. The digests are pinned by a test:
#: htmx is byte-identical to the book's ``src/vendor/htmx-2.0.4.min.js`` (0BSD,
#: https://github.com/bigskysoftware/htmx), ``ttt.css`` to the book's ``css/ttt.css`` (the
#: ONE stylesheet for this markup), ``host.css`` to ``ttt-host``'s ``static/host.css`` (the
#: color variables ``ttt.css`` reads, which the book's pages get from mdbook).
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

HTML = "text/html; charset=utf-8"

#: Beside ``ttt-host``'s 8070 and the Deno app's 8071.
DEFAULT_HTTP = "127.0.0.1:8072"

# -- the renderer -------------------------------------------------------------------------


class Html(str):
    """A value that goes into a slot as it is: another template's output."""


#: A slot: a lower-case name, then plain integers, one space before each.
SLOT = re.compile(r"\{\{([a-z][a-z-]*)((?: -?[0-9]+)*)\}\}")

#: The escape the Rust ``escape`` and the book's JS apply (``html.escape`` differs: ``&#x27;``).
ESCAPE = str.maketrans({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"})


def fill(template: str, value: Callable[..., str]) -> str:
    """``template`` with every slot replaced by ``value(name, *integers)``: text is escaped
    as it goes in, :class:`Html` is not."""

    def one(slot: re.Match) -> str:
        filled = value(slot[1], *map(int, slot[2].split()))
        return filled if isinstance(filled, Html) else filled.translate(ESCAPE)

    return SLOT.sub(one, template)


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


# -- the path <-> IRI rule, and the routes ------------------------------------------------

#: ``/`` or ``/game/{id}/``, then the markup's relative path. Game ids are the host's
#: (``[A-Za-z0-9-]+``).
ROUTE = re.compile(r"/(?:game/(?P<game>[A-Za-z0-9-]+)/)?(?P<path>.*)")

#: A coordinate in its one plain spelling, as the host's coordinates are.
PLAIN = re.compile(r"0|-?[1-9][0-9]*")


def iri_of(path: str) -> str | None:
    """A relative path's IRI: its segments joined by ``:`` after ``urn:`` — or ``None`` for
    a path the markup's rule does not answer (absolute, a URL, an empty/``.``/``..`` segment)."""
    segments = path.split("/")
    if "://" in path or any(s in ("", ".", "..") for s in segments):
        return None
    return "urn:" + ":".join(segments)


def view(verb: str, relative: str) -> Callable[[Game], str] | HTTPStatus:
    """What answers ``verb`` on a game's ``relative`` path, or the status that refuses it:
    the page for the empty path, else the view the path's IRI names."""
    iri = iri_of(relative) if relative else GAME
    local = iri.removeprefix(GAME) if iri and iri.startswith(GAME) else None
    reads = {"": page, "view:board": board, "view:status": status}
    writes = {"view:reset": lambda game: reply(game, "reset")}
    play = re.fullmatch(r"view:play:([^:]+):([^:]+)", local or "")
    if play and all(PLAIN.fullmatch(c) for c in play.groups()):
        writes[local] = lambda game: reply(game, "move:{}:{}".format(*play.groups()))
    wanted, other = (reads, writes) if verb == "GET" else (writes, reads)
    if local in wanted:
        return wanted[local]
    return HTTPStatus.METHOD_NOT_ALLOWED if local in other else HTTPStatus.NOT_FOUND


def page(game: Game) -> str:
    """A game's page: the document around the game's ``game`` template, its ``<base>`` at
    the game's path so the markup's relative paths arrive under it."""
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
        "<p>Rendered in Python from the game's resources; the game is kept by a Rust "
        "kernel.</p>\n</main>\n</body>\n</html>\n"
    )


class Handler(BaseHTTPRequestHandler):
    """One request: route it, open a connection to the host for it, answer."""

    socket_path: Path  # set on the subclass `make_server` builds
    server_version = "ikigai-python-ttt"

    def do_GET(self) -> None:
        self.handle_verb("GET")

    def do_POST(self) -> None:
        self.handle_verb("POST")

    def handle_verb(self, verb: str) -> None:
        path = self.path.split("?")[0]
        if verb == "GET" and path.startswith("/static/"):
            return self.static(path.removeprefix("/static/"))
        if re.fullmatch(r"/game/[A-Za-z0-9-]+", path):  # the page's <base> needs the slash
            return self.answer(HTTPStatus.MOVED_PERMANENTLY, "", headers={"Location": path + "/"})
        route = ROUTE.fullmatch(path)
        answer = HTTPStatus.NOT_FOUND if route is None else view(verb, route["path"])
        if isinstance(answer, HTTPStatus):
            return self.answer(answer, f"{verb} {path} is not answered here")
        try:
            with ikigai.connect(self.socket_path) as kernel:
                body = answer(Game(kernel, route["game"]))
        except UnresolvedError as e:  # a game the host does not have
            return self.answer(HTTPStatus.NOT_FOUND, str(e))
        except ConnectionLost as e:
            return self.answer(HTTPStatus.SERVICE_UNAVAILABLE, f"{e} — is ttt-host running?")
        except EndpointError as e:
            return self.answer(HTTPStatus.BAD_GATEWAY, said(e))
        headers = {"Cache-Control": "no-store"}
        if answer is page:
            headers["Content-Security-Policy"] = PAGE_CSP
        self.answer(HTTPStatus.OK, body, HTML, headers)

    def static(self, name: str) -> None:
        if name not in STATIC_FILES:
            return self.answer(HTTPStatus.NOT_FOUND, "no such file")
        media, _ = STATIC_FILES[name]
        self.answer(HTTPStatus.OK, (STATIC / name).read_bytes(), media)

    def answer(self, code, body, media="text/plain; charset=utf-8", headers=None) -> None:
        """Send ``body`` (text or bytes) as ``media`` with status ``code``."""
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", media)
        self.send_header("Content-Length", str(len(data)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
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
