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


def test_the_adapter_reads_and_lists_through_the_kernel(kernel):
    def fill(files: Path) -> None:
        (files / "deck").mkdir()
        (files / "deck" / "a.md").write_bytes(b"# A\n")
        (files / "deck" / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00")

    resources = KernelResources(kernel(fill))
    assert resources.get("urn:file:deck/a.md") == b"# A\n"
    assert resources.get("urn:file:deck/logo.png") == b"\x89PNG\r\n\x1a\n\x00"
    assert resources.get("urn:file:deck/gone.md") is None
    assert resources.list("urn:file:deck/") == ["urn:file:deck/a.md", "urn:file:deck/logo.png"]
    assert resources.list("urn:file:nowhere/") == []


def _lectern(request):
    """``(lectern modules, sample deck dir)``, or skip; under ``--require-interop`` FAIL."""
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


def test_lecterns_sample_deck_renders_from_the_kernel_as_from_disk(request, kernel, tmp_path):
    modules, deck = _lectern(request)
    a11y, resolve_source, assemble_resolved, get_renderer, FilesystemSource, KernelSource = modules

    def render(root, src, out_dir: Path):
        resolved = resolve_source(root, src=src)
        built = assemble_resolved(resolved, src=src)
        result = get_renderer(resolved.config.renderer).render(built, resolved.config, out_dir)
        return built, result

    def fill(files: Path) -> None:
        shutil.copytree(deck, files / "deck", ignore=shutil.ignore_patterns(".DS_Store"))

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
