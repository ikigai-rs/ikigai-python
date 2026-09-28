"""REST faces over the ikigai wire client.

Each module in this package is a small web app whose handlers are thin faces
over ``kernel.source(...)`` — the framework does HTTP, ikigai does resolution.
See ``examples/README.md`` for the run modes (direct vs through the kernel).
"""

from ikigai import (
    ConflictError,
    DeniedError,
    EndpointError,
    InvalidArgumentError,
    MissingArgumentError,
    NotFoundError,
)


def error_status(exc: EndpointError) -> int:
    """The wire taxonomy → HTTP status mapping every example app shares —
    the payoff of typed errors crossing the wire: a remote denial is a 403,
    an absent resource 404, a state that refuses the request 409 (wire v8's
    ``ConflictError`` — permanent, so not a 503), a bad input 400, a
    transient failure 503 (retry later), and only a genuine upstream fault
    stays the blanket 502. (A 412 is for a precondition the CALLER stated,
    ``If-Match``; nothing here states one.)"""
    if isinstance(exc, DeniedError):
        return 403
    if isinstance(exc, NotFoundError):
        return 404
    if isinstance(exc, ConflictError):
        return 409
    if isinstance(exc, MissingArgumentError | InvalidArgumentError):
        return 400
    if exc.transient:  # Timeout / Unavailable — re-issuing may succeed
        return 503
    return 502  # Unresolved, Endpoint, unknown-future: the upstream's fault
