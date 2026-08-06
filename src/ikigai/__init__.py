"""ikigai-python: a pure-Python client and servable peer for the ikigai wire protocol.

L0 of the polyglot ladder: stdlib-only (socket/asyncio/struct), speaking the
length-prefixed postcard wire protocol over Unix domain sockets.
"""

from .wire import (
    PROTOCOL_VERSION,
    CacheStatus,
    Capability,
    Content,
    EndpointError,
    Expiry,
    Inline,
    ProtocolError,
    Reference,
    Representation,
    Request,
    SpaceEntry,
    TraceEvent,
    Verb,
    WireError,
)

__all__ = [
    "PROTOCOL_VERSION",
    "CacheStatus",
    "Capability",
    "Content",
    "EndpointError",
    "Expiry",
    "Inline",
    "ProtocolError",
    "Reference",
    "Representation",
    "Request",
    "SpaceEntry",
    "TraceEvent",
    "Verb",
    "WireError",
]
