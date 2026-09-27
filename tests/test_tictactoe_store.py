"""The tic-tac-toe store: the Rust ``stored_cell``'s contract, from Python.

Every message asserted here is the Rust original's, word for word
(ikigai-tutorial ``crates/tic-tac-toe/src/lib.rs``) — the book prints them.
"""

import threading

import pytest

import ikigai
from examples.tictactoe_store import STORED, CellStore, stored_cell, stored_name
from ikigai.serve import Server

XSD_INTEGER = "http://www.w3.org/2001/XMLSchema#integer"
XSD_STRING = "http://www.w3.org/2001/XMLSchema#string"


@pytest.fixture
def store():
    return CellStore()


@pytest.fixture
def kernel(socket_dir, store):
    path = socket_dir / "ttt.sock"
    server = Server([stored_cell(store)], path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    with ikigai.connect(path) as k:
        yield k
    server.shutdown()
    thread.join(timeout=5)


def test_names():
    assert STORED == "urn:iki:tutorial:ttt:stored:{x}:{y}"
    assert stored_name(0, -1) == "urn:iki:tutorial:ttt:stored:0:-1"


def test_an_unplayed_square_is_not_found(kernel):
    with pytest.raises(ikigai.NotFoundError) as e:
        kernel.source(stored_name(1, 1))
    assert str(e.value).endswith("nothing has been played at 1,1")
    assert type(e.value) is ikigai.NotFoundError  # the variant the platonic cell keys on


def test_play_read_clear(kernel, store):
    assert kernel.sink(stored_name(1, 1), "X").text == "ok"
    read = kernel.source(stored_name(1, 1))
    assert read.text == "X"
    assert read.media_type == "text/plain;charset=utf-8"
    assert read.expiry.kind == "never"  # cacheable: the host caches it
    assert kernel.delete(stored_name(1, 1)).text == "ok"
    assert kernel.delete(stored_name(1, 1)).text == "ok"  # idempotent
    with pytest.raises(ikigai.NotFoundError):
        kernel.source(stored_name(1, 1))
    assert store.reads == 2


def test_any_mark_anywhere_trimmed(kernel, store):
    kernel.sink(stored_name(-3, 40), "  R\n")  # Connect-Four, off any 3x3 board
    assert kernel.source(stored_name(-3, 40)).text == "R"
    assert store.marks == {(-3, 40): "R"}


@pytest.mark.parametrize("empty", ["", "   ", "\n"])
def test_an_empty_mark_is_refused(kernel, empty):
    with pytest.raises(ikigai.InvalidArgumentError) as e:
        kernel.sink(stored_name(0, 0), empty)
    assert e.value.name == "content"
    assert e.value.detail == "an empty mark — to clear a cell, delete it"


def test_a_sink_without_content_is_missing_it(kernel):
    with pytest.raises(ikigai.MissingArgumentError) as e:
        kernel.sink(stored_name(0, 0))
    assert e.value.name == "content"


@pytest.mark.parametrize(
    ("x", "y", "blamed", "text"),
    [
        ("01", "0", "x", "01"),
        ("+1", "0", "x", "+1"),
        ("-0", "0", "x", "-0"),
        ("0", "007", "y", "007"),
        ("a", "0", "x", "a"),
        ("9223372036854775808", "0", "x", "9223372036854775808"),  # past i64, as Rust
    ],
)
def test_one_spelling_per_square(kernel, x, y, blamed, text):
    for verb in (kernel.source, kernel.delete):
        with pytest.raises(ikigai.InvalidArgumentError) as e:
            verb(f"urn:iki:tutorial:ttt:stored:{x}:{y}")
        assert e.value.name == blamed
        assert e.value.detail == f"`{text}` is not an integer in its plain form (e.g. 0, 2, -1)"


def test_the_square_is_judged_before_the_mark(kernel):
    # Rust reads the coordinates first, then the verb's own inputs.
    with pytest.raises(ikigai.InvalidArgumentError) as e:
        kernel.sink("urn:iki:tutorial:ttt:stored:01:0", "")
    assert e.value.name == "x"


def test_the_i64_edges_are_squares(kernel):
    low, high = -(2**63), 2**63 - 1
    kernel.sink(stored_name(low, high), "X")
    assert kernel.source(stored_name(low, high)).text == "X"


def test_exists_is_source_would_succeed(kernel, store):
    assert kernel.exists(stored_name(2, 2)).text == "false"
    kernel.sink(stored_name(2, 2), "O")
    assert kernel.exists(stored_name(2, 2)).text == "true"
    assert store.reads == 2  # the default Exists runs Source, as Rust's `_` arm does


def test_a_verb_it_does_not_answer_is_refused(kernel):
    # There is no fifth verb to try on the wire, so check the refusal's wording
    # through a Space directly: Exists and Meta are always answered.
    from ikigai.serve import Space
    from ikigai.wire import Issue, Request, Verb

    f = ikigai.family("urn:py:only-source:{x}", id="only")

    @f.source
    def read(x: int) -> str:
        return ""

    reply = Space([f]).dispatch(Issue(Request(Verb.DELETE, "urn:py:only-source:1", {})))
    assert str(reply.error).endswith(
        "verb Delete is not supported by `only` (it answers Source, Exists, Meta)"
    )


def test_one_action_per_verb_each_naming_the_coordinates(kernel):
    # The Rust original's own test:
    # the_stored_cell_declares_one_action_per_verb_each_naming_the_coordinates
    description = kernel.describe(STORED)
    assert description["id"] == "ttt-stored"
    assert description["title"] == "Stored cell"
    actions = description["actions"]
    assert [a["verb"] for a in actions] == ["Source", "Sink", "Delete"]
    for action in actions:
        bound = [i for i in action["inputs"] if i["source"] == "binding"]
        assert [i["name"] for i in bound] == ["x", "y"], action["verb"]
        assert all(i["class"] == XSD_INTEGER for i in bound)
        assert [i["summary"] for i in bound] == [
            "the column — any integer",
            "the row — any integer",
        ]
    sink = next(a for a in actions if a["verb"] == "Sink")
    content = [i for i in sink["inputs"] if i["name"] == "content"]
    assert content == [
        {
            "name": "content",
            "summary": "the mark to play, e.g. X or O",
            "required": True,
            "source": "argument",
            "class": XSD_STRING,
        }
    ]
    summaries = {a["verb"]: a["summary"] for a in actions}
    assert summaries == {
        "Source": "the mark played at (x, y); NotFound if none has been",
        "Sink": "play a mark at (x, y)",
        "Delete": "clear the cell at (x, y)",
    }


def test_the_catalog_names_the_family(kernel):
    assert [(e.pattern, e.endpoint) for e in kernel.entries()] == [(STORED, "ttt-stored")]
