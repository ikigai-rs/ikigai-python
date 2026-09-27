"""The tic-tac-toe app: its renderer, its routes, and parity with the Rust views.

The unit tests run anywhere. The parity tests need ``ttt-host`` (the ikigai tutorial's
``crates/ttt-host``) and skip without it: they run the host, drive one game through this
app over HTTP and a second game through the host's own Rust ``view:play``, and compare the
bytes — the app's board and status against the host's ``view:board`` / ``view:status`` for
the SAME game, and every reply against the Rust reply to the same move in the twin game.
If they differ, the Python filler is wrong, not the template.
"""

from __future__ import annotations

import hashlib
import http.client
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import ikigai
from examples import tictactoe_app as app
from examples.tictactoe_app import Html, fill, iri_of, said, view

# -- the vendored files -------------------------------------------------------------------

#: Pinned here as well as in the app, so changing a file means changing both on purpose.
DIGESTS = {
    # The book's src/vendor/htmx-2.0.4.min.js, which is ikigai-web's assets/htmx.min.js.
    "htmx-2.0.4.min.js": "e209dda5c8235479f3166defc7750e1dbcd5a5c1808b7792fc2e6733768fb447",
    # The book's css/ttt.css, the one stylesheet for the markup.
    "ttt.css": "f93bde4b6dacb82b085d88c8cf33c899eb3dd435dd64acd5e1e19c63be04f09b",
    # ttt-host's static/host.css: the color variables ttt.css reads.
    "host.css": "79437443cd22e56d183ebf5b4a6de625e72354d38e8a39e54894bf0dc19f27ac",
}


def test_the_vendored_files_are_the_books_byte_for_byte():
    assert {name: digest for name, (_, digest) in app.STATIC_FILES.items()} == DIGESTS
    for name, digest in DIGESTS.items():
        assert hashlib.sha256((app.STATIC / name).read_bytes()).hexdigest() == digest, name


# -- the renderer -------------------------------------------------------------------------


def test_fill_escapes_text_as_rust_and_the_book_do():
    # html.escape would write &#x27; — the Rust `escape` and the book's JS write &#39;.
    assert fill("<b>{{m}}</b>", lambda _: "a&b<c>\"d'e") == "<b>a&amp;b&lt;c&gt;&quot;d&#39;e</b>"


def test_fill_puts_html_in_raw_and_passes_slot_integers():
    seen = []

    def value(name, *args):
        seen.append((name, args))
        return Html("<i>ok</i>")

    assert fill("{{square 0 -2}}|{{status}}", value) == "<i>ok</i>|<i>ok</i>"
    assert seen == [("square", (0, -2)), ("status", ())]


def test_fill_leaves_what_is_not_a_slot():
    # Rust refuses such a template outright; the fixed templates have none, and this filler,
    # like the book's JS one, simply does not see them.
    assert fill("{{Nope}} {{x} {x}} {{x 1 }}", lambda *_: "!") == "{{Nope}} {{x} {x}} {{x 1 }}"


class FakeGame:
    """A game's resources as a dict — the choice rules without a host."""

    def __init__(self, cells: dict, winner: str = "-", turn: str = "X"):
        self.resources = {
            "template:board": "[{{square 0 0}}{{square 1 0}}]",
            "template:square-open": "open({{x}},{{y}})",
            "template:square-taken": "taken({{x}},{{y}},{{mark}})",
            "template:square-closed": "closed({{x}},{{y}})",
            "template:status-turn": "{{mark}} to play.",
            "template:status-won": "{{mark}} has won.",
            "template:status-draw": "A draw.",
            "template:reply": "{{message}}. {{status}}",
            "winner": winner,
            "turn": turn,
        }
        for (x, y), mark in cells.items():
            self.resources[f"cell:{x}:{y}"] = mark
        self.sunk = []

    def text(self, name):
        return self.resources[name]

    def sink(self, name):
        self.sunk.append(name)
        if name == "move:0:0":
            raise ikigai.InvalidArgumentError("x, y", "0,0 is taken — X played there")
        return "X plays 1,0"


