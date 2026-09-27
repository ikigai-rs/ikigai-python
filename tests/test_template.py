"""UriTemplate: a mirror of ikigai_core::UriTemplate (grammar.rs).

The first block ports core's own tests one for one; the rest pin the rule's
edges that core states in prose (leftmost capture, remainder capture, no
character class), because the host replays this face's catalog patterns
with core's matcher and the two must agree on every IRI.
"""

import pytest

from ikigai import TemplateError, UriTemplate

# -- core's tests, ported ---------------------------------------------------


def test_exact_matches_only_itself():
    t = UriTemplate("urn:test:to-upper")
    assert t.match("urn:test:to-upper") == {}
    assert t.match("urn:test:to-lower") is None
    assert t.is_exact


def test_template_captures_trailing_var():
    t = UriTemplate("urn:test:echo/{message}")
    assert t.match("urn:test:echo/hello") == {"message": "hello"}
    assert t.match("urn:test:echo/") is None  # empty capture
    assert t.match("urn:other:echo/hi") is None


def test_template_captures_middle_var():
    t = UriTemplate("urn:r:{id}/data")
    assert t.match("urn:r:42/data") == {"id": "42"}
    assert t.match("urn:r:42/other") is None


def test_expand_is_inverse_of_match():
    t = UriTemplate("urn:r:{id}/data")
    assert t.expand(t.match("urn:r:7/data")) == "urn:r:7/data"
    assert t.expand({}) is None  # a missing variable


def test_pattern_reflects_the_grammar():
    assert UriTemplate("urn:demo:echo/{message}").source == "urn:demo:echo/{message}"


@pytest.mark.parametrize(
    ("template", "detail"),
    [
        ("urn:{a}{b}", "adjacent variables are ambiguous in `urn:{a}{b}`"),
        ("urn:{a", "unclosed '{' in `urn:{a`"),
        ("urn:{}", "invalid variable `{}` in `urn:{}`"),
    ],
)
def test_rejects_ambiguous_and_malformed(template, detail):
    with pytest.raises(TemplateError) as e:
        UriTemplate(template)
    assert str(e.value) == f"invalid URI template: {detail}"


# -- the rule's edges -------------------------------------------------------


def test_a_middle_variable_is_lazy_and_the_last_takes_the_rest():
    # `{x}` stops at the LEFTMOST `:`; the trailing `{y}` swallows separators.
    t = UriTemplate("urn:iki:tutorial:ttt:stored:{x}:{y}")
    assert t.match("urn:iki:tutorial:ttt:stored:1:2") == {"x": "1", "y": "2"}
    assert t.match("urn:iki:tutorial:ttt:stored:1:2:3") == {"x": "1", "y": "2:3"}
    assert t.match("urn:iki:tutorial:ttt:stored:-1:0") == {"x": "-1", "y": "0"}
    assert t.match("urn:iki:tutorial:ttt:stored::2") is None  # empty x
    assert t.match("urn:iki:tutorial:ttt:stored:1:") is None  # empty y
    assert t.match("urn:iki:tutorial:ttt:stored:12") is None  # no separator at all


def test_a_capture_has_no_character_class():
    # Shape, never content: judging `01` or `a b` is the handler's business.
    t = UriTemplate("urn:x:{v}")
    assert t.match("urn:x:a b/{c}") == {"v": "a b/{c}"}


def test_a_template_matches_its_own_source():
    # What lets Meta on the catalog pattern itself describe the family.
    t = UriTemplate("urn:iki:tutorial:ttt:stored:{x}:{y}")
    assert t.match(t.source) == {"x": "{x}", "y": "{y}"}


def test_a_literal_after_the_last_variable_must_close_the_iri():
    t = UriTemplate("urn:r:{id}/data")
    assert t.match("urn:r:1/data/more") is None
    # the leftmost `/data` is taken, so a second one cannot rescue the match
    assert t.match("urn:r:1/data/data") is None


def test_variable_names_are_ascii_word_characters():
    assert UriTemplate("urn:{a_1}:{B2}").variables == ("a_1", "B2")
    for bad in ("urn:{a-b}", "urn:{é}", "urn:{٣}", "urn:{a b}", "urn:{+a}", "urn:{a{b}"):
        with pytest.raises(TemplateError, match="invalid variable"):
            UriTemplate(bad)


def test_a_lone_close_brace_is_literal():
    t = UriTemplate("urn:a}b:{x}")
    assert t.match("urn:a}b:1") == {"x": "1"}
