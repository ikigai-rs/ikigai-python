"""A by-IRI resource reader over a :class:`~ikigai.Client`: ``get`` and ``list``.

:class:`KernelResources` has the shape lectern's ``KernelSource`` reads through (lectern's
``lectern.source.KernelResources`` protocol): ``get(iri)`` answers a resource's bytes, or
``None`` when there is none, and ``list(container)`` answers the IRIs directly under a
container IRI. It only matches that shape. Nothing here imports lectern, and ikigai-python
keeps its one runtime dependency::

    from lectern.source import KernelSource
    import ikigai
    from ikigai.resources import KernelResources

    with ikigai.connect() as k:
        deck = KernelSource(KernelResources(k), "urn:file:talks/ai-sec/")

Absent is ``None``, never an error. A denial is a :class:`~ikigai.DeniedError` that names the
IRI it was refused, never a "file not found". Every other failure propagates as the client
raised it.
"""

from __future__ import annotations

from .client import Client
from .wire import DeniedError, NotFoundError

#: How ``get`` asks for a resource: the raw bytes, whatever the file holds.
OCTET_STREAM = "application/octet-stream"


class KernelResources:
    """``get(iri) -> bytes | None`` and ``list(container) -> list[str]`` over a client."""

    def __init__(self, client: Client):
        """``client`` is any connected :class:`~ikigai.Client`; it is not closed here."""
        self.client = client

    def get(self, iri: str) -> bytes | None:
        """The bytes at ``iri`` (``Source as=application/octet-stream``), or ``None`` when the
        kernel says the resource does not exist (:class:`~ikigai.NotFoundError`)."""
        try:
            return self.client.source(iri, **{"as": OCTET_STREAM}).data
        except NotFoundError:
            return None
        except DeniedError as e:
            raise _denied("reading", iri, e) from e

    def list(self, container: str) -> list[str]:
        """The full IRIs directly under ``container``, in the kernel's order.

        A trailing ``/`` is dropped before asking, because lectern names a container
        ``urn:file:deck/`` and the kernel's canonical spelling is ``urn:file:deck``; the
        answers still start with ``urn:file:deck/``. A container that does not exist
        lists nothing, as an absent directory on disk does."""
        name = container.rstrip("/")
        try:
            if self.client.exists(name).data != b"true":
                return []
            return self.client.children(name)
        except DeniedError as e:
            raise _denied("listing", container, e) from e


def _denied(action: str, iri: str, cause: DeniedError) -> DeniedError:
    """A denial that says which IRI was refused, keeping the kernel's words and scopes."""
    return DeniedError(f"{action} {iri} was refused: {cause.message}", cause.lacking)
