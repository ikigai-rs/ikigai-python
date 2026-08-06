"""The money demo: two Python functions served as ikigai resources.

Terminal A::

    python -m ikigai.demo [socket-path]

Terminal B::

    ikigai --mount urn:py:=<socket-path> -c 'source urn:py:hello who=Ada'
    # Hello, Ada!
    ikigai --mount urn:py:=<socket-path> -c list
    # …urn:py:hello  → hello   [<socket-path>]
"""

from __future__ import annotations

import sys
from pathlib import Path

from .client import default_socket_path
from .serve import endpoint, serve


@endpoint(
    "urn:py:hello",
    summary="Greet someone",
    args=[
        {
            "name": "who",
            "required": True,
            "summary": "the name to greet",
            "class": "http://www.w3.org/2001/XMLSchema#string",
        }
    ],
)
def hello(who: str) -> str:
    return f"Hello, {who}!"


@endpoint(
    "urn:py:reverse",
    summary="Reverse a string",
    args=[
        {
            "name": "in",
            "required": True,
            "summary": "the text to reverse",
            "class": "http://www.w3.org/2001/XMLSchema#string",
        }
    ],
    cacheable=True,  # a pure function of its input — the host kernel may cache it
)
def reverse(**kwargs) -> str:
    return kwargs["in"][::-1]


def main(argv: list[str]) -> int:
    if argv:
        path = Path(argv[0])
    else:
        path = default_socket_path().parent / "py.sock"
    print(f"ikigai-python demo: serving urn:py:hello, urn:py:reverse on {path}", file=sys.stderr)
    print(
        f"try:  ikigai --mount urn:py:={path} -c 'source urn:py:hello who=Ada'",
        file=sys.stderr,
    )
    try:
        serve([hello, reverse], path)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
