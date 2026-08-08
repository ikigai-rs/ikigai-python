"""The endpoint set the example apps resolve: hello / upper / reverse.

All three are pure functions of their inputs, so they declare
``cacheable=True`` — served directly that only marks the representation
(``Expiry::Never``); served through a Rust kernel mount the kernel actually
caches them, and the apps' ``X-Ikigai-Cache`` header flips to ``HIT``.

Serve them on a socket::

    python -m examples.endpoints [socket-path]

Then point any example app at that socket via ``IKIGAI_SOCKET``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated

from ikigai import endpoint, serve
from ikigai.client import default_socket_path

# The specs are DERIVED from the signatures (the L2 rung): each parameter's
# annotation becomes its XSD class on the describe face, ``Annotated``
# metadata becomes its summary, and a default would mark it optional. The
# explicit args= dicts these endpoints used to declare produce byte-for-byte
# the same face — args= remains available as the explicit override.


@endpoint("urn:py:hello", summary="Greet someone", cacheable=True)
def hello(who: Annotated[str, "the name to greet"]) -> str:
    return f"Hello, {who}!"


@endpoint("urn:py:upper", summary="Uppercase a string", cacheable=True)
def upper(text: Annotated[str, "the text to uppercase"]) -> str:
    return text.upper()


@endpoint("urn:py:reverse", summary="Reverse a string", cacheable=True)
def reverse(text: Annotated[str, "the text to reverse"]) -> str:
    return text[::-1]


ENDPOINTS = [hello, upper, reverse]


def main(argv: list[str]) -> int:
    if argv:
        path = Path(argv[0])
    else:
        path = default_socket_path().parent / "py-examples.sock"
    print(
        f"examples.endpoints: serving urn:py:hello, urn:py:upper, urn:py:reverse on {path}",
        file=sys.stderr,
    )
    print(f"point an example app at it:  IKIGAI_SOCKET={path}", file=sys.stderr)
    try:
        serve(ENDPOINTS, path)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
