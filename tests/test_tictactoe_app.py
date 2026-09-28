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
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import ikigai
from examples import tictactoe_app as app
from examples.tictactoe_app import Html, Refusal, Target, decoded, fill, respond, said, target

# -- the vendored files -------------------------------------------------------------------

#: Pinned here as well as in the app, so changing a file means changing both on purpose. All
#: three as ikigai-tutorial commit 4d9440a has them (`git show 4d9440a:<path>`), the build of
#: ttt-host the parity tests run against — never a working tree, which may be mid-edit.
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


#: The template format's cases, copied from ikigai-tutorial's ``crates/tic-tac-toe/README.md``
#: (the ``template-cases`` block, as of commit 4d9440a), which the Rust filler is tested
#: against. ``refuse`` is a template the filler must refuse; ``slots`` is a template, a tab,
#: and what it reads as (``name args…`` per slot, joined by `` | ``, ``-`` for none);
#: ``escape`` is a text, a tab, and its escaped form. A line with no kind continues the case
#: before it.
TEMPLATE_CASES = """\
slots   a {{square 0 -2}} b {{mark}}\tsquare 0 -2 | mark
slots   {{x}}}\tx
slots   }} {x} { {x}\t-
slots   {{a-b 12 -345}}\ta-b 12 -345
refuse  {{x 01}}
refuse  {{x -0}}
refuse  {{x +1}}
refuse  {{x 1 }}
refuse  {{x  1}}
refuse  {{ x}}
refuse  {{Mark}}
refuse  {{-x}}
refuse  {{}}
refuse  {{x}
refuse  {{x}} {{
refuse  {{{x}}}
refuse  {{x 99999999999999999999}}
refuse  {{x
y}}
escape  a&b<c>"d'e\ta&amp;b&lt;c&gt;&quot;d&#39;e
escape  it's\tit&#39;s
escape  ✓ 1,1\t✓ 1,1
"""


def template_cases() -> list[tuple[str, str]]:
    cases: list[tuple[str, str]] = []
    for line in TEMPLATE_CASES.splitlines():
        kind = next((k for k in ("slots", "refuse", "escape") if line.startswith(k)), None)
        if kind is None:
            cases[-1] = (cases[-1][0], cases[-1][1] + "\n" + line)
        else:
            cases.append((kind, line[len(kind) :].lstrip()))
    return cases


def test_there_are_template_cases():
    assert len(template_cases()) >= 20


@pytest.mark.parametrize(("kind", "text"), template_cases())
def test_the_template_format_cases_hold(kind, text):
    if kind == "refuse":
        with pytest.raises(ikigai.EndpointError, match="^a template: "):
            app.slots(text)
    elif kind == "slots":
        template, expected = text.split("\t")
        shown = [" ".join([name, *map(str, args)]) for name, args in app.slots(template)]
        assert (" | ".join(shown) or "-") == expected
    else:
        raw, escaped = text.split("\t")
        assert fill("{{m}}", lambda _: raw) == escaped


def test_a_refused_template_is_refused_by_fill_too():
    with pytest.raises(ikigai.EndpointError, match=r"`\{\{Mark\}\}` is not a slot"):
        fill("<b>{{Mark}}</b>", lambda _: "X")
    with pytest.raises(ikigai.EndpointError, match="is never closed"):
        fill("{{x}} {{", lambda _: "X")


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
        (ikigai.ConflictError("taken"), "conflict: taken"),
    ],
)
def test_a_refusal_is_said_as_the_rust_kernel_displays_it(error, shown):
    assert said(error) == shown


# -- the edge: ttt-host's HTTP rules -----------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "path"),
    [
        ("/game/a/", "/game/a/"),
        ("/a%2Fb", "/a/b"),  # decoded before it is split, so %2F separates
        ("/%30%31", "/01"),
        ("/+1", "/ 1"),  # `+` is a space in the path too, as ikigai-web decodes it
        ("/%2B1", "/+1"),
        ("/%+1", "/\x01"),  # Rust's from_str_radix takes a leading `+`
        ("/%zz/%/%4", "/%zz/%/%4"),  # not an escape: kept
        ("/%E2%82%AC", "/\u20ac"),
        ("/%FF", "/\ufffd"),
    ],
)
def test_a_path_is_decoded_as_ikigai_web_decodes_it(raw, path):
    assert decoded(raw) == path


