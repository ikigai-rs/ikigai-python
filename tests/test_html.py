"""``ikigai.html`` against the shared vectors (ledger #1813).

``tests/vectors/html_vectors.json`` is a VERBATIM copy of ikigai-fn's
``tests/vectors/html_vectors.json`` at commit 53d50c5 (ikigai-fn PR 18). To pick up new
vectors, copy that file again, unedited; it stays ``eol=lf`` through ``.gitattributes``.
"""

import json
from pathlib import Path

import pytest

from ikigai import html

PATH = Path(__file__).resolve().parent / "vectors" / "html_vectors.json"
VECTORS = json.loads(PATH.read_text(encoding="utf-8"))


def cases(section):
    return [pytest.param(c["in"], c["out"], id=f"{i}") for i, c in enumerate(VECTORS[section])]


def test_every_section_is_run():
    assert set(VECTORS) == {"about", "escape", "url_segment", "safe_href", "json_attr"}


@pytest.mark.parametrize("fn", [html.html_text, html.attr])
@pytest.mark.parametrize(("given", "want"), cases("escape"))
def test_escape(fn, given, want):
    assert fn(given) == want


@pytest.mark.parametrize(("given", "want"), cases("url_segment"))
def test_url_segment(given, want):
    assert html.url_segment(given) == want


@pytest.mark.parametrize(("given", "want"), cases("safe_href"))
def test_safe_href(given, want):
    assert html.safe_href(given) == want


@pytest.mark.parametrize(("given", "want"), cases("json_attr"))
def test_json_attr(given, want):
    assert html.json_attr([tuple(p) for p in given]) == want
