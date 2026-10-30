"""The shared vectors stay LF in every checkout (ledger #1205).

devtools `vector-drift` compares the files below with their ikigai-core and
ikigai-tutorial originals by raw bytes, so a Windows checkout with
`core.autocrlf=true` that turned them CRLF would read as drift. `.gitattributes`
pins them to `eol=lf`; these tests pin that it does, without cloning anything.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

SHARED = [
    "tests/vectors/contract_cases.json",
    "tests/vectors/contract_vectors.json",
    "tests/vectors/capability_vectors.json",
    "tests/vectors/html_vectors.json",
    "tests/vectors/auth_vectors.json",
    "tests/vectors/failure_class_vectors.json",
    "tests/ttt_template_cases.txt",
    "tests/test_wire.py",
]


@pytest.mark.parametrize("path", SHARED)
def test_shared_file_has_no_carriage_returns(path):
    # The property vector-drift needs, read off this checkout's bytes.
    assert b"\r" not in (ROOT / path).read_bytes()


@pytest.mark.parametrize("path", SHARED)
def test_shared_file_is_checked_out_lf_whatever_autocrlf_says(path):
    # The cause, not just the symptom: an `eol=lf` attribute overrides
    # core.autocrlf, so even a CRLF-configured clone checks this path out LF.
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    inside = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        text=True,
    )
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        pytest.skip("not a git checkout (an sdist, say)")
    out = subprocess.run(
        ["git", "-C", str(ROOT), "check-attr", "text", "eol", "--", path],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert f"{path}: text: set\n" in out
    assert f"{path}: eol: lf\n" in out