@pytest.mark.parametrize(
    ("path", "view"),
    [
        ("/", Target("page", None, "urn:ttt-host:page:root")),
        ("//", Target("page", None, "urn:ttt-host:page:root")),
        ("/game/a", Target("page", "a", "urn:ttt-host:page:game:a")),
        ("/game/a/", Target("page", "a", "urn:ttt-host:page:game:a")),
        ("/game/a_b", Target("page", "a_b", "urn:ttt-host:page:game:a_b")),  # the host says
        ("/static/ttt.css/", Target("static", None, "urn:ttt-host:static:ttt.css", "ttt.css")),
        ("/iki/tutorial/ttt/view/board", Target("board", None, "urn:iki:tutorial:ttt:view:board")),
        (
            "/game/a/iki/tutorial/ttt/view/status/",
            Target("status", "a", "urn:game:a:iki:tutorial:ttt:view:status"),
        ),
        (
            "/game/a//iki:tutorial/ttt/view/reset",  # empty segments dropped; `:` separates
            Target("reset", "a", "urn:game:a:iki:tutorial:ttt:view:reset"),
        ),
        (
            "/game/a/iki/tutorial/ttt/view/play/01/1/2",  # x to the next `:`, y the rest
            Target("play", "a", "urn:game:a:iki:tutorial:ttt:view:play:01:1:2", ("01", "1:2")),
        ),
        ("/game/a/iki/tutorial/ttt/view/play/1", None),
        ("/game/a/iki/tutorial/ttt/view/play/%3A1/1", None),  # an empty x is no play
        ("/game/a/iki/tutorial/ttt/stored/1/1", None),  # the app is not a proxy
        ("/game/a/iki/tutorial/ttt/cell/1/1", None),
        ("/game/a/x/iki/tutorial/ttt/view/board", None),
        ("/game/a:x/iki/tutorial/ttt/view/board", None),  # game a, then `x:iki:…`
        ("/game", None),
        ("/static/nope.js", None),
        ("/favicon.ico", None),
    ],
)
def test_the_views_a_path_names(path, view):
    assert target(path) == view


@pytest.mark.parametrize(
    ("iri", "valid"),
    [
        ("urn:iki:tutorial:ttt:view:play:1:'", True),
        ("urn:iki:tutorial:ttt:view:play:1:\u00a0", True),
        ("urn:iki:tutorial:ttt:view:play:1:?", True),
        ("urn:iki:tutorial:ttt:view:play:1:#?", True),
        ("urn:iki:tutorial:ttt:view:play:1:?a\ue000", True),  # private use, in the query
        ("urn:iki:tutorial:ttt:view:play:1:%41", True),
        ("urn:iki:tutorial:ttt:view:play:1:\ue000", False),  # ... but not in the path
        ("urn:iki:tutorial:ttt:view:play:1:##", False),
        ("urn:iki:tutorial:ttt:view:play:1: 1", False),
        ("urn:iki:tutorial:ttt:view:play:1:<", False),
        ("urn:iki:tutorial:ttt:view:play:1:[", False),
        ("urn:iki:tutorial:ttt:view:play:1:%", False),
        ("urn:iki:tutorial:ttt:view:play:1:\ufffe", False),
        ("urn:iki:tutorial:ttt:view:play:1:\ufffd", False),
    ],
)
def test_a_name_the_host_can_parse(iri, valid):
    assert bool(app.URN.fullmatch(iri)) is valid


BOARD = Target("board", "a", "urn:game:a:iki:tutorial:ttt:view:board")
PLAY = Target("play", "a", "urn:game:a:iki:tutorial:ttt:view:play:1:1", ("1", "1"))
READS, WRITES = "GET, HEAD, OPTIONS", "POST, PUT, PATCH, OPTIONS"


def unasked():
    raise AssertionError("the answer does not depend on the game")


