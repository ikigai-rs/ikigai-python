"""The tic-tac-toe game's stateful atom — the stored cell — served from Python.

The Rust original is ``stored_cell`` in the ikigai tutorial's
``crates/tic-tac-toe/src/lib.rs``; this is the same contract, message for
message, so a Rust host can mount it and keep everything ABOVE the atom (the
platonic cell, the lines, the board, the rules) in its own kernel::

    python -m examples.tictactoe_store /tmp/ttt.sock
    ikigai --override urn:iki:tutorial:ttt:stored:=/tmp/ttt.sock \\
        -c 'sink urn:iki:tutorial:ttt:stored:1:1 X' \\
        -c 'source urn:iki:tutorial:ttt:stored:1:1'

``urn:iki:tutorial:ttt:stored:{x}:{y}`` is ONE family of resources, bound
by a template, answering three verbs:

* **Source** — the mark played at ``(x, y)``, ``text/plain``, cacheable. A
  square nobody has played is ``NotFoundError`` — the name is bound, and
  nothing is stored there. (The host's platonic cell turns exactly that
  variant, and no other, into the empty cell ``-``.)
* **Sink** — plays the mark in ``content``, trimmed. Any non-empty mark is
  kept — ``X``, ``O``, a Connect-Four ``R``: the atom knows no rules. An empty
  one is ``InvalidArgumentError`` on ``content``.
* **Delete** — clears the square. Idempotent.

``x`` and ``y`` are any integers, in their ONE plain spelling: ``01``, ``+1``
and ``-0`` are refused, because two names for one square would be two golden
threads over one piece of state. The library enforces that for every ``int``
binding; this module adds only the 64-bit range the Rust original has.

No invalidation code appears anywhere: the host kernel cuts the stored
cell's golden thread after every Sink or Delete it forwards here, and its
cached Source goes with it.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from typing import Annotated

from ikigai import Family, InvalidArgumentError, NotFoundError, family, serve
from ikigai.client import default_socket_path

#: The stored cell: what has been played at ``(x, y)``.
STORED = "urn:iki:tutorial:ttt:stored:{x}:{y}"

Column = Annotated[int, "the column — any integer"]
Row = Annotated[int, "the row — any integer"]

_I64 = range(-(2**63), 2**63)


def stored_name(x: int, y: int) -> str:
    """The stored cell's name at ``(x, y)``, spelled the one way it is accepted."""
    return f"urn:iki:tutorial:ttt:stored:{x}:{y}"


class CellStore:
    """The marks that have been played, and how often the stored cell has been
    read. The state lives outside the endpoint, where a test can hold it: the
    read counter is what proves a cached answer was SERVED, with no code
    running, rather than recomputed to the same bytes."""

    def __init__(self) -> None:
        self.marks: dict[tuple[int, int], str] = {}
        #: How many times the stored cell's Source has actually run (the
        #: default Exists runs it too, as the Rust original's does).
        self.reads = 0
        self.lock = threading.Lock()  # one thread per connection


def _square(x: int, y: int) -> tuple[int, int]:
    # Python's int is unbounded and Rust's coordinate is an i64, whose parse
    # refuses the rest with the same words. Keep the two atoms one contract.
    for name, value in (("x", x), ("y", y)):
        if value not in _I64:
            raise InvalidArgumentError(
                name, f"`{value}` is not an integer in its plain form (e.g. 0, 2, -1)"
            )
    return x, y


def stored_cell(store: CellStore) -> Family:
    """``ttt-stored``: the mark at ``(x, y)``, held in ``store``."""
    cell = family(
        STORED,
        id="ttt-stored",
        title="Stored cell",
        summary="What has been played at (x, y): read it, play a mark, or clear it.",
    )

    @cell.source(cacheable=True, summary="the mark played at (x, y); NotFound if none has been")
    def read(x: Column, y: Row) -> str:
        at = _square(x, y)
        with store.lock:
            store.reads += 1
            mark = store.marks.get(at)
        if mark is None:
            raise NotFoundError(f"nothing has been played at {x},{y}")
        return mark

    @cell.sink(summary="play a mark at (x, y)")
    def play(
        x: Column,
        y: Row,
        content: Annotated[str, "the mark to play, e.g. X or O"],
    ) -> str:
        at = _square(x, y)
        mark = content.strip()
        if not mark:
            raise InvalidArgumentError("content", "an empty mark — to clear a cell, delete it")
        with store.lock:
            store.marks[at] = mark
        return "ok"

    @cell.delete(summary="clear the cell at (x, y)")
    def clear(x: Column, y: Row) -> str:
        at = _square(x, y)
        with store.lock:
            store.marks.pop(at, None)
        return "ok"

    return cell


def main(argv: list[str]) -> int:
    path = Path(argv[0]) if argv else default_socket_path().parent / "ttt-store.sock"
    print(f"examples.tictactoe_store: serving {STORED} on {path}", file=sys.stderr)
    print(
        f"mount it:  ikigai --override urn:iki:tutorial:ttt:stored:={path} …",
        file=sys.stderr,
    )
    try:
        serve([stored_cell(CellStore())], path)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
