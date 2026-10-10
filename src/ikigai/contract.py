"""The two identities an action has (ledger #948), mirroring ``ikigai_core::contract``.

An endpoint's id is not unique in a kernel: a mount surfaces a peer's endpoint under
the peer's id, and one endpoint can sit at two doors. So a manifold row and a catalog
contract node cannot be named after the id alone, which is what the retired
``urn:ikigai:endpoint:{id}:action:{verb}`` did. Core names them two ways instead:

- a **match IRI**, one per door and verb: ``urn:ikigai:match:{verb}:{pattern}``
  (:func:`match_iri`), which is what a host's ``urn:kernel:actions`` rows are;
- a **contract IRI**, one per verb's contract, CONTENT-ADDRESSED:
  ``urn:ikigai:contract:{id}:{verb}:b3:{hex}`` (:func:`contract_iri`), which is the
  ``ik:Action`` node a Meta turtle face and the catalog write. Identical contracts
  share a node; different ones never do.

The digest is BLAKE3 over core's prefix-free canonical form, so this module needs the
``blake3`` package (the reference binding, over the same Rust ``blake3`` crate core
hashes with): ``hashlib`` has no BLAKE3, and the IRI is only useful if it is
byte-for-byte the one a Rust kernel computes for the same contract. The vectors in
``tests/vectors/contract_vectors.json`` are a verbatim copy of ikigai-vocab's
``tests/vectors/contract_vectors.json``, which core computes and refuses to let drift,
and pin that (ledger #1106).

Lenient in, canonical out (ledger #1107): :func:`parse_match_iri` and
:func:`parse_contract_iri` accept every spelling core accepts, and anything this face
says back about an IRI it was handed goes through :func:`canonical_match_iri` /
:func:`canonical_contract_iri`. What it mints (:func:`match_iri`, :func:`contract_iri`)
is canonical by construction.

Contracts are taken in the serde (JSON) shape of ``ikigai_core::ActionSpec`` — the
shape a door's ``description_json`` carries and a Rust host deserializes when it
mounts this peer — so the digest here and the host's are computed from the same data.
"""

from __future__ import annotations

import struct

import blake3

from .wire import Verb

#: The prefix of every match IRI: ``urn:ikigai:match:{verb}:{pattern}``.
MATCH_PREFIX = "urn:ikigai:match:"

#: The prefix of every content-addressed contract IRI:
#: ``urn:ikigai:contract:{id}:{verb}:b3:{hex}``.
CONTRACT_PREFIX = "urn:ikigai:contract:"

# The domain tag that opens the canonical form (core's CONTRACT_FORM). Core bumps it if
# the form ever changes, so a stale mirror computes digests that match nothing rather
# than digests that match the wrong thing.
_CONTRACT_FORM = "ikigai:contract:v1"

# Everything a Turtle IRIREF cannot carry literally.
_IRIREF_FORBIDDEN = frozenset('<>"{}|^`\\')


def _forbidden(c: str) -> bool:
    return c <= " " or c in _IRIREF_FORBIDDEN


def _percent(c: str) -> str:
    return "".join(f"%{byte:02X}" for byte in c.encode("utf-8"))


def escape_iri_fragment(s: str) -> str:
    """``ikigai_core::escape_iri_fragment``: percent-encode what a Turtle ``IRIREF``
    cannot carry, leaving ``%`` alone (so NOT injective, by core's design: authored
    IRIs may already carry escapes). Safe strings come back unchanged."""
    return "".join(_percent(c) if _forbidden(c) else c for c in s)


def _encode_segment(s: str) -> str:
    """Core's injective segment encoding: the IRIREF set AND ``%`` itself, so every
    match or contract IRI parses back to exactly what made it."""
    return "".join(_percent(c) if c == "%" or _forbidden(c) else c for c in s)


_HEX = frozenset(b"0123456789abcdefABCDEF")


def _hex_byte(pair: bytes) -> int | None:
    """One escape's two bytes after ``%``: exactly two ASCII hex digits (RFC 3986
    ``pct-encoded``), else ``None``. A sign is refused, as core refuses it since 0.1.94
    (ledger #1096): before, ``%+1`` decoded to the same byte as ``%01``. Python's
    ``int`` is only called on checked digits, since it would also take a sign,
    whitespace and ``_``."""
    if len(pair) != 2 or any(b not in _HEX for b in pair):
        return None
    return int(pair.decode("ascii"), 16)


