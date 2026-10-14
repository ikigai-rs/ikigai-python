#!/usr/bin/env bash
# The gates CI runs, in CI's order: lint, format check, tests, packaging.
#
#   scripts/gates.sh [python]
#
# ONE source of truth: .github/workflows/ci.yml calls this after its Install step, and
# the justfile's `gates` recipe calls it locally (which is what
# `just -f ~/git-personal/ikigai-devtools/justfile gates` runs in a repo with no
# Cargo.toml). Change the gates here and both follow.
#
# Every tool runs through ONE interpreter (`python -m ruff`, `python -m pytest`), so the
# linter, the formatter and the tests cannot come from three different environments.
# With an argument, that interpreter is used as given; CI passes `python`, the one
# setup-python installed into. Without one, the first of .venv/bin/python,
# venv/bin/python and python3 that passes the check below.
#
# The check REFUSES an interpreter that cannot run what CI runs, rather than running
# anyway, because both ways it can fall short pass silently:
#   - the examples smoke tests `importorskip` their web framework, and the QUIC tests
#     `aioquic`, so without the `examples` and `quic` extras they SKIP, pytest exits 0,
#     and the gate covers less than CI;
#   - an `ikigai` imported from anywhere but this checkout's src/ means the tests
#     exercise some other copy of the package and say nothing about this one.
# The extras are read from pyproject.toml, so the list is not repeated here.
#
# The packaging gate BUILDS the sdist and wheel and runs `twine check` on them, the
# same command that renders the README the way PyPI will — it does NOT upload.
# Publishing is Brian's, and the publish fence refuses `twine upload` anyway. The
# build goes to a temp dir so a `dist/` never lands in the checkout (and is removed
# even on failure).
#
# Installing is not this script's job: CI's Install step does it, and locally it is
# `python -m pip install -e '.[dev,examples,quic]'` into the venv you use.

set -u
cd "$(dirname "$0")/.." || exit 2

# Exit 0 iff $1 can run the gates; otherwise print why and exit 1.
check_python() {
    "$1" - <<'EOF'
import importlib.metadata as md
import re
import sys
import tomllib
from pathlib import Path

if sys.version_info < (3, 11):
    sys.exit(f"Python {sys.version.split()[0]} is older than the 3.11 this package requires")
with open("pyproject.toml", "rb") as f:
    extras = tomllib.load(f)["project"]["optional-dependencies"]
missing = []
for req in extras["dev"] + extras["examples"] + extras["quic"]:
    name = re.match(r"[A-Za-z0-9._-]+", req).group(0)
    try:
        md.distribution(name)
    except md.PackageNotFoundError:
        missing.append(name)
if missing:
    sys.exit("missing the dev/examples/quic extras: " + ", ".join(missing))
try:
    import ikigai
except ImportError:
    sys.exit("cannot import ikigai (not installed from this checkout)")
src = (Path.cwd() / "src").resolve()
here = Path(ikigai.__file__).resolve()
if src not in here.parents:
    sys.exit(f"imports ikigai from {here}, not from this checkout's src/")
EOF
}

if [ $# -gt 0 ]; then
    PY="$1"
    why="$(check_python "$PY" 2>&1)" || {
        echo "gates: $PY cannot run the gates: $why" >&2
        echo "  install with: $PY -m pip install -e '.[dev,examples,quic]'" >&2
        exit 2
    }
else
    PY=""
    report=""
    for cand in .venv/bin/python venv/bin/python python3; do
        command -v "$cand" >/dev/null 2>&1 || continue
        if why="$(check_python "$cand" 2>&1)"; then
            PY="$cand"
            break
        fi
        report="${report}  ${cand}: ${why}
"
    done
    if [ -z "$PY" ]; then
        echo "gates: no interpreter here can run the gates — NOTHING WAS GATED" >&2
        printf '%s' "$report" >&2
        echo "  install into one with: <python> -m pip install -e '.[dev,examples,quic]'" >&2
        exit 2
    fi
fi

echo "gates: $PY ($("$PY" --version 2>&1))"
echo "── ruff check" &&
    "$PY" -m ruff check . &&
    echo "── ruff format --check" &&
    "$PY" -m ruff format --check . &&
    echo "── pytest" &&
    "$PY" -m pytest -q &&
    echo "── build + twine check (sdist and wheel; no upload)" &&
    DEST="$(mktemp -d)" &&
    trap 'rm -rf "$DEST"' EXIT &&
    "$PY" -m build --outdir "$DEST" . &&
    "$PY" -m twine check "$DEST"/*
