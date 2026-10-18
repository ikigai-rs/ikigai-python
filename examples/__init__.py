"""REST faces over the ikigai wire client.

Each module in this package is a small web app whose handlers are thin faces
over ``kernel.source(...)`` — the framework does HTTP, ikigai does resolution.
See ``examples/README.md`` for the run modes (direct vs through the kernel).
"""

from ikigai import (
    ConflictError,
    DeniedError,
    DepthExceededError,
    EndpointError,
    InvalidArgumentError,
    MissingArgumentError,
    NotFoundError,
    OverBudgetError,
    RefusedError,
    RetryAfterError,
    TimeoutError,
    UnavailableError,
    UnresolvedError,
    UnsupportedError,
)


def error_status(exc: EndpointError) -> int:
    """The wire taxonomy → HTTP status table every example app shares, pinned to
    ``failure_class_vectors.json`` (core 0.1.108, ledger #1546): the payoff of typed
    errors crossing the wire. Unresolved and NotFound 404, Denied 403, Conflict 409,
    bad input 400, Unavailable 503, Timeout 504, RetryAfter 429 (with
    :func:`retry_after_header`), OverBudget 422, Refused 502, an Unsupported verb 405
    (a missing host facility 501), and the blanket 500 for a plain Endpoint failure,
    DepthExceeded and an unknown-future variant. (A 412 is for a precondition the
    CALLER stated, ``If-Match``; nothing here states one.)"""
    if isinstance(exc, DeniedError):
        return 403
    if isinstance(exc, NotFoundError | UnresolvedError):
        return 404
    if isinstance(exc, ConflictError):
        return 409
    if isinstance(exc, MissingArgumentError | InvalidArgumentError):
        return 400
    if isinstance(exc, UnavailableError):
        return 503
    if isinstance(exc, TimeoutError):
        return 504
    if isinstance(exc, RetryAfterError):
        return 429
    if isinstance(exc, OverBudgetError):
        return 422
    if isinstance(exc, RefusedError):
        return 502
    if isinstance(exc, UnsupportedError):
        return 405 if exc.verb is not None else 501
    if isinstance(exc, DepthExceededError):
        return 500
    return 500  # Endpoint and unknown-future: this side's (or the peer's) fault


def retry_after_header(exc: EndpointError) -> dict[str, str]:
    """The ``Retry-After`` header for ``exc``: delay-seconds, rounded up and never 0,
    only for a :class:`~ikigai.RetryAfterError` that carries a delay; else empty."""
    seconds = exc.retry_after_seconds() if isinstance(exc, RetryAfterError) else None
    return {} if seconds is None else {"Retry-After": str(seconds)}