def test_the_board_chooses_each_square():
    assert app.board(FakeGame({(0, 0): "X", (1, 0): "-"})) == "[taken(0,0,X)open(1,0)]"
    over = FakeGame({(0, 0): "X", (1, 0): "-"}, winner="X")
    assert app.board(over) == "[taken(0,0,X)closed(1,0)]"


@pytest.mark.parametrize(
    ("winner", "turn", "shown"),
    [("-", "O", "O to play."), ("X", "-", "X has won."), ("draw", "-", "A draw.")],
)
def test_the_status_chooses_its_template(winner, turn, shown):
    assert app.status(FakeGame({}, winner=winner, turn=turn)) == shown


def test_a_reply_says_what_the_write_said_or_its_refusal():
    game = FakeGame({}, turn="O")
    assert app.reply(game, "move:1:0") == "X plays 1,0. O to play."
    assert app.reply(game, "move:0:0") == (
        "invalid argument `x, y`: 0,0 is taken — X played there. O to play."
    )
    assert game.sunk == ["move:1:0", "move:0:0"]


@pytest.mark.parametrize(
    ("error", "shown"),
    [
        (ikigai.InvalidArgumentError("x", "bad"), "invalid argument `x`: bad"),
        (ikigai.MissingArgumentError("content"), "missing required argument `content`"),
        (ikigai.UnresolvedError("urn:a"), "no endpoint resolved for urn:a"),
        (ikigai.EndpointError("boom"), "endpoint error: boom"),
        (ikigai.DeniedError("no"), "denied: no"),
        (ikigai.NotFoundError("gone"), "not found: gone"),
        (ikigai.TimeoutError("slow"), "timeout: slow"),
        (ikigai.UnavailableError("down"), "unavailable: down"),
    ],
)
def test_a_refusal_is_said_as_the_rust_kernel_displays_it(error, shown):
    assert said(error) == shown


# -- the path <-> IRI rule and the routes -------------------------------------------------


@pytest.mark.parametrize(
    ("path", "iri"),
    [
        ("iki/tutorial/ttt/view/board", "urn:iki:tutorial:ttt:view:board"),
        ("iki/tutorial/ttt/view/play/1/-1", "urn:iki:tutorial:ttt:view:play:1:-1"),
        ("iki//ttt", None),
        ("iki/./ttt", None),
        ("iki/../ttt", None),
        ("iki/ttt/", None),
        ("http://evil/x", None),
    ],
)
def test_the_path_iri_rule(path, iri):
    assert iri_of(path) == iri


@pytest.mark.parametrize(
    ("verb", "path", "answer"),
    [
        ("GET", "", app.page),
        ("GET", "iki/tutorial/ttt/view/board", app.board),
        ("GET", "iki/tutorial/ttt/view/status", app.status),
        ("POST", "iki/tutorial/ttt/view/board", 405),
        ("POST", "", 405),
        ("GET", "iki/tutorial/ttt/view/play/1/1", 405),
        ("GET", "iki/tutorial/ttt/view/reset", 405),
        ("POST", "iki/tutorial/ttt/view/play/01/1", 404),  # one spelling per square
        ("POST", "iki/tutorial/ttt/view/play/1", 404),
        ("GET", "iki/tutorial/ttt/stored/1/1", 404),  # the app is not a proxy
        ("GET", "iki/tutorial/ttt/view/board/", 404),
        ("GET", "favicon.ico", 404),
    ],
)
def test_the_routes(verb, path, answer):
    assert view(verb, path) == answer


def test_the_writes_route_to_the_hosts_writes():
    game = FakeGame({})
    view("POST", "iki/tutorial/ttt/view/play/2/-1")(game)
    view("POST", "iki/tutorial/ttt/view/reset")(game)
    assert game.sunk == ["move:2:-1", "reset"]


