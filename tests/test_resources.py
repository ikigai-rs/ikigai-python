"""``Client.children`` and the ``KernelResources`` adapter, without a kernel.

The listing face's bytes are pinned by ``tests/vectors/listing_vectors.json`` (shared with the
TypeScript face); ``tests/test_resources_interop.py`` holds the live kernel to the same file.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

import ikigai
from ikigai.client import URI_LIST, parse_uri_list
from ikigai.resources import KernelResources

VECTORS = json.loads(
    (Path(__file__).parent / "vectors" / "listing_vectors.json").read_text(encoding="utf-8")
)
CASES = VECTORS["kernel"] + VECTORS["parse"]


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.get("name") or c["container"])
def test_a_listing_body_reads_as_the_vectors_children(case):
    assert parse_uri_list(case["body"].encode("utf-8")) == case["children"]


def test_a_listing_that_is_not_utf8_is_a_protocol_error():
    with pytest.raises(ikigai.ProtocolError, match="not UTF-8"):
        parse_uri_list(b"urn:file:\xff")


class FakeClient:
    """``source`` and ``exists`` over a dict of files and a dict of directories."""

    def __init__(self, files=(), dirs=(), denied=()):
        self.files = dict(files)
        self.dirs = dict(dirs)
        self.denied = set(denied)
        self.calls: list[tuple[str, str, dict]] = []

    def _check(self, iri):
        if iri in self.denied:
            raise ikigai.DeniedError("lacks urn:cap:fs:read:x", ["urn:cap:fs:read:*"])

    def source(self, iri, **args):
        self.calls.append(("source", iri, args))
        self._check(iri)
        if args.get("as") == URI_LIST:
            if iri not in self.dirs:
                raise ikigai.InvalidArgumentError("as", "a directory listing face only")
            return ikigai.Representation("\n".join(self.dirs[iri]).encode(), URI_LIST)
        if iri not in self.files:
            raise ikigai.NotFoundError(f"`{iri}` does not exist")
        return ikigai.Representation(self.files[iri], "application/octet-stream")

    def exists(self, iri, **args):
        self.calls.append(("exists", iri, args))
        self._check(iri)
        found = iri in self.files or iri in self.dirs
        return ikigai.Representation(b"true" if found else b"false")

    def children(self, container, **args):
        return ikigai.Client.children(self, container, **args)


def test_children_asks_for_the_uri_list_face():
    client = FakeClient(dirs={"urn:file:d": ["urn:file:d/a.md", "urn:file:d/b.md"]})
    assert client.children("urn:file:d") == ["urn:file:d/a.md", "urn:file:d/b.md"]
    assert client.calls == [("source", "urn:file:d", {"as": "text/uri-list"})]


def test_get_answers_the_raw_bytes():
    client = FakeClient(files={"urn:file:d/a.png": b"\x89PNG"})
    assert KernelResources(client).get("urn:file:d/a.png") == b"\x89PNG"
    assert client.calls == [("source", "urn:file:d/a.png", {"as": "application/octet-stream"})]


def test_absent_is_none_not_an_error():
    assert KernelResources(FakeClient()).get("urn:file:d/gone.md") is None


def test_a_denied_read_names_the_iri_and_is_not_file_not_found():
    resources = KernelResources(FakeClient(denied={"urn:file:d/secret.md"}))
    with pytest.raises(ikigai.DeniedError) as raised:
        resources.get("urn:file:d/secret.md")
    assert not isinstance(raised.value, FileNotFoundError)
    assert raised.value.message == (
        "reading urn:file:d/secret.md was refused: lacks urn:cap:fs:read:x"
    )
    assert raised.value.lacking == ("urn:cap:fs:read:*",)
    assert "not found" not in raised.value.display().lower()


def test_a_denied_listing_names_the_container():
    resources = KernelResources(FakeClient(denied={"urn:file:d"}))
    with pytest.raises(ikigai.DeniedError, match="listing urn:file:d/ was refused"):
        resources.list("urn:file:d/")


def test_list_drops_the_trailing_slash_the_kernel_refuses():
    client = FakeClient(dirs={"urn:file:d": ["urn:file:d/a.md"]})
    assert KernelResources(client).list("urn:file:d/") == ["urn:file:d/a.md"]
    assert [iri for _, iri, _ in client.calls] == ["urn:file:d", "urn:file:d"]


def test_an_absent_container_lists_nothing():
    assert KernelResources(FakeClient()).list("urn:file:nowhere/") == []


def test_the_adapter_never_imports_lectern():
    tree = ast.parse(Path(ikigai.resources.__file__).read_text(encoding="utf-8"))
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        (node.module or "").split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.level == 0
    }
    assert imported <= {"__future__"}
