"""``ikigai.contract.is_iri`` answers as core's ``Iri::parse`` does (ledger #1113).

Core decides whether a ``class`` or ``requires`` value is written as ``<…>`` or as a
literal by parsing it with ``oxiri`` 0.2.11; this face ports that parser. Every expected
answer below was recorded from ``oxiri::Iri::parse`` 0.2.11 itself (a scratch binary,
not this face), including the cases where the stdlib or the RFC would answer otherwise.
The ported parser also agreed with oxiri on 400,000 generated strings when it landed;
the vectors' ``core/hostile-terms`` door pins the emission that rides on it.
"""

from __future__ import annotations

import pytest

from ikigai.contract import escape_iri_fragment, is_iri

ORACLE = [
    # The hostile door's values, escaped as the emitter escapes them.
    ("urn:cap:core:read", True),
    ("urn:cls:a%3E%20b", True),
    ("urn:cap:x%3E%20.%20%3Curn:everything", True),
    ("http://ok%3E%20.%20%3Curn:z%3E%20a%20%3Curn:w", False),  # a port that is not digits
    ("https://cap%3E%20.%20%3Curn:z", False),
    ("not%20a%20class", False),  # no scheme
    ("not%20an%20iri", False),
    # Hosts.
    ("http://[::1]/", True),
    ("http://[::ffff:1.2.3.4]:80/x", True),
    ("http://[::1.2.3.4]", True),
    ("http://[1:2:3:4:5:6:7:1.2.3.4]", False),
    ("http://[fe80::1%25eth0]/", False),  # Python's ipaddress takes a zone; Rust does not
    ("http://[::ffff:01.2.3.4]/", False),
    ("http://[00001::]", False),
    ("http://[1::2::3]", False),
    ("http://[::1", False),
    ("http://[v1.x]", True),
    ("http://[v.x]", False),
    ("http://u@h@x", False),
    ("http://h:8a", False),
    # Escapes and schemes.
    ("s:%4", False),
    ("s:%zz", False),
    (":x", False),
    ("1s:x", False),
    ("s+-.9:x", True),
    ("mailto:a@b", True),
    ("urn:a?", True),  # iprivate, allowed in a query only
    ("urn:a", False),
    ("urn:a#", False),
    ("urn:\U000e1000", True),
    ("urn:\U000e0fff", False),
]


@pytest.mark.parametrize(("iri", "expected"), ORACLE)
def test_is_iri_answers_as_oxiri_0_2_11_does(iri, expected):
    assert is_iri(iri) is expected


def test_escaping_makes_a_legal_iriref_not_always_a_legal_iri():
    # The reason the parse exists. Recorded from oxiri, not from core's doc comment: core's
    # `iri_term` comment says `http://a> . <b` escapes to a non-IRI, but it has no ':' after
    # the authority, so its host is just percent-encoded and oxiri ACCEPTS it. A ':' makes
    # the rest a "port" that is not digits, and that one falls back to a literal.
    assert is_iri(escape_iri_fragment("http://a> . <b"))
    escaped = escape_iri_fragment("http://ok> . <urn:z")
    assert escaped == "http://ok%3E%20.%20%3Curn:z"
    assert not is_iri(escaped)
