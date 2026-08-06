"""The money demo: two Python functions served as ikigai resources.

Terminal A::

    python -m ikigai.demo [socket-path] [--verbatim]

``--verbatim`` serves entry patterns unstripped (``urn:py:hello`` as-is) —
the form an ``--override``/``--prefer`` mount needs, since those forward
IRIs unchanged. The default (alias-stripped) is what ``--mount`` needs.

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
    # --verbatim: list entry patterns unstripped, the form override/prefer
    # mounts need (they forward IRIs unchanged; alias mounts re-prefix
    # stripped patterns and would double-prefix verbatim ones).
    strip_alias = "--verbatim" not in argv
    rest = [a for a in argv if a != "--verbatim"]
    if rest:
        path = Path(rest[0])
    else:
        path = default_socket_path().parent / "py.sock"
    print(f"ikigai-python demo: serving urn:py:hello, urn:py:reverse on {path}", file=sys.stderr)
    mount_flag = "--mount" if strip_alias else "--prefer"
    print(
        f"try:  ikigai {mount_flag} urn:py:={path} -c 'source urn:py:hello who=Ada'",
        file=sys.stderr,
    )
    try:
        serve([hello, reverse], path, strip_alias=strip_alias)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