@pytest.mark.parametrize(
    ("verb", "view", "known", "answer"),
    [
        ("GET", BOARD, None, app.board),
        ("HEAD", BOARD, None, app.board),
        ("GET", Target("page", None, "urn:ttt-host:page:root"), None, app.page),
        ("POST", BOARD, True, Refusal(405, "method not allowed", READS)),
        ("POST", BOARD, False, Refusal(404, f"no endpoint resolved for {BOARD.iri}")),
        ("DELETE", BOARD, False, Refusal(404, f"no endpoint resolved for {BOARD.iri}")),
        ("PATCH", BOARD, True, Refusal(405, "method not allowed", READS)),
        ("PATCH", BOARD, False, Refusal(415, "no patch strategy for this Content-Type")),
        ("PATCH", PLAY, None, Refusal(415, "no patch strategy for this Content-Type")),
        ("GET", PLAY, True, Refusal(405, "method not allowed", WRITES)),
        ("HEAD", PLAY, True, Refusal(405, "method not allowed", WRITES)),
        ("GET", PLAY, False, Refusal(404, f"no endpoint resolved for {PLAY.iri}")),
        ("OPTIONS", PLAY, True, Refusal(204, "", WRITES)),
        ("OPTIONS", PLAY, False, Refusal(204, "", READS)),
        ("FOO", PLAY, True, Refusal(405, "method not allowed", WRITES)),
    ],
)
def test_the_refusals_come_in_ikigai_webs_order(verb, view, known, answer):
    assert respond(verb, view, unasked if known is None else lambda: known) == answer


def test_the_writes_route_to_the_hosts_writes():
    game = FakeGame({})
    game.resources["cell:2:-1"] = "-"
    respond("POST", target("/iki/tutorial/ttt/view/play/2/-1"), unasked)(game)
    respond("PUT", target("/iki/tutorial/ttt/view/reset"), unasked)(game)
    assert game.sunk == ["move:2:-1", "reset"]


def test_a_play_reads_its_cell_first_so_the_host_refuses_a_spelling():
    class Refusing(FakeGame):
        def text(self, name):
            if name == "cell:01:1":
                raise ikigai.InvalidArgumentError("x", "`01` is not an integer in its plain form")
            return super().text(name)

    game = Refusing({})
    with pytest.raises(ikigai.InvalidArgumentError):
        respond("POST", target("/iki/tutorial/ttt/view/play/01/1"), unasked)(game)
    assert game.sunk == []


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


def test_the_files_the_root_games_refusals_and_the_non_views_need_no_host(serve_app, socket_dir):
    base = serve_app(socket_dir / "nobody.sock")
    for name, (media, _) in app.STATIC_FILES.items():
        code, headers, body = fetch(f"{base}/static/{name}")
        assert (code, headers["Content-Type"]) == (200, media)
        assert body == (app.STATIC / name).read_bytes()
    assert fetch(f"{base}/static/ttt.css", "DELETE")[0] == 405
    assert fetch(f"{base}/iki/tutorial/ttt/view/play/1/1")[0] == 405
    assert fetch(f"{base}/", method="POST")[0] == 405
    for path in ["/static/nope.js", "/game/a/iki/tutorial/ttt/nope", "/favicon.ico"]:
        code, headers, body = fetch(base + path)
        assert (code, body) == (404, b"not found"), path
    assert fetch(f"{base}/game/a/iki/tutorial/ttt/view/play/1/a%20b", "POST")[:3:2] == (
        400,
        b"not a resource path",
    )


def test_no_host_is_a_503(serve_app, socket_dir):
    code, _, body = fetch(f"{serve_app(socket_dir / 'nobody.sock')}/game/a/")
    assert code == 503
    assert b"is ttt-host running?" in body


# -- parity with the Rust views, against a running ttt-host -------------------------------

TTT_HOST = shutil.which("ttt-host") or next(
    (str(p) for p in [Path.home() / ".local/ttt-host/bin/ttt-host"] if p.exists()), None
)
needs_host = pytest.mark.skipif(TTT_HOST is None, reason="no `ttt-host` binary")


