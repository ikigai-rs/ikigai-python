# ikigai-python local gates.
#
# `just gates` runs scripts/gates.sh, the same script .github/workflows/ci.yml runs
# after installing: ruff check, ruff format --check, pytest, then the packaging gate
# (python -m build + twine check on the sdist and wheel — never an upload), stopping
# at the first failure with that command's exit status. The commands live in the
# script only, so CI and this recipe cannot drift apart.
#
# The devtools gates (`just -f ~/git-personal/ikigai-devtools/justfile gates`) find
# this recipe because the repo has no Cargo.toml, and run it with a full log and a
# real exit status. Do not call those gates from here: they refuse to loop.
#
# With no argument the script picks the first of .venv/bin/python, venv/bin/python
# and python3 that has the dev and examples extras and imports ikigai from this
# checkout; `just gates path/to/python` names one instead.

# Lint, format check and tests, exactly as CI runs them.
gates *python:
    scripts/gates.sh {{python}}
