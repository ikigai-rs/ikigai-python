"""The pre-flight check: an action's contract applied to a proposed set of named
arguments, mirroring ``ikigai_core::ActionSpec::check`` (ledger #1547).

Core has ONE checker; this face cannot call it, so it mirrors it rule for rule and
``tests/test_check.py`` holds it to a VERBATIM copy of core's
``tests/vectors/validation_vectors.json`` (every row's kinds and paths, in order).

The contract is taken in the serde (JSON) shape of ``ikigai_core::ActionSpec``, as
:mod:`ikigai.contract` takes it; the proposed arguments are ``(name, value)`` pairs in the
order given, repeats kept. The check is a PURE function of the two: authority is not a
property of the arguments, so the capability floor stays in :mod:`ikigai.serve`.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from .contract import group_segment

#: The detail of every refusal of a name given more than once that is not a list argument
#: (core's ``GIVEN_TWICE``).
GIVEN_TWICE = "given twice; an argument takes one value, so name it once"

_XSD = "http://www.w3.org/2001/XMLSchema#"

# Rust's `char::is_whitespace` / `str::trim` (Unicode White_Space), which is NOT Python's
# `str.isspace` (that also holds U+001C to U+001F).
_WHITESPACE = "\t\n\x0b\x0c\r \x85\xa0                　"

# The canonical character a list's repeats are joined with, by separator word.
_CANONICAL = {"comma": ",", "whitespace": " ", "comma-or-whitespace": ",", "newline": "\n"}

#: The words a list argument's separator is spelled with.
LIST_SEPARATORS = frozenset(_CANONICAL)


@dataclass(frozen=True)
class Violation:
    """One rule a proposed call breaks. ``kind`` is the shared vectors' word (``missing``,
    ``not-one-of``, ``implausible``, ``unknown``, ``exactly-one``, ``at-least-one``,
    ``at-most-one``, ``needs``, ``given-twice``, ``envelope``); ``path`` is ``input:{name}``,
    a group's ``group:{kind}:{members}``, ``input:{name}:value:{value}``, or ``None`` for a
    name the contract does not declare."""

    kind: str
    path: str | None
    message: str


def split_list(separator: str, value: str) -> list[str]:
    """``ListSeparator::split``: the items of ``value``, empty items dropped."""
    if separator == "comma":
        items = [i.strip(_WHITESPACE) for i in value.split(",")]
    elif separator == "whitespace":
        items = _split_whitespace(value)
    elif separator == "comma-or-whitespace":
        items = _split_whitespace(value.replace(",", " "))
    elif separator == "newline":
        items = [i.strip(_WHITESPACE) for i in value.split("\n")]
    else:
        raise ValueError(f"unknown list separator {separator!r}")
    return [i for i in items if i]


def _split_whitespace(value: str) -> list[str]:
    out: list[str] = []
    current: list[str] = []
    for c in value:
        if c in _WHITESPACE:
            if current:
                out.append("".join(current))
                current = []
        else:
            current.append(c)
    if current:
        out.append("".join(current))
    return out


def _given(value: str | None) -> bool:
    # A group member (or a dependency) is GIVEN only with a value that is not blank.
    return value is not None and bool(value.strip(_WHITESPACE))


def _quoted(names: Iterable[str]) -> str:
    return ", ".join(f"`{n}`" for n in names)


def _plausible(cls: str, value: str) -> bool:
    if cls == _XSD + "boolean":
        return value in ("true", "false")
    if cls == _XSD + "integer":
        # Rust's `i64::from_str`: an optional sign, ASCII digits, in range.
        digits = value[1:] if value[:1] in ("+", "-") else value
        if not (digits.isascii() and digits.isdigit()):
            return False
        return -(2**63) <= int(value) < 2**63
    if cls == _XSD + "dateTime":
        b = value.encode("utf-8")
        return (
            len(b) >= 10
            and b[:4].isdigit()
            and b[4:5] == b"-"
            and b[5:7].isdigit()
            and b[7:8] == b"-"
            and b[8:10].isdigit()
        )
    return True  # an entity class, or a datatype not statically checkable


def _enumerated(arg: dict) -> bool:
    return "one_of" in arg or bool(arg.get("one_of"))


def _check_group(group: dict, proposed: dict[str, str]) -> Violation | None:
    members = list(group.get("members") or ())
    present = [m for m in members if _given(proposed.get(m))]
    kind = group["kind"]
    if kind == "exactly-one":
        broken, rule = len(present) != 1, "exactly one of"
    elif kind == "at-least-one":
        broken, rule = not present, "at least one of"
    elif kind == "at-most-one":
        broken, rule = len(present) > 1, "at most one of"
    else:
        raise ValueError(f"unknown group kind {kind!r}")
    if not broken:
        return None
    got = f"got {_quoted(present)}" if present else "got none"
    return Violation(
        kind,
        group_segment(group),
        f"{rule} {_quoted(members)} is required; {got}",
    )


def _check_value(arg: dict, value: str) -> Violation | None:
    name = arg["name"]
    one_of = arg.get("one_of") or []
    if _enumerated(arg) and value not in one_of:
        accepted = (
            f"one of: {', '.join(one_of)}"
            if one_of
            else "it accepts none: its enumeration is empty"
        )
        return Violation(
            "not-one-of",
            f"input:{name}",
            f"`{value}` is not an accepted value of `{name}` ({accepted})",
        )
    cls = arg.get("class")
    if cls is not None and not _plausible(cls, value):
        return Violation(
            "implausible",
            f"input:{name}",
            f"`{value}` does not look like a {cls} for `{name}`",
        )
    return None


def join_repeats(
    action: dict, pairs: Iterable[tuple[str, str]]
) -> tuple[dict[str, str], list[str]]:
    """One value per name, in first-seen order (core's ``joined``): a list argument's
    repeats joined with its separator's canonical character, any other name's FIRST value
    kept; and the names that were repeated without being a list."""
    pairs = list(pairs)
    values: dict[str, list[str]] = {}
    for name, value in pairs:
        values.setdefault(name, []).append(value)
    lists = {a["name"]: a.get("list") for a in reversed(action.get("inputs") or ())}
    joined: dict[str, str] = {}
    repeated: list[str] = []
    for name, given in values.items():
        separator = lists.get(name)
        if len(given) == 1:
            joined[name] = given[0]
        elif separator is not None:
            joined[name] = _CANONICAL[separator].join(given)
        else:
            repeated.append(name)
            joined[name] = given[0]
    return joined, repeated


def check(action: dict, pairs: Iterable[tuple[str, str]]) -> list[Violation]:
    """Every rule the proposed call ``pairs`` breaks against ``action`` (the serde shape of
    ``ActionSpec``), in core's stable order: each input in declaration order, then the
    groups, then the names the contract does not declare. Empty exactly when the arguments
    conform. See ``ActionSpec::check`` for the rules; each is a row of the shared vectors."""
    pairs = list(pairs)
    inputs = list(action.get("inputs") or ())
    violations: list[Violation] = []
    effective, repeated = join_repeats(action, pairs)
    arguments = action.get("arguments") or {}
    # An envelope's fields are checked when the caller proposes them, and only its carrier
    # otherwise; giving both is giving the body twice.
    carrier = arguments.get("envelope")
    fields_given = carrier is not None and any(
        i["name"] != carrier and _given(effective.get(i["name"])) for i in inputs
    )
    if carrier is not None and fields_given and _given(effective.get(carrier)):
        violations.append(
            Violation(
                "envelope",
                f"input:{carrier}",
                f"the body is given twice: as `{carrier}` and as the fields that belong "
                "inside it; give one",
            )
        )
    for arg in inputs:
        name = arg["name"]
        binding = arg.get("source", "argument") == "binding"
        if carrier is not None:
            skipped = name == carrier if fields_given else (name != carrier and not binding)
            if skipped:
                continue
        if name in repeated:
            violations.append(
                Violation("given-twice", f"input:{name}", f"`{name}` is {GIVEN_TWICE}")
            )
        value = effective.get(name)
        if arg.get("required", True) and not binding and value is None:
            violations.append(
                Violation("missing", f"input:{name}", f"required input `{name}` is missing")
            )
            continue
        if value is None:
            continue
        separator = arg.get("list")
        items = split_list(separator, value) if separator is not None else [value]
        for item in items:
            found = _check_value(arg, item)
            if found is not None:
                violations.append(found)
        # A value's own dependencies (a value note's `needs`), per distinct item.
        seen: list[str] = []
        for item in items:
            if item in seen:
                continue
            seen.append(item)
            note = (arg.get("values") or {}).get(item)
            if note is None:
                continue
            missing = [n for n in note.get("needs") or () if not _given(effective.get(n))]
            if missing:
                violations.append(
                    Violation(
                        "needs",
                        f"input:{name}:value:{item}",
                        f"`{name}={item}` needs {_quoted(missing)}",
                    )
                )
        missing = [n for n in arg.get("needs") or () if not _given(effective.get(n))]
        if _given(value) and missing:
            violations.append(
                Violation("needs", f"input:{name}", f"`{name}` needs {_quoted(missing)}")
            )
    violations.extend(v for g in action.get("groups") or () if (v := _check_group(g, effective)))
    if "open" not in arguments:
        declared = {i["name"] for i in inputs}
        unknown: list[str] = []
        for name, _ in pairs:
            if name not in declared and name not in unknown:
                unknown.append(name)
                violations.append(
                    Violation(
                        "unknown",
                        None,
                        f"unknown argument `{name}` — not in this action's contract",
                    )
                )
    return violations
