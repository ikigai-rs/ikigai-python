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
from .serve import ArgSpec, EndpointDef, Family, Server, endpoint, family, serve
from .template import TemplateError, UriTemplate
from .wire import (
    PROTOCOL_VERSION,
    CacheStatus,
    Capability,
    Content,
    DeniedError,
    EndpointError,
    Expiry,
    Inline,
    InvalidArgumentError,
    MissingArgumentError,
    NotFoundError,
    ProtocolError,
    Reference,
    Representation,
    Request,
    SpaceEntry,
    TimeoutError,
    TraceEvent,
    UnavailableError,
    UnresolvedError,
    Verb,
    WireError,
)

__all__ = [
    "DEFAULT_TIMEOUT",
    "PROTOCOL_VERSION",
    "ArgSpec",
    "Client",
    "ConnectionLost",
    "EndpointDef",
    "Family",
    "Server",
    "connect",
    "default_socket_path",
    "endpoint",
    "family",
    "serve",
    "CacheStatus",
    "Capability",
    "Content",
    "DeniedError",
    "EndpointError",
    "Expiry",
    "Inline",
    "InvalidArgumentError",
    "MissingArgumentError",
    "NotFoundError",
    "ProtocolError",
    "Reference",
    "Representation",
    "Request",
    "SpaceEntry",
    "TemplateError",
    "TimeoutError",
    "TraceEvent",
    "UnavailableError",
    "UnresolvedError",
    "UriTemplate",
    "Verb",
    "WireError",
]
