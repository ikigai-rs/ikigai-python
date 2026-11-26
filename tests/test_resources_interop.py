"""The listing face and the lectern adapter against a real kernel: ``ikigai serve``.

Skips without the ``ikigai`` binary and FAILS without it under ``--require-interop`` (CI's
interop job), like every host-backed test. The deck test also needs lectern (lectern-slides
0.2, with ``KernelSource``; PyPI has only 0.1.0) installed and ``LECTERN_SAMPLE_DECK`` naming
its ``examples/sample-deck``. Without them it skips, and with ``IKIGAI_REQUIRE_LECTERN`` set
it FAILS instead, so a job that installs lectern cannot pass by skipping it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

import ikigai
from ikigai.resources import KernelResources
from tests.rust_door import isolated, require_ikigai

VECTORS = json.loads(
    (Path(__file__).parent / "vectors" / "listing_vectors.json").read_text(encoding="utf-8")
)


@pytest.fixture
def kernel(request):
    """``start(fill)``: an isolated ``ikigai serve`` over a fresh file root that ``fill(files)``
    populates first; answers a connected client."""
    binary = require_ikigai(request)
    stack: list = []

    def start(fill):
        d = Path(tempfile.mkdtemp(prefix="ik-res-"))
        argv = isolated(binary, d)
        fill(d / "files")
        sock = d / "kernel.sock"
        process = subprocess.Popen(
            [*argv, "serve", str(sock)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        stack.append((process, d))
        deadline = time.monotonic() + 15
        while not sock.exists():
            if time.monotonic() > deadline or process.poll() is not None:
                pytest.fail("ikigai serve did not come up")
            time.sleep(0.05)
        client = ikigai.connect(sock)
        stack.append(client)
        return client

    yield start
    for item in reversed(stack):
        if isinstance(item, tuple):
            process, d = item
            process.terminate()
            process.wait(timeout=10)
            shutil.rmtree(d, ignore_errors=True)
        else:
            item.close()


def _vector_tree(files: Path) -> None:
    for name in VECTORS["tree"]:
        path = files / name
        if name.endswith("/"):
            path.mkdir(parents=True, exist_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x", encoding="utf-8")


def test_the_kernel_answers_the_listing_vectors_byte_for_byte(kernel):
    client = kernel(_vector_tree)
    for case in VECTORS["kernel"]:
        representation = client.source(case["container"], **{"as": "text/uri-list"})
        assert representation.base_media_type == case["media_type"]
        assert representation.data == case["body"].encode("utf-8"), case["container"]
        assert client.children(case["container"]) == case["children"]


def test_the_kernel_reads_each_canonical_spelling_and_refuses_the_others(kernel):
    client = kernel(_vector_tree)
    for case in VECTORS["kernel"]:
        for child in case["children"]:
            if child.endswith("/sub"):
                continue
            assert client.source(child).data == b"x", child
    for case in VECTORS["canonical"]:
        spelled, canonical = case["spelled"], case["canonical"]
        # A literal space is not an IRI at all: the kernel drops the connection rather than
        # answering, so only spellings that are IRIs are held to the refusal here.
        if spelled != canonical and spelled.startswith("urn:file:") and " " not in spelled:
            with pytest.raises(ikigai.InvalidArgumentError, match="canonical percent-encoding"):
                client.source(spelled)


def test_the_adapter_reads_and_lists_through_the_kernel(kernel):
    def fill(files: Path) -> None:
        (files / "deck" / "q&a").mkdir(parents=True)
        (files / "deck" / "empty").mkdir()
        (files / "deck" / "a.md").write_bytes(b"# A\n")
        (files / "deck" / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00")
        (files / "deck" / "a(b)+c.md").write_bytes(b"# sub-delims\n")
        (files / "deck" / "don't.md").write_bytes(b"# apostrophe\n")
        (files / "deck" / "q&a" / "x(y).md").write_bytes(b"# nested\n")

    resources = KernelResources(kernel(fill))
    assert resources.get("urn:file:deck/a.md") == b"# A\n"
    assert resources.get("urn:file:deck/logo.png") == b"\x89PNG\r\n\x1a\n\x00"
    assert resources.get("urn:file:deck/gone.md") is None
    # lectern's KernelSource.iri() leaves sub-delims literal; ikigai-fs wants them escaped.
    assert resources.get("urn:file:deck/a(b)+c.md") == b"# sub-delims\n"
    assert resources.get("urn:file:deck/don't.md") == b"# apostrophe\n"
    assert resources.get("urn:file:deck/q&a/x(y).md") == b"# nested\n"
    # A container is not a resource, empty or not, as on disk.
    for container in (
        "urn:file:deck",
        "urn:file:deck/",
        "urn:file:deck/q&a",
        "urn:file:deck/empty",
    ):
        assert resources.get(container) is None, container
    assert resources.list("urn:file:deck/") == [
        "urn:file:deck/a%28b%29%2Bc.md",
        "urn:file:deck/a.md",
        "urn:file:deck/don%27t.md",
        "urn:file:deck/empty",
        "urn:file:deck/logo.png",
        "urn:file:deck/q%26a",
    ]
    assert resources.list("urn:file:deck/q&a/") == ["urn:file:deck/q&a/x%28y%29.md"]
    assert resources.list("urn:file:deck/empty/") == []
    assert resources.list("urn:file:nowhere/") == []


def _lectern():
    """``(lectern modules, sample deck dir)``, or skip; with ``IKIGAI_REQUIRE_LECTERN`` set,
    FAIL instead. ``--require-interop`` alone does not require lectern: CI's interop job sets
    ``IKIGAI_REQUIRE_LECTERN`` in the step that installs it."""
    deck = os.environ.get("LECTERN_SAMPLE_DECK")
    try:
        from lectern import a11y
        from lectern.config import resolve_source
        from lectern.preprocess import assemble_resolved
        from lectern.render import get_renderer
        from lectern.source import FilesystemSource, KernelSource
    except ImportError:
        missing = "lectern is not installed"
    else:
        if deck and (Path(deck) / "deck.toml").is_file():
            return (
                a11y,
                resolve_source,
                assemble_resolved,
                get_renderer,
                FilesystemSource,
                KernelSource,
            ), Path(deck)
        missing = f"LECTERN_SAMPLE_DECK does not name lectern's sample deck ({deck!r})"
    if os.environ.get("IKIGAI_REQUIRE_LECTERN"):
        pytest.fail(f"IKIGAI_REQUIRE_LECTERN is set, and {missing}")
    pytest.skip(missing)


def _sub_delim_names(deck: Path) -> None:
    """Rename a slide, a partial and an asset of a copied deck to names with sub-delims and an
    apostrophe, which lectern's ``KernelSource.iri()`` leaves literal and ikigai-fs wants
    escaped, and point ``deck.toml`` at them."""
    renames = {
        "slides/80-closing.md": "slides/80-q&a (don't).md",
        "_partials/principles.md": "_partials/a(b)+c.md",
    }
    toml = (deck / "deck.toml").read_text(encoding="utf-8")
    for old, new in renames.items():
        (deck / old).rename(deck / new)
        assert f'"{old}' in toml, old
        toml = toml.replace(f'"{old}', f'"{new}')
    (deck / "deck.toml").write_text(toml, encoding="utf-8")


@pytest.mark.parametrize("variant", ["as-shipped", "sub-delim-names"])
def test_lecterns_sample_deck_renders_from_the_kernel_as_from_disk(variant, kernel, tmp_path):
    modules, shipped = _lectern()
    deck = tmp_path / "sample-deck"
    shutil.copytree(shipped, deck, ignore=shutil.ignore_patterns(".DS_Store"))
    if variant == "sub-delim-names":
        _sub_delim_names(deck)
    a11y, resolve_source, assemble_resolved, get_renderer, FilesystemSource, KernelSource = modules

    def render(root, src, out_dir: Path):
        resolved = resolve_source(root, src=src)
        built = assemble_resolved(resolved, src=src)
        result = get_renderer(resolved.config.renderer).render(built, resolved.config, out_dir)
        return built, result

    def fill(files: Path) -> None:
        shutil.copytree(deck, files / "deck")

    client = kernel(fill)
    root = Path("/kernel/sample-deck")
    assert not root.exists()
    source = KernelSource(KernelResources(client), "urn:file:deck/", root)

    disk_deck, disk = render(deck, FilesystemSource(), tmp_path / "disk")
    kernel_deck, from_kernel = render(root, source, tmp_path / "kernel")

    assert from_kernel.output.read_text(encoding="utf-8") == disk.output.read_text(encoding="utf-8")
    assert kernel_deck.slide_count == disk_deck.slide_count > 0
    assert kernel_deck.markdown(provenance=False) == disk_deck.markdown(provenance=False)
    assert from_kernel.warnings == disk.warnings
    assert a11y.audit(kernel_deck) == a11y.audit(disk_deck)

    def assets(out: Path) -> dict[str, bytes]:
        return {
            p.relative_to(out).as_posix(): p.read_bytes() for p in out.rglob("*") if p.is_file()
        }

    assert assets(tmp_path / "kernel") == assets(tmp_path / "disk")
