"""ikigai-python: a pure-Python client and servable peer for the ikigai wire protocol.

L0 of the polyglot ladder: stdlib-only (socket/asyncio/struct), speaking the
length-prefixed postcard wire protocol over Unix domain sockets.
"""

from .client import (
    DEFAULT_TIMEOUT,
    Client,
    ConnectionLost,
    connect,
    default_socket_path,
)
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
    "DEFAULT_TIMEOUT",
    "PROTOCOL_VERSION",
    "Client",
    "ConnectionLost",
    "connect",
    "default_socket_path",
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
