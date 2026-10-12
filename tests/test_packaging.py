"""PyPI readiness is checked, not remembered (ledger #1363).

Publishing stays Brian's; this file only keeps the package READY to publish, so
a later edit cannot quietly drop a metadata field, a project URL, the docstring
gate or the packaging gate. The heavy half of the check is
``scripts/gates.sh``'s build + ``twine check`` (run by CI); what is pinned here
is the wiring that makes that gate run at all, and the metadata it checks.

``twine check`` is what proves the README renders on PyPI (it renders the long
description through ``readme-renderer``), so these tests do not duplicate it —
they pin that the gate runs and that nothing uploads.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Every field the item names, and the value a PyPI page needs.
REQUIRED_FIELDS = (
    "name",
    "version",
    "description",
    "readme",
    "license",
    "requires-python",
    "classifiers",
)


def load_pyproject() -> dict:
    with open(ROOT / "pyproject.toml", "rb") as f:
        return tomllib.load(f)


def test_metadata_is_complete():
    project = load_pyproject()["project"]
    missing = [field for field in REQUIRED_FIELDS if not project.get(field)]
    assert not missing, f"pyproject [project] is missing: {', '.join(missing)}"
    assert project["readme"] == "README.md", "PyPI renders the long description from README.md"
    assert re.match(r"^\d+\.\d+\.\d+", project["version"]), "PyPI requires a PEP 440 version"
    assert project["requires-python"].startswith(">="), "a floor, not a pin"


def test_project_urls_name_the_repository_and_its_issues():
    urls = load_pyproject()["project"]["urls"]
    assert urls["Homepage"].startswith("https://")
    assert urls["Repository"] == urls["Homepage"]
    assert urls["Issues"].startswith(urls["Repository"] + "/issues")


def test_classifiers_cover_every_supported_python():
    project = load_pyproject()["project"]
    floor = int(re.search(r"(\d+)\.(\d+)", project["requires-python"]).group(2))
    supported = {
        int(c.rsplit(" ", 1)[1].split(".")[1])
        for c in project["classifiers"]
        if c.startswith("Programming Language :: Python :: 3.")
    }
    # A classifier below the floor would claim an interpreter the package refuses.
    assert floor in supported, f"no classifier for the {floor} floor of requires-python"
    assert "License :: OSI Approved :: MIT License" not in project["classifiers"], (
        "license = 'MIT OR Apache-2.0' is an SPDX expression; a License :: classifier "
        "would contradict it (PEP 639)"
    )


def test_the_packaging_gate_builds_and_never_uploads():
    script = (ROOT / "scripts" / "gates.sh").read_text()
    assert "-m build" in script, "the gate must build the sdist and wheel"
    assert "-m twine check" in script, "the gate must run twine check on the artifacts"
    # The fence, restated where a careless edit would land: no upload COMMAND,
    # ever (a comment may name the thing it refuses).
    commands = [line for line in script.splitlines() if not line.lstrip().startswith("#")]
    assert not any(re.search(r"twine\s+upload", line) for line in commands), (
        "publishing is Brian's — the gate never uploads"
    )


def test_ci_runs_the_gates_script_so_the_packaging_gate_cannot_be_skipped():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert "scripts/gates.sh python" in workflow


def test_dev_extra_carries_the_packaging_tools():
    # The gate's tools must be installed by the same extras CI installs, or the
    # packaging step would fail on a clean runner.
    dev = load_pyproject()["project"]["optional-dependencies"]["dev"]
    names = {re.match(r"[A-Za-z0-9._-]+", req).group(0) for req in dev}
    assert {"build", "twine"} <= names, f"dev extras lack the packaging tools: {sorted(names)}"


def test_docstring_gate_is_selected_for_the_public_api():
    lint = load_pyproject()["tool"]["ruff"]["lint"]
    assert "D1" in lint["select"], "the public-API docstring gate is ruff's D1 rules"
    ignores = lint["per-file-ignores"]
    # tests/ and examples/ are not shipped, so their test-name-as-docstring
    # convention is exempt — but the exemption must not cover src/.
    assert "D1" in ignores["tests/**"]
    assert "D1" in ignores["examples/**"]
    assert not any(pattern.startswith("src") for pattern in ignores)
