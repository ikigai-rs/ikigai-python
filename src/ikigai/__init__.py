"""ikigai-python: a pure-Python client and servable peer for the ikigai wire protocol.

L0 of the polyglot ladder: stdlib-only (socket/asyncio/struct), speaking the
length-prefixed postcard wire protocol over Unix domain sockets.
"""

PROTOCOL_VERSION = 5

__all__ = ["PROTOCOL_VERSION"]