@pytest.fixture
def serve_app():
    """A factory: the app on an ephemeral port over ``socket``; yields its base URL."""
    servers = []

    def start(socket: Path) -> str:
        server = app.make_server(socket, ("127.0.0.1", 0))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_address[1]}"

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def fetch(url: str, method: str = "GET") -> tuple[int, dict, bytes]:
    request = urllib.request.Request(url, method=method, data=b"" if method == "POST" else None)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def test_the_static_files_and_the_refusals_need_no_host(serve_app, socket_dir):
    base = serve_app(socket_dir / "nobody.sock")
    for name, (media, _) in app.STATIC_FILES.items():
        code, headers, body = fetch(f"{base}/static/{name}")
        assert (code, headers["Content-Type"]) == (200, media)
        assert body == (app.STATIC / name).read_bytes()
    assert fetch(f"{base}/static/nope.js")[0] == 404
    assert fetch(f"{base}/game/a/iki/tutorial/ttt/view/play/1/1")[0] == 405
    assert fetch(f"{base}/game/a/iki/tutorial/ttt/nope")[0] == 404
    connection = http.client.HTTPConnection(base.removeprefix("http://"), timeout=30)
    connection.request("GET", "/game/a")
    response = connection.getresponse()
    assert (response.status, response.getheader("Location")) == (301, "/game/a/")
    connection.close()


def test_no_host_is_a_503(serve_app, socket_dir):
    code, _, body = fetch(f"{serve_app(socket_dir / 'nobody.sock')}/game/a/")
    assert code == 503
    assert b"is ttt-host running?" in body


# -- parity with the Rust views, against a running ttt-host -------------------------------

TTT_HOST = shutil.which("ttt-host") or next(
    (str(p) for p in [Path.home() / ".local/ttt-host/bin/ttt-host"] if p.exists()), None
)
needs_host = pytest.mark.skipif(TTT_HOST is None, reason="no `ttt-host` binary")


def start_host(socket: Path, *games: str) -> subprocess.Popen:
    argv = [TTT_HOST, "--http", "127.0.0.1:0", "--socket", str(socket)]
    for game in games:
        argv += ["--game", game]
    process = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    deadline = time.monotonic() + 15
    while not socket.exists():
        if process.poll() is not None:
            pytest.fail(f"ttt-host exited: {process.stderr.read().decode()}")
        if time.monotonic() > deadline:
            process.kill()
            pytest.fail("ttt-host did not open its socket")
        time.sleep(0.05)
    try:
        ikigai.connect(socket).close()
    except ikigai.ProtocolError as e:
        process.kill()
        process.wait()
        pytest.skip(f"ttt-host speaks another wire version: {e}")
    return process


@pytest.fixture
def host(socket_dir):
    """``ttt-host`` with two in-memory games: ``py`` (played through the app) and ``rs``
    (played through the host's Rust views), plus the root game."""
    socket = socket_dir / "h.sock"
    process = start_host(socket, "py", "rs")
    yield socket
    process.terminate()
    process.wait(timeout=10)


class Twins:
    """Game ``py`` played through the app, game ``rs`` through the Rust ``view:play``."""

    def __init__(self, socket: Path, base: str):
        self.kernel = ikigai.connect(socket)
        self.base = base
        self.checked = 0

    def rust(self, game: str, name: str, verb: str = "source") -> str:
        prefix = "urn:iki:tutorial:ttt:" if game == "" else f"urn:game:{game}:iki:tutorial:ttt:"
        return getattr(self.kernel, verb)(prefix + name).text

    def python(self, game: str, path: str, method: str = "GET") -> str:
        where = "" if game == "" else f"/game/{game}"
        code, headers, body = fetch(f"{self.base}{where}/iki/tutorial/ttt/{path}", method)
        assert code == 200, body
        assert headers["Content-Type"] == "text/html; charset=utf-8"
        return body.decode("utf-8")

    def same_views(self, game: str = "py") -> None:
        """The app's board and status for ``game``, byte for byte the host's Rust views."""
        for name in ("board", "status"):
            assert self.python(game, f"view/{name}") == self.rust(game, f"view:{name}"), name
            self.checked += 1

    def play(self, x: int, y: int) -> str:
        """Play ``(x, y)`` in both games; the replies must match, and so must the boards."""
        ours = self.python("py", f"view/play/{x}/{y}", "POST")
        theirs = self.rust("rs", f"view:play:{x}:{y}", "sink")
        assert ours == theirs
        self.same_views("py")
        assert self.rust("py", "view:board") == self.rust("rs", "view:board")
        return ours

    def reset(self) -> str:
        ours = self.python("py", "view/reset", "POST")
        assert ours == self.rust("rs", "view:reset", "sink")
        self.same_views("py")
        return ours