def free_port() -> int:
    """A port nothing is listening on, for the host's HTTP face (ttt-host cannot say which
    port it bound when given 0)."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def start_host(socket: Path, *games: str, http: str = "127.0.0.1:0") -> subprocess.Popen:
    argv = [TTT_HOST, "--http", http, "--socket", str(socket)]
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
    (played through the host's Rust views), plus the root game. Yields its socket and the
    URL of its own HTTP face."""
    socket = socket_dir / "h.sock"
    http = f"127.0.0.1:{free_port()}"
    process = start_host(socket, "py", "rs", http=http)
    yield socket, f"http://{http}"
    process.terminate()
    process.wait(timeout=10)


class Twins:
    """Game ``py`` played through the app, game ``rs`` through the Rust ``view:play``."""

    def __init__(self, socket: Path, base: str, host_http: str):
        self.kernel = ikigai.connect(socket)
        self.base = base
        self.host_http = host_http
        self.checked = 0

    def rust(self, game: str, name: str, verb: str = "source") -> str:
        prefix = "urn:iki:tutorial:ttt:" if game == "" else f"urn:game:{game}:iki:tutorial:ttt:"
        return getattr(self.kernel, verb)(prefix + name).text

    def python(self, game: str, path: str, method: str = "GET") -> str:
        where = "" if game == "" else f"/game/{game}"
        code, headers, body = fetch(f"{self.base}{where}/iki/tutorial/ttt/{path}", method)
        assert code == 200, body
        assert headers["Content-Type"] == "text/html;charset=utf-8"
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
    socket, host_http = host
    twins = Twins(socket, serve_app(socket), host_http)
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
@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/game/py/",
        "/game/rs",
        "/game/root/",
        "/static/htmx-2.0.4.min.js",
        "/static/ttt.css",
        "/static/host.css",
    ],
)
def test_the_page_and_its_files_are_ttt_hosts_byte_for_byte(twins, path):
    ours, theirs = fetch(twins.base + path), fetch(twins.host_http + path)
    assert (ours[0], ours[2]) == (theirs[0], theirs[2]) == (200, theirs[2])
    assert ours[1]["Content-Type"] == theirs[1]["Content-Type"]
    if not path.startswith("/static/"):
        assert "base-uri 'self'" in ours[1]["Content-Security-Policy"]


@needs_host
def test_the_page_is_the_games_template_under_its_base(twins):
    page = fetch(f"{twins.base}/game/py/")[2].decode("utf-8")
    assert '<base href="/game/py/">' in page
    assert fill(twins.rust("py", "template:game"), lambda _: "py") in page
    root = fetch(f"{twins.base}/")[2].decode("utf-8")
    assert '<base href="/">' in root and 'aria-label="Game root"' in root


@needs_host
def test_a_game_the_host_does_not_have_is_a_404(twins):
    assert fetch(f"{twins.base}/game/nope/")[0] == 404
    assert fetch(f"{twins.base}/game/nope/iki/tutorial/ttt/view/board")[0] == 404


VIEWS = "iki/tutorial/ttt/view"