def _decode_segment(s: str) -> str | None:
    out = bytearray()
    i = 0
    raw = s.encode("utf-8")
    while i < len(raw):
        if raw[i] == 0x25:  # '%'
            pair = _hex_byte(raw[i + 1 : i + 3])
            if pair is None:
                return None
            out.append(pair)
            i += 3
        else:
            out.append(raw[i])
            i += 1
    try:
        return out.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _verb_segment(verb: Verb) -> str:
    return verb.name.lower()


def _parse_verb_segment(name: str) -> Verb | None:
    if name != name.lower():
        return None
    try:
        return Verb[name.upper()]
    except KeyError:
        return None


def match_iri(verb: Verb, pattern: str) -> str:
    """The match IRI of one door and verb: ``urn:ikigai:match:{verb}:{pattern}``, the
    pattern percent-encoded injectively (a template's braces become ``%7B``/``%7D``).

    >>> match_iri(Verb.SOURCE, "urn:py:echo/{message}")
    'urn:ikigai:match:source:urn:py:echo/%7Bmessage%7D'
    """
    return f"{MATCH_PREFIX}{_verb_segment(verb)}:{_encode_segment(pattern)}"


def parse_match_iri(iri: str) -> tuple[Verb, str] | None:
    """A match IRI's verb and door pattern; ``None`` if ``iri`` is not one."""
    if not iri.startswith(MATCH_PREFIX):
        return None
    verb, sep, pattern = iri[len(MATCH_PREFIX) :].partition(":")
    parsed_verb = _parse_verb_segment(verb) if sep else None
    decoded = _decode_segment(pattern) if parsed_verb is not None else None
    return None if decoded is None else (parsed_verb, decoded)


def canonical_match_iri(iri: str) -> str | None:
    """The canonical spelling of a match IRI, ``match_iri(*parse_match_iri(iri))``, or
    ``None`` if ``iri`` is not one (core's ``canonical_match_iri``).

    Lenient in, canonical out (ledger #1107): the parse accepts every spelling that
    decodes to the same door (lower-case hex, an unnecessary escape, a raw brace), and
    an IRI taken from a caller is said back in this spelling, never verbatim.

    >>> canonical_match_iri("urn:ikigai:match:source:urn:%66ile:%7bpath}")
    'urn:ikigai:match:source:urn:file:%7Bpath%7D'
    >>> canonical_match_iri("urn:ikigai:match:source:%+1") is None
    True
    """
    parsed = parse_match_iri(iri)
    return None if parsed is None else match_iri(*parsed)


# -- the canonical form ------------------------------------------------------


def _feed_str(h, s: str) -> None:
    b = s.encode("utf-8")
    h.update(struct.pack("<Q", len(b)))
    h.update(b)


def _feed_u64(h, v: int) -> None:
    h.update(struct.pack("<Q", v))


def _feed_u8(h, v: int) -> None:
    h.update(bytes([v]))


def _feed_option(h, value: str | None) -> None:
    if value is None:
        _feed_u8(h, 0)
    else:
        _feed_u8(h, 1)
        _feed_str(h, value)


def _sorted_set(values) -> list[str]:
    # Rust sorts Strings by UTF-8 bytes; Python sorts str by code point. UTF-8
    # preserves code-point order, so the two agree.
    return sorted(set(values))


def _feed_set(h, values) -> None:
    items = _sorted_set(values)
    _feed_u64(h, len(items))
    for v in items:
        _feed_str(h, v)


def _input_key(arg: dict) -> tuple:
    # Core's `input_key`, field for field: name, summary, required, is-binding, class,
    # default, one_of (already canonical). An Option orders None before Some.
    def opt(value):
        return (0, "") if value is None else (1, value)

    return (
        arg["name"],
        arg.get("summary", ""),
        bool(arg.get("required", True)),
        arg.get("source", "argument") == "binding",
        opt(arg.get("class")),
        opt(arg.get("default")),
        tuple(_sorted_set(arg.get("one_of") or ())),
    )