@pytest.fixture
def twins(host, serve_app):
    twins = Twins(host, serve_app(host))
    yield twins
    twins.kernel.close()


@needs_host
def test_parity_through_a_won_game_a_refusal_and_a_draw(twins):
    twins.same_views("py")  # empty
    assert twins.play(1, 1) == "X plays 1,1. O to play."
    assert twins.play(1, 1) == "invalid argument `x, y`: 1,1 is taken — X played there. O to play."
    assert twins.play(3, 1) == (
        "invalid argument `x, y`: 3,1 is off the board — no line passes through it. O to play."
    )
    for x, y in [(0, 0), (2, 0), (1, 0), (0, 2)]:  # X takes the 2,0-1,1-0,2 diagonal
        twins.play(x, y)
    assert twins.python("py", "view/status") == "X has won."
    assert 'aria-label="1,2: empty, the game is over"' in twins.python("py", "view/board")
    assert twins.play(1, 2) == ("invalid argument `x, y`: the game is over — X has won. X has won.")
    assert twins.reset() == "The board is clear. X to play."
    for x, y in [(0, 0), (1, 0), (2, 0), (1, 1), (0, 1), (2, 1), (1, 2), (0, 2), (2, 2)]:
        twins.play(x, y)
    assert twins.python("py", "view/status") == "A draw."
    assert twins.play(0, 0) == "invalid argument `x, y`: the game is over — a draw. A draw."
    assert twins.checked == 2 * (1 + 8 + 1 + 10)


@needs_host
def test_parity_in_the_root_game(twins):
    twins.same_views("")
    assert twins.python("", "view/play/2/0", "POST") == "X plays 2,0. O to play."
    twins.same_views("")
    assert twins.rust("py", "view:status") == "X to play."  # the root's move stayed there


@needs_host
def test_the_page_is_the_games_template_under_its_base(twins):
    code, headers, body = fetch(f"{twins.base}/game/py/")
    assert code == 200
    assert "base-uri 'self'" in headers["Content-Security-Policy"]
    page = body.decode("utf-8")
    assert '<base href="/game/py/">' in page
    assert fill(twins.rust("py", "template:game"), lambda _: "py") in page
    for name in app.STATIC_FILES:
        assert f'"/static/{name}"' in page
    root = fetch(f"{twins.base}/")[2].decode("utf-8")
    assert '<base href="/">' in root and 'aria-label="Game root"' in root


@needs_host
def test_a_game_the_host_does_not_have_is_a_404(twins):
    assert fetch(f"{twins.base}/game/nope/")[0] == 404
    assert fetch(f"{twins.base}/game/nope/iki/tutorial/ttt/view/board")[0] == 404


@needs_host
def test_all_in_python_except_the_middle(socket_dir, serve_app):
    """The README's demo: the Python store under the Rust host under the Python app."""
    from examples.tictactoe_store import CellStore, stored_cell
    from ikigai.serve import Server

    store = CellStore()
    store_socket = socket_dir / "s.sock"
    server = Server([stored_cell(store)], store_socket)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    process = start_host(socket_dir / "h.sock", f"py={store_socket}")
    try:
        base = serve_app(socket_dir / "h.sock")
        for x, y in [(1, 1), (0, 0), (2, 2)]:
            fetch(f"{base}/game/py/iki/tutorial/ttt/view/play/{x}/{y}", "POST")
        assert store.marks == {(1, 1): "X", (0, 0): "O", (2, 2): "X"}
        board = fetch(f"{base}/game/py/iki/tutorial/ttt/view/board")[2].decode()
        assert 'aria-label="O at 0,0"' in board
        reads = store.reads
        fetch(f"{base}/game/py/iki/tutorial/ttt/view/board")
        assert store.reads == reads  # the host's cache answered; the store was not asked
    finally:
        process.terminate()
        process.wait(timeout=10)
        server.shutdown()
