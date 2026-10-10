"""ikigai-python: a pure-Python client and servable peer for the ikigai wire protocol.

L0 of the polyglot ladder, speaking the length-prefixed postcard wire protocol over
Unix domain sockets. The codec and transport are stdlib (socket/asyncio/struct); the
one dependency, ``blake3``, names a served door's content-addressed contracts.
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
    MIN_PROTOCOL_VERSION,
    PROTOCOL_VERSION,
    CacheStatus,
    Capability,
    ConflictError,
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
    "MIN_PROTOCOL_VERSION",
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
    "ConflictError",
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
