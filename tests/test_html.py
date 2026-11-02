"""``ikigai.html`` against the shared vectors (ledger #1813).

``tests/vectors/html_vectors.json`` is a VERBATIM copy of ikigai-fn's
``tests/vectors/html_vectors.json`` at tag v0.9.0 (commit c5c871a, ikigai-fn PR 33). To pick up new
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
    assert set(VECTORS) == {
        "about",
        "escape",
        "url_segment",
        "percent_decode",
        "safe_href",
        "json_attr",
    }


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


DECODE = [
    pytest.param(c, id=f"{i}-{c.get('error', 'ok')}")
    for i, c in enumerate(VECTORS["percent_decode"])
]


@pytest.mark.parametrize("case", DECODE)
def test_percent_decode(case):
    given, plus = case["in"], case["plus_is_space"]
    error = case.get("error")
    if error is None:
        assert html.percent_decode(given, plus) == case["out"]
        assert html.percent_decode_bytes(given.encode("utf-8"), plus) == bytes(case["bytes"])
    elif error == "malformed_escape":
        both = ((html.percent_decode, given), (html.percent_decode_bytes, given.encode("utf-8")))
        for fn, arg in both:
            with pytest.raises(html.PercentDecodeError) as e:
                fn(arg, plus)
            assert (e.value.kind, e.value.offset) == ("malformed_escape", case["offset"])
    else:
        assert error == "invalid_utf8", f"unknown error kind {error!r}: a port runs every case"
        assert html.percent_decode_bytes(given.encode("utf-8"), plus) == bytes(case["bytes"])
        with pytest.raises(html.PercentDecodeError) as e:
            html.percent_decode(given, plus)
        assert (e.value.kind, e.value.offset) == ("invalid_utf8", None)
        assert isinstance(e.value.__cause__, UnicodeDecodeError)


@pytest.mark.parametrize("plus", [False, True])
@pytest.mark.parametrize(("given", "encoded"), cases("url_segment"))
def test_url_segment_decodes_back(given, encoded, plus):
    assert html.percent_decode(encoded, plus) == given


def test_not_urllib_unquote():
    """The cases ``urllib.parse.unquote`` lets through are refused here."""
    with pytest.raises(html.PercentDecodeError):
        html.percent_decode("100%", False)
    with pytest.raises(html.PercentDecodeError):
        html.percent_decode("%E9t%E9", False)


def test_bytes_decoder_takes_raw_bytes():
    assert html.percent_decode_bytes(b"%FF+%2b", True) == b"\xff +"
    assert html.percent_decode_bytes(bytearray(b"\xff"), False) == b"\xff"
    with pytest.raises(TypeError):
        html.percent_decode_bytes("text", False)


def test_percent_decode_error_words():
    """The refusals read as ``ikigai_fn::html::PercentDecodeError``'s ``Display``."""
    with pytest.raises(html.PercentDecodeError) as e:
        html.percent_decode("日%0g", True)
    assert str(e.value) == "percent_decode: malformed percent-escape at byte 3"
    with pytest.raises(html.PercentDecodeError) as e:
        html.percent_decode("%FF", False)
    assert str(e.value) == "percent_decode: decoded bytes are not UTF-8"