#: The edges the parity test asks the app and the host, measured on ``ttt-host`` (tutorial
#: 4d9440a): ikigai-deno's rows (its ``tests/tictactoe_app_test.ts``, PR #13) with game ``py``
#: for ``a`` and ``zz`` the game the host lacks, then this face's own. Game ``py`` is over
#: when they are asked, so every play is refused and nothing moves: the app and the host can
#: answer the SAME game, and each row's status, body, Content-Type and Allow must agree.
EDGES = [
    # The page: a trailing slash or not, an unknown game, a name that is not an IRI.
    ("GET", "/game/py"),
    ("GET", "/game/py//"),
    ("GET", "/game/zz"),
    ("GET", "/game/zz/"),
    ("GET", "/game/a_b/"),
    ("GET", "/game/p%79/"),  # decoded before routing: game py
    ("GET", "/game/a%20b/"),
    ("GET", "/game/a%4"),
    ("GET", "/game/a:b/"),
    ("HEAD", "/game/py/"),
    ("HEAD", "/game/zz/"),
    ("POST", "/"),
    ("POST", "/game/py/"),
    ("POST", "/game/zz/"),
    ("PUT", "/game/py/"),
    ("DELETE", "/game/py"),
    ("PATCH", "/game/py/"),
    ("PATCH", "/game/zz/"),
    ("OPTIONS", "/"),
    ("OPTIONS", "/game/py/"),
    ("OPTIONS", "/game/zz/"),
    ("FOO", "/game/py/"),
    ("FOO", "/game/zz/"),
    # A play: its coordinates in every wrong spelling, and every method.
    ("POST", f"/game/py/{VIEWS}/play/01/0"),
    ("POST", f"/game/py/{VIEWS}/play/-0/0"),
    ("POST", f"/game/py/{VIEWS}/play/+1/0"),  # `+` is a space: not an IRI
    ("POST", f"/game/py/{VIEWS}/play/%2B1/0"),
    ("POST", f"/game/py/{VIEWS}/play/x/0"),
    ("POST", f"/game/py/{VIEWS}/play/1.0/0"),
    ("POST", f"/game/py/{VIEWS}/play/0/01"),
    ("POST", f"/game/py/{VIEWS}/play/99999999999999999999/0"),
    ("POST", f"/game/py/{VIEWS}/play/9223372036854775808/0"),
    ("POST", f"/game/py/{VIEWS}/play/-9223372036854775808/0"),
    ("POST", f"/game/py/{VIEWS}/play/1/2/3"),  # y is the rest: `2:3`
    ("POST", f"/game/py/{VIEWS}/play/1/a%20b"),
    ("POST", f"/game/py/{VIEWS}/play/1/%3C"),
    ("POST", f"/game/py/{VIEWS}/play/1/%5B"),
    ("POST", f"/game/py/{VIEWS}/play/1/%"),
    ("POST", f"/game/py/{VIEWS}/play/1/%zz"),
    ("POST", f"/game/py/{VIEWS}/play/1/%+1"),
    ("POST", f"/game/py/{VIEWS}/play/1/%3F"),
    ("POST", f"/game/py/{VIEWS}/play/1/%23"),
    ("POST", f"/game/py/{VIEWS}/play/1/%23%23"),
    ("POST", f"/game/py/{VIEWS}/play/1/%EE%80%80"),
    ("POST", f"/game/py/{VIEWS}/play/1/%3Fa%EE%80%80"),
    ("POST", f"/game/py/{VIEWS}/play/1/%EF%BF%BE"),
    ("POST", f"/game/py/{VIEWS}/play/1/%FF"),
    ("POST", f"/game/py/{VIEWS}/play/%E2%82%AC/0"),
    ("POST", f"/game/py/{VIEWS}/play/1/'"),
    ("POST", f"/game/py/{VIEWS}/play/3/1"),
    ("POST", f"/game/py/{VIEWS}/play/%31/%31"),
    ("POST", f"/game/py/{VIEWS}/play/1%2F1"),
    ("POST", "/game/py/iki:tutorial/ttt/view/play/1/1"),
    ("PUT", f"/game/py/{VIEWS}/play/1/1"),
    ("POST", f"/game/py/{VIEWS}/play/1/1/"),
    ("GET", f"/game/py/{VIEWS}/play/1/1"),
    ("HEAD", f"/game/py/{VIEWS}/play/1/1"),
    ("DELETE", f"/game/py/{VIEWS}/play/1/1"),
    ("PATCH", f"/game/py/{VIEWS}/play/1/1"),
    ("OPTIONS", f"/game/py/{VIEWS}/play/1/1"),
    ("GET", f"/game/py/{VIEWS}/reset"),
    ("DELETE", f"/game/py/{VIEWS}/reset"),
    ("PATCH", f"/game/py/{VIEWS}/reset"),
    ("POST", f"/{VIEWS}/play/01/0"),
    # The reads.
    ("POST", f"/game/py/{VIEWS}/board"),
    ("PUT", f"/game/py/{VIEWS}/board"),
    ("DELETE", f"/game/py/{VIEWS}/status"),
    ("PATCH", f"/game/py/{VIEWS}/status"),
    ("HEAD", f"/game/py/{VIEWS}/board"),
    ("OPTIONS", f"/game/py/{VIEWS}/board"),
    ("FOO", f"/game/py/{VIEWS}/board"),
    ("GET", f"/game/py/{VIEWS}/board/"),
    ("GET", "/game/py/iki/tutorial/ttt//view//status"),
    ("GET", f"/{VIEWS}/status"),
    ("GET", f"/{VIEWS}/board?x=1"),
    # An unknown game: nothing is declared there, so no 405 and no coordinate check.
    ("GET", f"/game/zz/{VIEWS}/board"),
    ("GET", f"/game/zz/{VIEWS}/play/1/1"),
    ("POST", f"/game/zz/{VIEWS}/play/1/1"),
    ("POST", f"/game/zz/{VIEWS}/play/01/0"),
    ("POST", f"/game/zz/{VIEWS}/reset"),
    ("DELETE", f"/game/zz/{VIEWS}/board"),
    ("PATCH", f"/game/zz/{VIEWS}/reset"),
    ("OPTIONS", f"/game/zz/{VIEWS}/board"),
    ("FOO", f"/game/zz/{VIEWS}/board"),
    ("GET", f"/game/a_b/{VIEWS}/board"),
    # The root game's gateway name, which the host's catalog does not list.
    ("GET", "/game/root/"),
    ("POST", "/game/root/"),
    ("GET", f"/game/root/{VIEWS}/status"),
    ("POST", f"/game/root/{VIEWS}/board"),
    ("GET", f"/game/root/{VIEWS}/play/1/1"),
    ("POST", f"/game/root/{VIEWS}/play/01/1"),
    # The files the page loads.
    ("GET", "/static//ttt.css"),
    ("GET", "/static/ttt.css/"),
    ("HEAD", "/static/host.css"),
    ("POST", "/static/ttt.css"),
    ("OPTIONS", "/static/host.css"),
]