def contract_id(endpoint_id: str, action: dict) -> str:
    """One verb's contract, content-addressed: ``b3:{hex}`` (core's
    ``ActionSpec::contract_id``).

    ``action`` is the serde shape of ``ikigai_core::ActionSpec`` (``verb`` as its
    wire name — ``"Source"`` — plus ``summary``, ``inputs``, ``outputs`` and
    ``requires``, each optional). The canonical form: the domain tag, the endpoint id,
    the verb's lower-case name, the summary; the inputs sorted (by name, then every
    other field) and deduplicated, each as name, summary, required, source, class,
    default and its sorted, deduplicated ``one_of``; the outputs and the required
    scopes, each sorted and deduplicated. Every variable-length field is
    length-prefixed (u64, little-endian). So declaration ORDER never changes the
    identity, and two contracts that would render differently never share one.
    """
    verb = Verb[action["verb"].upper()]
    h = blake3.blake3()
    _feed_str(h, _CONTRACT_FORM)
    _feed_str(h, endpoint_id)
    _feed_str(h, _verb_segment(verb))
    _feed_str(h, action.get("summary", ""))
    keys: list[tuple] = []
    for key in sorted(_input_key(arg) for arg in action.get("inputs") or ()):
        if not keys or keys[-1] != key:
            keys.append(key)
    _feed_u64(h, len(keys))
    for name, summary, required, binding, cls, default, one_of in keys:
        _feed_str(h, name)
        _feed_str(h, summary)
        _feed_u8(h, 1 if required else 0)
        _feed_u8(h, 1 if binding else 0)
        _feed_option(h, cls[1] if cls[0] else None)
        _feed_option(h, default[1] if default[0] else None)
        _feed_set(h, one_of)
    _feed_set(h, action.get("outputs") or ())
    _feed_set(h, action.get("requires") or ())
    return "b3:" + h.hexdigest()


def contract_iri(endpoint_id: str, action: dict) -> str:
    """One verb's contract IRI, ``urn:ikigai:contract:{id}:{verb}:b3:{hex}`` (core's
    ``ActionSpec::contract_iri``): the ``ik:Action`` node of a Meta turtle face and a
    catalog, and what a manifold row's ``ik:contract`` points at."""
    return _contract_iri_of(
        endpoint_id, Verb[action["verb"].upper()], contract_id(endpoint_id, action)
    )


def _contract_iri_of(endpoint_id: str, verb: Verb, digest: str) -> str:
    # A contract IRI from its parts, in the one canonical spelling (core's
    # `contract_iri_of`): the id encoded, the digest as given (``b3:`` + lower hex).
    return f"{CONTRACT_PREFIX}{_encode_segment(endpoint_id)}:{_verb_segment(verb)}:{digest}"


def parse_contract_iri(iri: str) -> tuple[str, Verb, str] | None:
    """A contract IRI's endpoint id, verb and ``b3:{hex}`` digest; ``None`` if ``iri``
    is not one. Parsed from the right, because an id may carry colons of its own."""
    if not iri.startswith(CONTRACT_PREFIX):
        return None
    head, sep, hex_digest = iri[len(CONTRACT_PREFIX) :].rpartition(":b3:")
    # ContentId::parse: 32 bytes of hex in either case, rendered back lower-case.
    if (
        not sep
        or len(hex_digest) != 64
        or any(c not in "0123456789abcdefABCDEF" for c in hex_digest)
    ):
        return None
    endpoint_id, sep, verb = head.rpartition(":")
    parsed_verb = _parse_verb_segment(verb) if sep else None
    decoded = _decode_segment(endpoint_id) if parsed_verb is not None else None
    return None if decoded is None else (decoded, parsed_verb, "b3:" + hex_digest.lower())


def canonical_contract_iri(iri: str) -> str | None:
    """The canonical spelling of a contract IRI, its parsed id, verb and digest written
    back as :func:`contract_iri` writes them (id encoded, digest in lower-case hex), or
    ``None`` if ``iri`` is not one (core's ``canonical_contract_iri``; see
    :func:`canonical_match_iri` for the rule).

    >>> iri = canonical_contract_iri("urn:ikigai:contract:%66ile:source:b3:" + "AB" * 32)
    >>> iri == "urn:ikigai:contract:file:source:b3:" + "ab" * 32
    True
    """
    parsed = parse_contract_iri(iri)
    return None if parsed is None else _contract_iri_of(*parsed)
