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

A ``urn:file:`` IRI is put into ikigai-fs's canonical percent-encoding before it is asked for
(lectern leaves sub-delims such as ``(``, ``+`` and ``&`` literal; ikigai-fs accepts only
unreserved characters and ``/`` literally), so ``q&a.md`` reads from the kernel as it does from
disk. A container is not a resource: ``get`` answers ``None`` for one, as a disk source does
for a directory.

Absent is ``None``, never an error. A denial is a :class:`~ikigai.DeniedError` that names the
IRI it was refused, never a "file not found". Every other failure propagates as the client
raised it.
"""

from __future__ import annotations

from urllib.parse import quote, unquote_to_bytes

from .client import URI_LIST, Client
from .wire import DeniedError, InvalidArgumentError, NotFoundError

#: How ``get`` asks for a resource: the raw bytes, whatever the file holds.
OCTET_STREAM = "application/octet-stream"

#: The scheme whose paths ikigai-fs requires in canonical percent-encoding.
FILE_SCHEME = "urn:file:"


def canonical_file_iri(iri: str) -> str:
    """``iri`` in ikigai-fs's canonical percent-encoding when it is a ``urn:file:`` IRI, and
    unchanged otherwise.

    ikigai-fs accepts one spelling of a path: UTF-8, unreserved characters (``A-Z a-z 0-9
    - . _ ~``) and ``/`` literal, every other byte as ``%XX`` in uppercase hex. Each segment is
    decoded to bytes and encoded again, so ``urn:file:deck/a(b)+c.md`` becomes
    ``urn:file:deck/a%28b%29%2Bc.md``, ``%7e`` becomes ``~``, and a canonical IRI is
    unchanged. An escaped ``/`` (``%2F``) stays escaped: it is never a separator."""
    if not iri.startswith(FILE_SCHEME):
        return iri
    path = iri[len(FILE_SCHEME) :]
    return FILE_SCHEME + "/".join(quote(unquote_to_bytes(s), safe="") for s in path.split("/"))


class KernelResources:
    """``get(iri) -> bytes | None`` and ``list(container) -> list[str]`` over a client."""

    def __init__(self, client: Client):
        """``client`` is any connected :class:`~ikigai.Client`; it is not closed here."""
        self.client = client

    def get(self, iri: str) -> bytes | None:
        """The bytes at ``iri`` (``Source as=application/octet-stream``), or ``None`` when the
        kernel says the resource does not exist (:class:`~ikigai.NotFoundError`) or ``iri``
        names a container.

        ikigai-fs reads a directory as its listing, so a read that succeeds is followed by
        asking for the listing face (``as=text/uri-list``): a container answers it, and a
        file refuses it (:class:`~ikigai.InvalidArgumentError`)."""
        name = canonical_file_iri(iri)
        if name.startswith(FILE_SCHEME):
            # ``urn:file:deck/`` names the container ``urn:file:deck``; ikigai-fs refuses the
            # trailing ``/``, and the probe below answers ``None`` for it.
            name = name.rstrip("/")
        try:
            data = self.client.source(name, **{"as": OCTET_STREAM}).data
        except NotFoundError:
            return None
        except DeniedError as e:
            raise _denied("reading", iri, e) from e
        try:
            self.client.source(name, **{"as": URI_LIST})
        except InvalidArgumentError:
            return data
        except DeniedError as e:
            raise _denied("reading", iri, e) from e
        return None

    def list(self, container: str) -> list[str]:
        """The full IRIs directly under ``container``, in the kernel's order.

        A trailing ``/`` is dropped before asking, because lectern names a container
        ``urn:file:deck/`` and the kernel's canonical spelling is ``urn:file:deck``. Each
        answer is spelled ``container`` (with a ``/``) followed by the child's name as the
        kernel encodes it, so it starts with the container exactly as the caller wrote it,
        even when ikigai-fs spells that container differently (``urn:file:q&a/`` lists
        ``urn:file:q&a/x.md``, not ``urn:file:q%26a/x.md``). A container that does not
        exist lists nothing, as an absent directory on disk does."""
        name = canonical_file_iri(container.rstrip("/"))
        prefix = container if container.endswith("/") else container + "/"
        try:
            if self.client.exists(name).data != b"true":
                return []
            children = self.client.children(name)
        except DeniedError as e:
            raise _denied("listing", container, e) from e
        under = name + "/"
        return [prefix + c[len(under) :] if c.startswith(under) else c for c in children]


def _denied(action: str, iri: str, cause: DeniedError) -> DeniedError:
    """A denial that says which IRI was refused, keeping the kernel's words and scopes."""
    return DeniedError(f"{action} {iri} was refused: {cause.message}", cause.lacking)