#: Paths the host serves that are not views: the app answers ``404 not found``, whatever the
#: method — it is not a proxy for the host's other names.
NOT_VIEWS = [
    ("GET", "/game/py/iki/tutorial/ttt/board"),
    ("GET", "/game/py/iki/tutorial/ttt/cell/1/1"),
    ("GET", "/game/py/iki/tutorial/ttt/winner"),
    ("GET", "/game/py/iki/tutorial/ttt/template/board"),
    ("GET", "/iki/tutorial/ttt/turn"),
    ("GET", "/iki/tutorial/ttt/stored/1/1"),
    ("GET", "/game/py/iki/tutorial/ttt/stored/1/1"),
    ("GET", "/game/py/iki/tutorial/ttt/view/nothing"),
    ("POST", "/game/py/iki/tutorial/ttt/view/play/1"),
    ("POST", "/game/py/iki/tutorial/ttt/view/play/%3A1/1"),
    ("GET", "/static/nothing.css"),
    ("GET", "/favicon.ico"),
    ("GET", "/game"),
    ("FOO", "/nothing"),
    ("OPTIONS", "/nothing"),
    ("GET", "/a%20b"),
]


def call(base: str, method: str, path: str) -> tuple[int, bytes, str | None, str | None]:
    """``method`` on ``path`` as it is spelled, which ``urllib`` would not send: its status,
    body, Content-Type and Allow."""
    host, port = base.removeprefix("http://").split(":")
    connection = http.client.HTTPConnection(host, int(port), timeout=30)
    try:
        connection.putrequest(method, path, skip_accept_encoding=True)
        connection.endheaders()
        response = connection.getresponse()
        body = response.read()
        return (
            response.status,
            body,
            response.getheader("Content-Type"),
            response.getheader("Allow"),
        )
    finally:
        connection.close()


@needs_host
def test_the_edges_are_ttt_hosts(twins):
    for x, y in [(1, 1), (0, 0), (2, 0), (1, 0), (0, 2)]:  # X takes a diagonal: the game is over
        twins.play(x, y)
    answered = {}
    for method, path in EDGES:
        ours, theirs = call(twins.base, method, path), call(twins.host_http, method, path)
        assert ours == theirs, f"{method} {path}"
        answered[ours[0]] = answered.get(ours[0], 0) + 1
    for method, path in NOT_VIEWS:
        assert call(twins.base, method, path)[:2] == (404, b"not found"), f"{method} {path}"
    assert twins.rust("py", "view:status") == "X has won."  # no edge moved anything
    print(f"edges: {len(EDGES)} rows the same as ttt-host's, by status {sorted(answered.items())}")


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
