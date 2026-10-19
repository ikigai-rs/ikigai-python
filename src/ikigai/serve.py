"""Serve Python functions as ikigai resources over a Unix socket.

The peer-module seed: a Rust host mounts this server
(``ikigai --mount urn:py:=<socket>``) and the functions join its resolution
space — listed in the catalog with their origin, named-arg routed via their
declared ArgSpecs, invoked over the wire::

    from ikigai import endpoint, serve

    @endpoint("urn:py:hello", summary="Greet someone")
    def hello(who: str, greeting: str = "Hello") -> str:
        return f"{greeting}, {who}!"

    serve([hello], "/tmp/py.sock")   # blocks

**The signature is the contract.** Without an ``args=`` list the ArgSpecs
are derived from the function signature: ``who`` above is required
(xsd:string), ``greeting`` optional with default ``"Hello"``. ``int`` /
``float`` / ``bool`` map to their XSD datatypes and incoming wire text is
coerced back to the annotated type before the handler runs;
``typing.Literal`` becomes (enforced) ``one_of``; ``Optional[T]`` /
``T | None`` marks the argument optional; ``Annotated[T, "…"]`` carries the
per-argument summary; a trailing underscore maps a reserved word onto the
wire (``in_`` declares and receives the argument ``in``). Unannotated
parameters are accepted with no class — gradual typing, gradually rewarded.
An explicit ``args=`` list still wins wholesale (no merging), with a loud
error when its names do not match the signature.

**Families: templated, multi-verb doors.** An endpoint may be declared over
a URI template and answer every IRI it matches, and one name may answer
Source, Sink, Delete and Exists with a contract per verb::

    from ikigai import family

    cell = family("urn:iki:tutorial:ttt:stored:{x}:{y}", id="ttt-stored")

    @cell.source(cacheable=True)
    def read(x: int, y: int) -> str: ...

    @cell.sink
    def play(x: int, y: int, content: str) -> str: ...

Template matching mirrors ``ikigai_core::UriTemplate`` exactly (see
:mod:`ikigai.template`); the first declared door that matches wins, as in an
``EndpointSpace``; the catalog lists the template; each variable is an
``ik:source "binding"`` input the handler receives by name. See
:class:`Family` for the binding, Exists and cacheability rules.
``@endpoint`` takes a template too, for a single-verb Source family.

**Alias mounts strip the prefix.** ``--mount urn:py:=<socket>`` rewrites
``urn:py:hello`` to ``urn:hello`` before forwarding, and re-prefixes catalog
patterns coming back. This server therefore answers BOTH the declared IRI
(``urn:py:hello`` — for ``--override`` mounts and direct ``--connect``
clients) and its alias-stripped form (``urn:hello``) — for a template,
``urn:py:echo/{m}`` answers as ``urn:echo/{m}`` too. The stripped form assumes
the mount prefix is the FIRST segment (``urn:py:``); a deeper prefix
(``--mount urn:py:echo:=…``) strips more than this server can know about, so
mount a family with ``--override <its prefix>=<socket>``, which forwards
IRIs unchanged. Every connection's
hello declares its mount mode (the hello is required since wire v7), and
``entries`` answers in that mode's form per connection; the ``strip_alias``
constructor default now only governs direct ``Space.entries()`` calls.

**Failures cross typed** (wire v7): unknown IRI → ``Unresolved``, missing
required argument → ``MissingArgument``, an unusable value →
``InvalidArgument``, a handler exception → ``Endpoint``. A handler may also
RAISE the taxonomy deliberately (``NotFoundError``, ``DeniedError``,
``TimeoutError``, ``UnavailableError``, ``ConflictError`` from
:mod:`ikigai.wire`) and the variant crosses intact — the far side's HTTP face
can answer 404/403/409/… instead of a blanket 502. ``ConflictError`` is wire
v8: a v7 peer (the server accepts one) receives it as
``Endpoint("conflict: …")``, exactly what it saw before v8.

**Security posture**: the socket is ``0600`` in a ``0700`` directory and
peers are refused unless their kernel-verified UID matches the server's —
the same transport trust as the Rust IPC server.

**Declared = enforced** (ledger #1580): every call runs the door's own
declared-capability floor before a handler is entered, the same rule a Rust
kernel applies, so a host that mounts this peer and memoizes its contract can
rely on the peer refusing what the contract no longer admits.

- The capability a call carries is the one checked: ``IssueAs`` and
  ``IssueTraced`` resolve under theirs. A plain ``Issue`` resolves under ROOT,
  exactly as the Rust IPC door does (its ``Issue`` is ``issue_as(root)``): the
  socket admits only the owner, whose entitlement is root, so the carried
  capability needs no clamp.
- **Wire v9** (``IssueWith``): a call may carry the run's retry pool and the
  TIME LEFT in whole milliseconds. The server builds its :class:`RunContext` at
  its OWN now (``deadline = monotonic() + time_left``; an instant never
  crosses) and a served handler reads it with :func:`current_run`. A peer that
  sends none gets ``current_run().retries is None`` and no deadline.
- What a verb requires is core's table (:func:`required_scopes`): a declared
  verb, its own ``requires`` (an explicit per-verb ``requires`` REPLACES the
  flat one); an undeclared ``Exists``, what ``Source`` requires; any other
  undeclared verb, everything declared; ``Meta``, nothing.
- Coverage is core's predicate (:mod:`ikigai.capability`): a plain scope held
  exactly, an exclusion never granted, a trailing ``*`` satisfied by any held
  scope under its prefix.
- A refusal crosses as ``Denied`` with core's text, word for word
  (``capability does not grant `urn:cap:x` (declared by `urn:py:x`)``), and the
  handler never runs. An unbound name is still ``Unresolved`` whatever the
  caller holds.
"""

from __future__ import annotations

import contextvars
import dataclasses
import inspect
import json
import socket
import struct
import sys
import threading
import time
import types
import typing
from pathlib import Path

from . import check as preflight
from . import contract, wire
from .capability import denial_message
from .capability import satisfies as capability_satisfies
from .template import TemplateError, UriTemplate
from .wire import (
    Cached,
    CacheStatus,
    Capability,
    DeniedError,
    EndpointError,
    EntriesCall,
    EntriesReply,
    ErrorTypedReply,
    Expiry,
    Inline,
    InvalidArgumentError,
    IsCached,
    Issue,
    IssueAs,
    IssueTraced,
    IssueWithCall,
    MissingArgumentError,
    NotFoundError,
    Reply,
    Representation,
    Request,
    Resolved,
    ResolvedTraced,
    SpaceEntry,
    TraceEvent,
    UnresolvedError,
    Verb,
)

VOCAB_NS = "https://ikigai-rs.dev/ns#"
TEXT_PLAIN = "text/plain;charset=utf-8"


class ArgSpec:
    """One named input, mirroring ``ikigai_core::ArgSpec``. The describe face
    built from these is what the host engine routes named arguments by — the
    names and required/optional flags are load-bearing, not decoration."""

    def __init__(
        self,
        name: str,
        *,
        summary: str = "",
        required: bool = True,
        cls: str | None = None,
        default: str | None = None,
        one_of: list[str] | None = None,
        source: str = "argument",
        needs: list[str] | None = None,
        list: str | None = None,
        values: dict[str, dict] | None = None,
        examples: list[str] | None = None,
    ):
        """``name`` is the wire argument name. ``default`` forces ``required`` False (as
        in Rust); ``cls`` is an XSD class IRI; ``one_of`` an enforced value set;
        ``source`` is ``"argument"`` or ``"binding"``. ``one_of=[]`` is an ENUMERATION
        THAT ACCEPTS NO VALUE (``one_of=None`` is open-valued). ``needs`` names inputs
        that must be given when this one is; ``list`` is the separator word (``comma``,
        ``whitespace``, ``comma-or-whitespace``, ``newline``) of a list-valued argument,
        which may be given more than once; ``values`` describes particular ``one_of``
        values (``{"retract": {"summary": …, "tags": […], "needs": […]}}``);
        ``examples`` are values the contract accepts. The ``py_*`` attributes are
        routing state, set later, never serialized."""
        if source not in ("argument", "binding"):
            raise ValueError(f"ArgSpec source must be `argument` or `binding` (got {source!r})")
        self.name = name
        self.summary = summary
        # A declared default implies the argument is optional (as in Rust).
        self.required = required if default is None else False
        self.cls = cls
        self.default = default
        if list is not None and list not in preflight.LIST_SEPARATORS:
            raise ValueError(
                f"ArgSpec list must be one of {', '.join(sorted(preflight.LIST_SEPARATORS))}"
            )
        self.one_of = [*(one_of or ())]
        #: Whether an enumeration was declared at all, so an EMPTY one accepts no value.
        self.enumerated = one_of is not None
        self.needs = [*(needs or ())]
        self.list = list
        self.values = {value: dict(note) for value, note in (values or {}).items()}
        self.examples = [*(examples or ())]
        #: ``"argument"`` (a by-value input, the default) or ``"binding"`` (a
        #: variable captured from the IRI by the endpoint's template).
        self.source = source
        # Invocation routing (never serialized): which Python parameter this
        # spec delivers to, whether that parameter has its own (typed)
        # default, and — for signature-derived specs — the annotated type
        # incoming wire text is coerced back to.
        self.py_name = name
        self.py_has_default = False
        #: False only for an input the host must SEND but the handler never
        #: asked for (a Sink's auto-declared ``content``): validated, not passed.
        self.py_deliver = True
        self.py_type: type | None = None

    @classmethod
    def of(cls, spec) -> ArgSpec:
        """An :class:`ArgSpec` from ``spec``: itself, a bare name string, or a dict in
        the serde shape. ``TypeError`` for anything else."""
        if isinstance(spec, ArgSpec):
            return spec
        if isinstance(spec, str):
            return cls(spec)
        if isinstance(spec, dict):
            return cls(
                spec["name"],
                summary=spec.get("summary", ""),
                required=spec.get("required", True),
                cls=spec.get("class"),
                default=spec.get("default"),
                one_of=spec.get("one_of"),
                source=spec.get("source", "argument"),
                needs=spec.get("needs"),
                list=spec.get("list"),
                values=spec.get("values"),
                examples=spec.get("examples"),
            )
        raise TypeError(f"not an ArgSpec: {spec!r}")

    def to_json(self) -> dict:
        """The serde shape of ``ikigai_core::ArgSpec`` (fields with
        ``skip_serializing_if`` omitted when unset, like the Rust side)."""
        out = {
            "name": self.name,
            "summary": self.summary,
            "required": self.required,
            "source": self.source,
        }
        if self.cls is not None:
            out["class"] = self.cls
        if self.default is not None:
            out["default"] = self.default
        if self.one_of or self.enumerated:
            out["one_of"] = self.one_of
        if self.examples:
            out["examples"] = self.examples
        if self.needs:
            out["needs"] = self.needs
        if self.list is not None:
            out["list"] = self.list
        if self.values:
            out["values"] = self.values
        return out


class InputGroup:
    """How a set of inputs relate, mirroring ``ikigai_core::InputGroup``: exactly one,
    at least one or at most one of ``members`` is GIVEN (a member counts only with a
    non-blank value). ``pipe`` names the member a pipe, a positional value or a batch's
    stdin fills when none is named (``content``, or absent, on a Sink or Delete)."""

    def __init__(self, kind: str, members, pipe: str | None = None):
        """``kind`` is ``exactly-one``, ``at-least-one`` or ``at-most-one``; ``members`` are
        input names (kept as a sorted set)."""
        if kind not in ("exactly-one", "at-least-one", "at-most-one"):
            raise ValueError(
                f"InputGroup kind must be exactly-one, at-least-one or at-most-one: {kind!r}"
            )
        self.kind = kind
        self.members = sorted(set(members))  # a set, as core's builders keep it
        self.pipe = pipe

    @classmethod
    def exactly_one(cls, *members: str, pipe: str | None = None) -> InputGroup:
        """Exactly one of ``members`` is given."""
        return cls("exactly-one", members, pipe)

    @classmethod
    def at_least_one(cls, *members: str, pipe: str | None = None) -> InputGroup:
        """At least one of ``members`` is given."""
        return cls("at-least-one", members, pipe)

    @classmethod
    def at_most_one(cls, *members: str, pipe: str | None = None) -> InputGroup:
        """At most one of ``members`` is given."""
        return cls("at-most-one", members, pipe)

    @classmethod
    def of(cls, spec) -> InputGroup:
        """An :class:`InputGroup` from ``spec``: itself or a dict in the serde shape."""
        if isinstance(spec, InputGroup):
            return spec
        if isinstance(spec, dict):
            return cls(spec["kind"], spec.get("members", ()), spec.get("pipe"))
        raise TypeError(f"not an InputGroup: {spec!r}")

    def to_json(self) -> dict:
        """The serde shape of ``ikigai_core::InputGroup`` (``pipe`` omitted when absent)."""
        out: dict = {"kind": self.kind, "members": self.members}
        if self.pipe is not None:
            out["pipe"] = self.pipe
        return out


def _arguments_json(arguments) -> dict | None:
    """The serde shape of ``Arguments`` (``{"open": summary}`` / ``{"envelope": carrier}``),
    ``None`` for the default closed set."""
    if not arguments:
        return None
    if not (
        isinstance(arguments, dict)
        and len(arguments) == 1
        and isinstance(next(iter(arguments.values())), str)
        and next(iter(arguments)) in ("open", "envelope")
    ):
        raise ValueError(
            f'arguments must be {{"open": summary}} or {{"envelope": carrier}}: {arguments!r}'
        )
    return dict(arguments)


def _output_from_json(output_from):
    """The serde shape of ``OutputFrom``: ``"origin"`` or ``{"input": name}``, or ``None``."""
    if output_from is None or output_from == "origin":
        return output_from
    if isinstance(output_from, str):
        return {"input": output_from}  # a bare name is the input the output comes from
    if isinstance(output_from, dict) and set(output_from) == {"input"}:
        return dict(output_from)
    raise ValueError(
        f'output_from must be "origin", an input name or {{"input": name}}: {output_from!r}'
    )


# ---------------------------------------------------------------------------
# Deriving ArgSpecs from function signatures
# ---------------------------------------------------------------------------

_XSD = {
    str: "http://www.w3.org/2001/XMLSchema#string",
    int: "http://www.w3.org/2001/XMLSchema#integer",
    float: "http://www.w3.org/2001/XMLSchema#double",
    bool: "http://www.w3.org/2001/XMLSchema#boolean",
}


def _render_value(value) -> str:
    """A default or Literal member as wire text (bools as the REPL grammar's
    ``true``/``false``; everything else via ``str``)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _wire_name(name: str) -> str:
    """``in_`` → ``in``: PEP 8's own trailing-underscore convention maps a
    reserved-word parameter onto its wire name."""
    if name.endswith("_") and not name.endswith("__") and len(name) > 1:
        return name[:-1]
    return name


def _annotation_facts(annotation) -> tuple[str, type | None, list[str], bool]:
    """Unwind one annotation to ``(summary, base type, one_of, optional)``.

    ``base type`` is a key of ``_XSD`` or ``bytes`` when the annotation names
    one scalar, else ``None``. Unknown shapes come back as (.., None, [], ..)
    — accepted with no class, never an error (gradual typing)."""
    summary = ""
    optional = False
    while True:
        origin = typing.get_origin(annotation)
        if origin is typing.Annotated:
            metadata = typing.get_args(annotation)
            for item in metadata[1:]:
                if isinstance(item, str) and not summary:
                    summary = item
            annotation = metadata[0]
            continue
        if origin is typing.Union or origin is types.UnionType:
            members = typing.get_args(annotation)
            rest = [m for m in members if m is not type(None)]
            if len(rest) < len(members):
                optional = True  # Optional[T] / T | None
            if len(rest) == 1:
                annotation = rest[0]
                continue
            return summary, None, [], optional  # a many-typed union: no one class
        break
    if typing.get_origin(annotation) is typing.Literal:
        members = typing.get_args(annotation)
        member_types = {type(m) for m in members}
        base = member_types.pop() if len(member_types) == 1 else None
        if base is not None and base not in _XSD and base is not bytes:
            base = None
        return summary, base, [_render_value(m) for m in members], optional
    if annotation in _XSD or annotation is bytes:
        return summary, annotation, [], optional
    return summary, None, [], optional


def derive_args(fn) -> list[ArgSpec]:
    """Derive ArgSpecs from ``fn``'s signature — the ``@endpoint`` behavior
    when no ``args=`` list is given. The signature is the contract: names,
    required/optional, XSD classes from annotations, defaults, Literal →
    ``one_of``, ``Annotated`` summaries, ``in_`` → ``in``."""
    try:
        signature = inspect.signature(fn)
    except (TypeError, ValueError):  # not introspectable (C callable, …)
        return []
    try:
        hints = typing.get_type_hints(fn, include_extras=True)
    except Exception:  # unresolvable annotations: accepted untyped, never an error
        hints = {}
    specs: list[ArgSpec] = []
    for name, param in signature.parameters.items():
        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue  # *args/**kwargs declare nothing by themselves
        if param.kind is inspect.Parameter.POSITIONAL_ONLY:
            raise TypeError(
                f"{fn.__name__}(): positional-only parameter `{name}` cannot be routed "
                "by name; make it keyword-addressable or declare args= explicitly"
            )
        summary, base, one_of, optional = _annotation_facts(hints.get(name, param.empty))
        has_default = param.default is not inspect.Parameter.empty
        default = None
        if has_default and param.default is not None:
            default = _render_value(param.default)
        spec = ArgSpec(
            _wire_name(name),
            summary=summary,
            required=not (has_default or optional),
            cls=_XSD.get(base),
            default=default,
            one_of=one_of or None,
        )
        spec.py_type = base
        specs.append(spec)
    return specs


def _check_explicit_args(handler, where: str, specs: list[ArgSpec], bound=()) -> None:
    """An explicit ``args=`` wins over the signature wholesale, but a NAME
    mismatch between the two is a bug in the declaration — fail loud at
    decoration time, not at first invocation. ``bound`` names the template
    variables, which reach the handler as bindings rather than args."""
    try:
        params = inspect.signature(handler).parameters
    except (TypeError, ValueError):
        return
    has_var_kw = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
    named = {
        n: p
        for n, p in params.items()
        if p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    }
    for spec in specs:
        if spec.name in named or spec.name + "_" in named or has_var_kw:
            continue
        raise TypeError(
            f"{where}: declared arg `{spec.name}` matches no parameter of "
            f"{handler.__name__}() (parameters: {', '.join(named) or 'none'}) — "
            "explicit args= wins, so fix the declaration or the signature"
        )
    declared = {s.name for s in specs} | set(bound)
    for name, param in named.items():
        if param.default is not inspect.Parameter.empty:
            continue  # the Python default covers it
        if name in declared or _wire_name(name) in declared:
            continue
        raise TypeError(
            f"{where}: required parameter `{name}` of {handler.__name__}() is not "
            "declared in args= — the endpoint could never invoke it"
        )


def _map_parameters(handler, specs: list[ArgSpec]) -> None:
    """Bind each spec to the Python parameter it delivers to (``in`` → ``in_``
    when the reserved-word convention is in play; specs a ``**kwargs`` absorbs
    keep their wire name)."""
    try:
        params = inspect.signature(handler).parameters
    except (TypeError, ValueError):
        return
    for spec in specs:
        for candidate in (spec.name, spec.name + "_"):
            param = params.get(candidate)
            if param is not None:
                spec.py_name = candidate
                spec.py_has_default = param.default is not inspect.Parameter.empty
                break


def _binding_specs(handler, where: str, template: UriTemplate) -> list[ArgSpec]:
    """One ``binding`` ArgSpec per template variable, in template order.

    Bindings are ALWAYS read from the signature, even beside an explicit
    ``args=`` list: the parameter named like the variable supplies its class,
    summary and coercion (``x: int`` declares ``xsd:integer`` and receives an
    ``int``). A variable no parameter can receive — and no ``**kwargs``
    absorbs — is a declaration error, not a silently dropped value."""
    if template.is_exact:
        return []
    by_name = {spec.name: spec for spec in derive_args(handler)}
    try:
        params = inspect.signature(handler).parameters.values()
        has_var_kw = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params)
    except (TypeError, ValueError):
        has_var_kw = True  # not introspectable: trust it, as derive_args does
    specs = []
    # `{a}:{a}` is legal (core's Bindings is a map; the last capture wins) and
    # is ONE input, as core has one binding.
    for var in dict.fromkeys(template.variables):
        spec = by_name.get(var)
        if spec is None:
            if not has_var_kw:
                raise TypeError(
                    f"{where}: template variable `{var}` matches no parameter of "
                    f"{handler.__name__}() — the handler could never receive it"
                )
            spec = ArgSpec(var)
        # A binding is part of the name: always present, never defaulted.
        spec.required = True
        spec.default = None
        spec.source = "binding"
        specs.append(spec)
    return specs


def _can_receive(handler, name: str) -> bool:
    """Whether ``handler`` takes a keyword ``name`` (or ``name_``, or ``**kwargs``)."""
    try:
        params = inspect.signature(handler).parameters
    except (TypeError, ValueError):
        return True
    return (
        name in params
        or name + "_" in params
        or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
    )


def _is_mutating(verb: Verb) -> bool:
    return verb in (Verb.SINK, Verb.DELETE)


class Action:
    """One verb's contract on one endpoint — ``ikigai_core::ActionSpec`` —
    plus the handler that answers it. Inputs are the template's bindings
    first (in template order), then the by-value arguments."""

    def __init__(
        self,
        verb: Verb,
        handler,
        template: UriTemplate,
        where: str,
        *,
        summary: str = "",
        args: list | None = None,
        output: str = TEXT_PLAIN,
        cacheable: bool = False,
        requires: list[str] | None = None,
        groups: list | None = None,
        arguments: dict | None = None,
        output_from=None,
    ):
        """``verb`` and ``handler`` are the action; ``template`` supplies the bindings
        (each variable is an input, first in order). ``cacheable`` is refused for a
        mutating verb; ``args=None`` derives the contract from the signature.
        ``groups`` (:class:`InputGroup`), ``arguments`` (``{"open": summary}`` or
        ``{"envelope": carrier}``) and ``output_from`` (``"origin"`` or an input name)
        declare the rest of the contract shape."""
        if cacheable and _is_mutating(verb):
            raise ValueError(f"{where}: a {verb.wire_name} answer is never cacheable")
        self.verb = verb
        self.handler = handler
        self.summary = summary
        variables = set(template.variables)
        bindings = _binding_specs(handler, where, template)
        # No args= list → the signature IS the contract. An explicit list
        # wins wholesale (no merging) after a loud name-mismatch check.
        self.derived = args is None
        if self.derived:
            rest = [s for s in derive_args(handler) if s.name not in variables]
        else:
            rest = [ArgSpec.of(a) for a in args]
            for spec in rest:
                if spec.name in variables:
                    raise TypeError(
                        f"{where}: declared arg `{spec.name}` is also a variable of the "
                        f"template {template.source} — a binding and an argument cannot "
                        "share a name"
                    )
                if spec.source == "binding":
                    raise TypeError(
                        f"{where}: declares `{spec.name}` as a binding, but bindings come "
                        "from the template's variables (and their parameters), not from args="
                    )
            _check_explicit_args(handler, where, rest, bound=variables)
        auto_content = None
        if verb == Verb.SINK and not any(spec.name == "content" for spec in rest):
            # The pipeline rule: the host engine routes every piped or trailing
            # value to `content`, so a Sink declares it whether or not its
            # handler reads it. Required, like the Deno face's.
            auto_content = ArgSpec("content", summary="the body to write")
            rest.append(auto_content)
        self.args = bindings + rest
        _map_parameters(handler, self.args)
        if auto_content is not None and not _can_receive(handler, "content"):
            auto_content.py_deliver = False
        self.output = output
        self.cacheable = cacheable
        self.requires = list(requires or [])
        self.groups = [InputGroup.of(g) for g in groups or ()]
        self.arguments = _arguments_json(arguments)
        self.output_from = _output_from_json(output_from)
        names = {spec.name for spec in self.args}
        for ref in self._references():
            if ref not in names:
                raise ValueError(f"{where}: the contract names `{ref}`, which is not an input")

    def _references(self):
        """Every input name the contract's shape refers to."""
        for spec in self.args:
            yield from spec.needs
            for value, note in spec.values.items():
                if spec.one_of and value not in spec.one_of:
                    raise ValueError(
                        f"input `{spec.name}` describes `{value}`, not one of its one_of"
                    )
                yield from note.get("needs", ())
        for group in self.groups:
            yield from group.members
            if group.pipe is not None:
                yield group.pipe
        if self.arguments and "envelope" in self.arguments:
            yield self.arguments["envelope"]
        if isinstance(self.output_from, dict):
            yield self.output_from["input"]

    def spec_json(self) -> dict:
        """The serde shape of ``ikigai_core::ActionSpec`` (empty fields
        omitted, as the Rust side's ``skip_serializing_if`` does)."""
        out: dict = {"verb": self.verb.wire_name}
        if self.summary:
            out["summary"] = self.summary
        if self.args:
            out["inputs"] = [a.to_json() for a in self.args]
        out["outputs"] = [self.output]
        if self.requires:
            out["requires"] = self.requires
        if self.groups:
            out["groups"] = [g.to_json() for g in self.groups]
        if self.arguments:
            out["arguments"] = self.arguments
        if self.output_from is not None:
            out["output_from"] = self.output_from
        return out


# The order verbs are listed in, everywhere a door lists them.
_VERB_ORDER = (Verb.SOURCE, Verb.SINK, Verb.EXISTS, Verb.DELETE)


class _Door:
    """What the served space binds: a pattern (an exact IRI, or a template
    naming a family), an identity, and one :class:`Action` per verb."""

    #: The flat authoring form (``@endpoint``): one Source action whose
    #: contract IS the description's top-level fields. A :class:`Family` is
    #: not flat — every verb is an explicit action.
    flat = False

    def __init__(self, pattern: str, id: str, title: str, summary: str):
        """``pattern`` must be a ``urn:`` (a template for a family). ``id`` names the
        door (used in catalog rows and contract IRIs); ``actions`` starts empty."""
        if not pattern.startswith("urn:"):
            raise ValueError(f"endpoint IRI must be a urn: ({pattern!r})")
        self.template = UriTemplate(pattern)
        self.iri = pattern
        self.id = id
        self.title = title
        self.summary = summary
        self.actions: dict[Verb, Action] = {}

    @property
    def alias_template(self) -> UriTemplate | None:
        """The alias-stripped form an alias mount forwards: ``urn:py:hello``
        arrives as ``urn:hello`` after ``--mount urn:py:=…`` strips its
        prefix, and ``urn:py:echo/{m}`` as ``urn:echo/{m}``. ``None`` when
        there is no plain first segment to strip."""
        parts = self.iri.split(":", 2)
        if len(parts) != 3 or "{" in parts[1]:
            return None
        try:
            return UriTemplate(f"urn:{parts[2]}")
        except TemplateError:
            return None

    @property
    def alias_iri(self) -> str | None:
        """:attr:`alias_template` as text (kept for callers of the exact-IRI era)."""
        alias = self.alias_template
        return None if alias is None else alias.source

    @property
    def verbs(self) -> list[Verb]:
        """The declared verbs, Meta last (every door answers Meta)."""
        return [v for v in _VERB_ORDER if v in self.actions] + [Verb.META]

    def answered_verbs(self) -> list[Verb]:
        """What a request may actually use: the declared verbs, plus Exists
        wherever it has a default (see :meth:`Space._exists`) — every flat
        door, and a family that declares Source."""
        has_default_exists = self.flat or Verb.SOURCE in self.actions
        verbs = [
            v for v in _VERB_ORDER if v in self.actions or (v is Verb.EXISTS and has_default_exists)
        ]
        return verbs + [Verb.META]

    # The flat form's top-level fields. A family states everything per
    # action, so its flat fields are empty — as ``ttt-stored``'s are in Rust.

    def _flat_inputs(self) -> list[ArgSpec]:
        return self.actions[Verb.SOURCE].args if self.flat else []

    def _flat_outputs(self) -> list[str]:
        return [self.actions[Verb.SOURCE].output] if self.flat else []

    def _flat_requires(self) -> list[str]:
        return self.actions[Verb.SOURCE].requires if self.flat else []

    # -- the Meta faces ----------------------------------------------------

    def description_json(self) -> dict:
        """The serde shape of ``ikigai_core::Description`` — the face the
        host's engine parses to route named arguments over a mount."""
        out = {
            "id": self.id,
            "title": self.title,
            "summary": self.summary,
            "verbs": [v.wire_name for v in self.verbs],
            "inputs": [a.to_json() for a in self._flat_inputs()],
            "outputs": self._flat_outputs(),
        }
        if self._flat_requires():
            out["requires"] = self._flat_requires()
        if self.flat:
            # The flat form's shape fields are the Source action's, read through.
            source = self.actions[Verb.SOURCE].spec_json()
            out.update(
                {k: source[k] for k in ("groups", "arguments", "output_from") if k in source}
            )
        if not self.flat:
            out["actions"] = [self.actions[v].spec_json() for v in self.verbs if v in self.actions]
        return out

    def description_text(self) -> str:
        """The human face (mirrors ``ikigai_vocab::to_text``, which lists
        only the flat inputs — so a family's text face names its verbs and
        not their contracts, exactly as a Rust family's does)."""
        s = f"{self.id} — {self.title}\n"
        if self.summary:
            s += f"{self.summary}\n"
        s += "verbs: " + ", ".join(v.wire_name for v in self.verbs) + "\n"
        for arg in self._flat_inputs():
            opt = "" if arg.required else " (optional)"
            s += f"  input {arg.name} [{arg.source}]{opt}: {arg.summary}\n"
        if self.flat:
            for group in self.actions[Verb.SOURCE].groups:
                pipe = f" (a pipe fills {group.pipe})" if group.pipe is not None else ""
                s += f"  {group.kind} of: {', '.join(group.members)}{pipe}\n"
        if self._flat_outputs():
            s += f"outputs: {', '.join(self._flat_outputs())}\n"
        return s

    def action_specs_json(self) -> list[dict]:
        """:func:`action_specs` over :meth:`description_json`, the face a Rust host
        deserializes when it mounts this peer, so a contract computed here is computed
        from the same data as the host's."""
        return action_specs(self.description_json())

    def action_matches(self) -> list[dict]:
        """This door's rows as a host's manifold names them: :func:`action_matches`
        of its pattern and :meth:`description_json`."""
        return action_matches(self.iri, self.description_json())

    def description_turtle(self) -> str:
        """The graph face: :func:`to_turtle` of :meth:`description_json`, the face a
        Rust host reads, so the two cannot describe different contracts."""
        return to_turtle(self.description_json())


def action_specs(description: dict) -> list[dict]:
    """``Description::action_specs`` over a description in its serde (JSON) shape: one
    ``ActionSpec`` (serde shape) per non-Meta verb, the explicit one where declared,
    else synthesized from the flat fields with an empty summary. A verb with several
    explicit actions takes the FIRST, as core's ``find`` does; that choice is the floor,
    the contract IRI and the turtle face alike (ledger #1586)."""
    explicit: dict[str, dict] = {}
    for action in description.get("actions", []):
        explicit.setdefault(action["verb"], action)
    specs = []
    for verb in description["verbs"]:
        if verb == Verb.META.wire_name:
            continue
        specs.append(
            explicit.get(verb)
            or {
                "verb": verb,
                "summary": "",
                "inputs": description["inputs"],
                "outputs": description["outputs"],
                "requires": description.get("requires", []),
                **{
                    key: description[key]
                    for key in ("failures", "groups", "arguments", "output_from")
                    if key in description
                },
            }
        )
    return specs


def required_scopes(description: dict, verb: Verb) -> list[str]:
    """The scopes a request with ``verb`` must hold to pass the declared-capability floor,
    read from a description in its serde (JSON) shape: core's
    ``Description::required_scopes``, the same table, row for row, a Rust kernel's floor
    applies (ledger #750).

    - A declared verb: exactly its own ``requires``, from :func:`action_specs`, so an
      explicit per-verb ``requires`` REPLACES the flat one rather than adding to it.
    - An undeclared ``Exists``: what ``Source`` requires, or everything declared when
      there is no ``Source``.
    - Any other undeclared verb: every scope declared anywhere (a mutating verb forces
      a cut, so it is never cheaper than the verbs that were declared).
    - ``Meta``: nothing. Self-description stays readable wherever the catalog lists it.

    The union an undeclared verb gets is deduplicated, in declaration order; a declared
    verb's own ``requires`` comes back as declared, duplicates and all. That is core's
    behavior today, whatever its doc says (ledger #1587), and the vectors pin it.

    >>> dup = {"verbs": ["Source"], "inputs": [], "outputs": [],
    ...     "requires": ["urn:cap:a", "urn:cap:a", "urn:cap:b"]}
    >>> required_scopes(dup, Verb.SOURCE), required_scopes(dup, Verb.DELETE)
    (['urn:cap:a', 'urn:cap:a', 'urn:cap:b'], ['urn:cap:a', 'urn:cap:b'])
    >>> calendar = {"verbs": ["Source", "Sink", "Meta"], "inputs": [], "outputs": [],
    ...     "actions": [{"verb": "Source", "requires": ["urn:cap:cal:read"]},
    ...                 {"verb": "Sink", "requires": ["urn:cap:cal:write"]}]}
    >>> required_scopes(calendar, Verb.SINK), required_scopes(calendar, Verb.EXISTS)
    (['urn:cap:cal:write'], ['urn:cap:cal:read'])
    >>> required_scopes(calendar, Verb.DELETE), required_scopes(calendar, Verb.META)
    (['urn:cap:cal:read', 'urn:cap:cal:write'], [])
    """
    declared = [(spec["verb"], spec.get("requires", [])) for spec in action_specs(description)]
    every: list[str] = []
    sources = [requires for _, requires in declared]
    if not declared:
        sources = [description.get("requires", [])]
        sources += [a.get("requires", []) for a in description.get("actions", [])]
    for requires in sources:
        for scope in requires:  # one scope at a time, so a repeat WITHIN a list is caught too
            if scope not in every:
                every.append(scope)
    own: dict[str, list[str]] = {}
    for declared_verb, requires in declared:
        own.setdefault(declared_verb, requires)  # the first spec for a verb, as core's find
    if verb.wire_name in own:
        return list(own[verb.wire_name])
    if verb == Verb.META:
        return []
    if verb == Verb.EXISTS and Verb.SOURCE.wire_name in own:
        return list(own[Verb.SOURCE.wire_name])
    return every


def unsatisfied_scopes(description: dict, verb: Verb, capability: Capability) -> list[str]:
    """The scopes in :func:`required_scopes` that ``capability`` does not satisfy (core's
    ``Description::unsatisfied_scopes``): empty exactly when the floor admits the
    request. Satisfaction is :func:`ikigai.capability.satisfies`."""
    return [
        s for s in required_scopes(description, verb) if not capability_satisfies(capability, s)
    ]


def action_matches(pattern: str, description: dict) -> list[dict]:
    """A door's rows as a host's manifold names them (ledger #948): per non-Meta verb,
    the ``match`` IRI (``urn:ikigai:match:{verb}:{pattern}``, one per door and verb)
    and the content-addressed ``contract`` IRI it satisfies
    (``urn:ikigai:contract:{id}:{verb}:b3:{hex}``, its ``ik:contract``)."""
    return [
        {
            "verb": spec["verb"],
            "match": contract.match_iri(Verb[spec["verb"].upper()], pattern),
            "contract": contract.contract_iri(description["id"], spec),
        }
        for spec in action_specs(description)
    ]


def to_turtle(description: dict) -> str:
    """A door's graph face from its description (serde shape), mirroring
    ``ikigai_vocab::to_turtle`` byte for byte (pinned by
    ``tests/vectors/contract_vectors.json``, core's own vectors copied verbatim):
    skolemized node IRIs, no blank nodes, the shared ``ik:`` vocabulary. Each verb's
    ``ik:Action`` node is its CONTENT-ADDRESSED contract
    (:func:`ikigai.contract.contract_iri`) and owns its inputs under its own IRI, for a
    flat door too.

    A ``class`` or ``requires`` value is author-supplied and meant to be an IRI, so it
    gets core's two defenses (``ikigai_vocab``'s ``iri_term``, mirrored below): it is
    percent-encoded into a legal ``IRIREF`` (:func:`ikigai.contract.escape_iri_fragment`)
    and then PARSED (:func:`ikigai.contract.is_iri`, core's ``Iri::parse``); one that is
    still not an IRI is written as a literal of the original string, never raw. Pinned
    by the vectors' ``core/hostile-terms`` door.

    A NAME (the endpoint id, an input name) is not an authored IRI: it goes through
    core's ONE name encoder (``ikigai_core::encode_name``,
    :func:`ikigai.contract.encode_name`, mirrored below), so its ``%`` is a character
    (``%25``) and so are ``[``, ``]``, ``#``, ``?`` and every non-``ucschar`` code
    point. The node IRIs then spell an id exactly as a contract IRI does, ``100%`` can
    no longer emit an IRI no parser accepts (ledger #1345), a bracket name and an
    IPv6-host id stay in the path (ledger #1361) and ``a#b`` cannot move the rest of a
    name into a fragment (ledger #1390). Pinned by the vectors' ``core/percent-names``,
    ``core/bracket-names`` and ``core/hash-query-names`` doors."""

    def lit(s: str) -> str:
        escaped = (
            s.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\n", "\\n")
            .replace("\r", "\\r")
            .replace("\t", "\\t")
        )
        return f'"{escaped}"'

    def iri_term(s: str) -> str:
        # Core's `iri_term`: escape, then parse; a value that is still no IRI (a class
        # `http://ok> . <urn:z` escapes to an authority with a non-numeric port) is a literal.
        escaped = contract.escape_iri_fragment(s)
        return f"<{escaped}>" if contract.is_iri(escaped) else lit(s)

    def name_fragment(s: str) -> str:
        # Core's `name_fragment`: ikigai_core::encode_name, the ONE spelling of every
        # name core mints an IRI from. An authored IRI (`iri_term`) keeps its `%`, `[`,
        # `]`, `#`, `?` and non-ucschar code points; a name's are all encoded.
        return contract.encode_name(s)

    def cap_term(scope: str) -> str:
        # Core's `cap_term`: the prefix only decides whether to TRY an IRI.
        if scope.startswith(("urn:", "http://", "https://")):
            return iri_term(scope)
        return lit(scope)

    def failure_term(word: str) -> str:
        # Core's `failure_class_term`: `ik:{Word}Failure`, the class word in UpperCamelCase.
        return "ik:" + "".join(part.capitalize() for part in word.split("-")) + "Failure"

    def input_predicates(owner: str, arg: dict) -> str:
        node = (
            f"ik:inputName {lit(arg['name'])} ;\n"
            f"    ik:source {lit(arg['source'])} ;\n"
            f"    ik:required {'true' if arg['required'] else 'false'}"
        )
        if arg["summary"]:
            node += f" ;\n    ik:summary {lit(arg['summary'])}"
        if arg.get("class") is not None:
            node += f" ;\n    ik:class {iri_term(arg['class'])}"
        if arg.get("default") is not None:
            node += f" ;\n    ik:default {lit(arg['default'])}"
        for value in arg.get("one_of", []):
            node += f" ;\n    ik:oneOf {lit(value)}"
        if "one_of" in arg and not arg["one_of"]:
            node += " ;\n    ik:enumerated true"
        for example in arg.get("examples", []):
            node += f" ;\n    ik:example {lit(example)}"
        if arg.get("list") is not None:
            node += f" ;\n    ik:listSeparator {lit(arg['list'])}"
        for needed in arg.get("needs", []):
            node += f" ;\n    ik:needs <{owner}:input:{name_fragment(needed)}>"
        for value in sorted(arg.get("values", {})):
            node += f" ;\n    ik:valueNote <{value_node_iri(owner, arg['name'], value)}>"
        return node

    def value_node_iri(owner: str, name: str, value: str) -> str:
        return f"{owner}:input:{name_fragment(name)}:value:{name_fragment(value)}"

    def value_nodes(owner: str, arg: dict) -> list[str]:
        skos = "http://www.w3.org/2004/02/skos/core#"
        nodes = []
        for value in sorted(arg.get("values", {})):
            note = arg["values"][value]
            preds = [f"a <{skos}Concept>", f"<{skos}notation> {lit(value)}"]
            if note.get("summary"):
                preds.append(f"<{skos}definition> {lit(note['summary'])}")
            for tag in note.get("tags", []):
                preds.append(f"ik:tag {lit(tag)}")
            for needed in note.get("needs", []):
                preds.append(f"ik:needs <{owner}:input:{name_fragment(needed)}>")
            iri = value_node_iri(owner, arg["name"], value)
            nodes.append(f"<{iri}> " + " ;\n    ".join(preds) + " .")
        return nodes

    def output_from_predicate(owner: str, source) -> str | None:
        if source is None:
            return None
        if source == "origin":
            return "ik:outputFrom ik:Origin"
        return f"ik:outputFrom <{owner}:input:{name_fragment(source['input'])}>"

    def arguments_predicate(owner: str, arguments) -> str | None:
        if not arguments:
            return None
        if "open" in arguments:
            return f"ik:openArguments {lit(arguments['open'])}"
        return f"ik:envelope <{owner}:input:{name_fragment(arguments['envelope'])}>"

    def group_node(owner: str, group: dict) -> tuple[str, str]:
        iri = f"{owner}:{contract.group_segment(group)}"
        preds = ["a ik:InputGroup", f"ik:groupKind {lit(group['kind'])}"]
        for member in group.get("members", []):
            preds.append(f"ik:member <{owner}:input:{name_fragment(member)}>")
        if group.get("pipe") is not None:
            preds.append(f"ik:pipe <{owner}:input:{name_fragment(group['pipe'])}>")
        return iri, f"<{iri}> " + " ;\n    ".join(preds) + " ."

    def add_shape(owner: str, source: dict, preds: list[str]) -> None:
        # The argument set, the output origin and the groups, in the order core writes them.
        for predicate in (
            arguments_predicate(owner, source.get("arguments")),
            output_from_predicate(owner, source.get("output_from")),
        ):
            if predicate is not None:
                preds.append(predicate)
        for group in source.get("groups", []):
            iri, statement = group_node(owner, group)
            preds.append(f"ik:group <{iri}>")
            extra_nodes.append(statement)

    endpoint_iri = f"urn:ikigai:endpoint:{name_fragment(description['id'])}"
    preds = ["a ik:Endpoint", f"ik:id {lit(description['id'])}"]
    if description["title"]:
        preds.append(f"ik:title {lit(description['title'])}")
    if description["summary"]:
        preds.append(f"ik:summary {lit(description['summary'])}")
    preds.append("ik:verb " + ", ".join(lit(v) for v in description["verbs"]))
    if description["outputs"]:
        preds.append("ik:output " + ", ".join(lit(o) for o in description["outputs"]))
    if description.get("requires"):
        preds.append("ik:requires " + ", ".join(cap_term(c) for c in description["requires"]))
    if description.get("failures"):
        preds.append("ik:mayFail " + ", ".join(failure_term(c) for c in description["failures"]))

    extra_nodes: list[str] = []
    for arg in description["inputs"]:
        node_iri = f"{endpoint_iri}:input:{name_fragment(arg['name'])}"
        preds.append(f"ik:input <{node_iri}>")
        extra_nodes.append(f"<{node_iri}> {input_predicates(endpoint_iri, arg)} .")
        extra_nodes.extend(value_nodes(endpoint_iri, arg))
    add_shape(endpoint_iri, description, preds)

    # The per-verb ACTION view — the unit of selection — one content-addressed
    # contract node per non-Meta verb, its inputs under its own IRI.
    for spec in action_specs(description):
        action_iri = contract.contract_iri(description["id"], spec)
        preds.append(f"ik:action <{action_iri}>")
        action_preds = ["a ik:Action", f"ik:verb {lit(spec['verb'])}"]
        if spec.get("summary"):
            action_preds.append(f"ik:summary {lit(spec['summary'])}")
        for output in spec.get("outputs", []):
            action_preds.append(f"ik:output {lit(output)}")
        for cap in spec.get("requires", []):
            action_preds.append(f"ik:requires {cap_term(cap)}")
        for cls in spec.get("failures", []):
            action_preds.append(f"ik:mayFail {failure_term(cls)}")
        for arg in spec.get("inputs", []):
            node_iri = f"{action_iri}:input:{name_fragment(arg['name'])}"
            extra_nodes.append(f"<{node_iri}> {input_predicates(action_iri, arg)} .")
            extra_nodes.extend(value_nodes(action_iri, arg))
            action_preds.append(f"ik:input <{node_iri}>")
        add_shape(action_iri, spec, action_preds)
        extra_nodes.append(f"<{action_iri}> " + " ;\n    ".join(action_preds) + " .")

    ttl = f"@prefix ik: <{VOCAB_NS}> .\n\n<{endpoint_iri}> " + " ;\n    ".join(preds) + " .\n"
    for node in extra_nodes:
        ttl += f"\n{node}\n"
    return ttl


class EndpointDef(_Door):
    """A served single-verb Source endpoint: a handler plus its
    self-description, in the flat authoring form. ``iri`` may be a template
    (``urn:py:echo/{message}``); its variables reach the handler by name."""

    flat = True

    def __init__(
        self,
        handler,
        iri: str,
        *,
        id: str | None = None,
        title: str = "",
        summary: str = "",
        args: list | None = None,
        output: str = TEXT_PLAIN,
        cacheable: bool = False,
        requires: list[str] | None = None,
        groups: list | None = None,
        arguments: dict | None = None,
        output_from: str | dict | None = None,
    ):
        """Wrap ``handler`` as the Source action of ``iri``. ``id`` defaults to the
        handler's name; ``summary`` to the handler docstring's first line."""
        super().__init__(
            iri,
            id or handler.__name__,
            title,
            summary or (handler.__doc__ or "").strip().split("\n")[0],
        )
        self.actions[Verb.SOURCE] = Action(
            Verb.SOURCE,
            handler,
            self.template,
            f"endpoint {iri}",
            args=args,
            output=output,
            cacheable=cacheable,
            requires=requires,
            groups=groups,
            arguments=arguments,
            output_from=output_from,
        )

    # The flat form's contract, read through (the names predate actions).

    @property
    def handler(self):
        """The Source handler."""
        return self.actions[Verb.SOURCE].handler

    @property
    def args(self) -> list[ArgSpec]:
        """The Source action's inputs (bindings first, then by-value arguments)."""
        return self.actions[Verb.SOURCE].args

    @property
    def derived(self) -> bool:
        """True when the Source contract came from the signature (no explicit ``args=``)."""
        return self.actions[Verb.SOURCE].derived

    @property
    def output(self) -> str:
        """The Source action's declared output media type."""
        return self.actions[Verb.SOURCE].output

    @property
    def cacheable(self) -> bool:
        """True when the Source answer is a pure function of its inputs."""
        return self.actions[Verb.SOURCE].cacheable

    @property
    def requires(self) -> list[str]:
        """The Source action's required capabilities (``urn:cap:`` scopes)."""
        return self.actions[Verb.SOURCE].requires


def endpoint(
    iri: str,
    *,
    id: str | None = None,
    title: str = "",
    summary: str = "",
    args: list | None = None,
    output: str = TEXT_PLAIN,
    cacheable: bool = False,
    requires: list[str] | None = None,
    groups: list | None = None,
    arguments: dict | None = None,
    output_from: str | dict | None = None,
):
    """Declare a function as a single-verb Source endpoint.

    With no ``args=`` list the ArgSpecs are DERIVED from the signature (see
    the module docstring for the full table): annotations become XSD classes
    (and incoming wire text is coerced back to the annotated type), defaults
    mark arguments optional, ``Literal`` becomes enforced ``one_of``,
    ``Annotated[T, "…"]`` carries per-argument summaries, and a trailing
    underscore names a reserved word (``in_`` serves the argument ``in`` —
    no ``**kwargs`` workaround needed). An explicit ``args=`` list wins
    wholesale — handlers then receive the wire text uncoerced, exactly as
    before — and a name mismatch with the signature fails at decoration time.

    ``iri`` may be a URI template (``urn:py:echo/{message}``): the endpoint
    then answers every IRI the template matches, and each variable arrives
    as the handler parameter of the same name (see :class:`Family` for the
    binding rules, which are the same here).

    Either way the declaration is REAL: the host engine routes ``key=value``
    arguments by it. ``cacheable=True`` marks the result a pure function of
    its inputs (``Expiry::Never``) — the HOST kernel then caches it. For more
    than one verb over one name, use :func:`family`."""

    def wrap(fn):
        fn.ikigai_endpoint = EndpointDef(
            fn,
            iri,
            id=id,
            title=title,
            summary=summary,
            args=args,
            output=output,
            cacheable=cacheable,
            requires=requires,
            groups=groups,
            arguments=arguments,
            output_from=output_from,
        )
        return fn

    return wrap


class Family(_Door):
    """A multi-verb endpoint, usually over a URI template: one name (or one
    family of names), one contract PER VERB — ``ikigai_core``'s explicit
    ``ActionSpec`` form. Build one with :func:`family` and declare each verb
    with a decorator::

        cell = family("urn:iki:tutorial:ttt:stored:{x}:{y}", id="ttt-stored")

        @cell.source(cacheable=True, summary="the mark played at (x, y)")
        def read(x: int, y: int) -> str: ...

        @cell.sink(summary="play a mark at (x, y)")
        def play(x: int, y: int, content: str) -> str: ...

        serve([cell], path)

    **Bindings.** Each template variable reaches every verb's handler as the
    parameter of the same name, declared ``ik:source "binding"`` in every
    action's inputs (an explicit action does not inherit flat inputs, so each
    names them — as the Rust original does). The parameter's annotation types
    it: ``x: int`` declares ``xsd:integer`` and the handler receives an
    ``int``. **A binding is part of the resource's NAME, so an ``int`` binding
    accepts exactly one spelling per integer** — ``01``, ``+1``, ``-0`` and
    ``1_0`` are refused with ``InvalidArgument`` naming the variable, because
    two spellings of one name are two cache entries and two golden threads
    over one piece of state. (By-value ``int`` ARGUMENTS stay lenient: they
    are request inputs, not identity.) Floats are not canonicalized; do not
    name resources by them. A variable no parameter receives, or a declared
    ``args=`` entry sharing a variable's name, fails at declaration.

    **Verbs.** ``source`` and ``exists`` may be ``cacheable=True``
    (``Expiry::Never``); a ``sink``/``delete`` answer never is, whatever the
    handler returns. A Sink receives its body as ``content`` — the
    ecosystem's pipeline rule (the host engine always routes a piped or
    trailing value there), so every Sink declares a required ``content``:
    if the handler's contract does not name it, it is added (summary "the
    body to write") and validated, and handed to the handler only if it can
    receive it. A ``sink``/``delete`` handler returning ``None`` answers
    ``ok``; an ``exists`` handler may return a ``bool``.

    **Exists** defaults to "Source would succeed": the source handler runs,
    a result answers ``true``, a raised ``NotFoundError`` answers ``false``,
    and any other failure crosses as itself; the answer is cacheable exactly
    when Source is. Declare ``.exists`` to answer it more cheaply. (The
    default runs the Source handler, so it counts as a read wherever reads
    are counted.) A family with neither Source nor Exists refuses Exists. A
    verb the family does not declare is refused with an ``EndpointError``
    naming the verbs it does answer. ``id`` defaults to the pattern's last
    variable-free segment (``stored`` for ``urn:py:stored:{x}:{y}``).

    The host kernel cuts the target's golden thread after every successful
    Sink or Delete it forwards here (ikigai-core >= 0.1.73), so a cacheable
    Source needs no invalidation code on this side."""

    def __init__(self, pattern: str, *, id: str | None = None, title: str = "", summary: str = ""):
        """``id`` defaults to the pattern's last variable-free segment (see
        :func:`_default_id`); every verb is declared later by its decorator."""
        super().__init__(pattern, id or _default_id(pattern), title, summary)

    def _declare(self, verb: Verb, fn, **contract):
        held = self.actions.get(verb)
        if held is not None:
            raise ValueError(
                f"family {self.iri}: {verb.wire_name} is already declared "
                f"(by {held.handler.__name__}())"
            )
        where = f"{verb.wire_name} of {self.iri}"
        self.actions[verb] = Action(verb, fn, self.template, where, **contract)
        return fn

    def _decorator(self, verb: Verb, fn, contract: dict):
        def wrap(f):
            return self._declare(verb, f, **contract)

        return wrap if fn is None else wrap(fn)

    def source(
        self,
        fn=None,
        *,
        summary: str = "",
        args: list | None = None,
        output: str = TEXT_PLAIN,
        cacheable: bool = False,
        requires: list[str] | None = None,
        groups: list | None = None,
        arguments: dict | None = None,
        output_from: str | dict | None = None,
    ):
        """Declare the Source verb (bare ``@f.source`` or ``@f.source(...)``)."""
        contract = dict(
            summary=summary,
            args=args,
            output=output,
            cacheable=cacheable,
            requires=requires,
            groups=groups,
            arguments=arguments,
            output_from=output_from,
        )
        return self._decorator(Verb.SOURCE, fn, contract)

    def sink(
        self,
        fn=None,
        *,
        summary: str = "",
        args: list | None = None,
        output: str = TEXT_PLAIN,
        requires: list[str] | None = None,
        groups: list | None = None,
        arguments: dict | None = None,
        output_from: str | dict | None = None,
    ):
        """Declare the Sink verb; the body arrives as ``content``."""
        contract = dict(
            summary=summary,
            args=args,
            output=output,
            requires=requires,
            groups=groups,
            arguments=arguments,
            output_from=output_from,
        )
        return self._decorator(Verb.SINK, fn, contract)

    def delete(
        self,
        fn=None,
        *,
        summary: str = "",
        args: list | None = None,
        output: str = TEXT_PLAIN,
        requires: list[str] | None = None,
        groups: list | None = None,
        arguments: dict | None = None,
        output_from: str | dict | None = None,
    ):
        """Declare the Delete verb."""
        contract = dict(
            summary=summary,
            args=args,
            output=output,
            requires=requires,
            groups=groups,
            arguments=arguments,
            output_from=output_from,
        )
        return self._decorator(Verb.DELETE, fn, contract)

    def exists(
        self,
        fn=None,
        *,
        summary: str = "",
        args: list | None = None,
        cacheable: bool = False,
        requires: list[str] | None = None,
        groups: list | None = None,
        arguments: dict | None = None,
        output_from: str | dict | None = None,
    ):
        """Declare the Exists verb, replacing the "Source would succeed" default."""
        contract = dict(
            summary=summary,
            args=args,
            cacheable=cacheable,
            requires=requires,
            groups=groups,
            arguments=arguments,
            output_from=output_from,
        )
        return self._decorator(Verb.EXISTS, fn, contract)


def _default_id(pattern: str) -> str:
    """The last segment with no variable in it: ``urn:py:stored:{x}:{y}`` is
    ``stored`` (the Deno face's rule, so a family ported between the two
    keeps its catalog name)."""
    plain = [segment for segment in pattern.split(":") if "{" not in segment]
    return plain[-1] if plain else pattern


def family(pattern: str, *, id: str | None = None, title: str = "", summary: str = "") -> Family:
    """A multi-verb endpoint over ``pattern`` — see :class:`Family`. ``id``
    defaults to the pattern's last variable-free segment."""
    return Family(pattern, id=id, title=title, summary=summary)


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def _decode_arg(name: str, arg) -> str | bytes:
    if isinstance(arg, Inline):
        try:
            return arg.data.decode("utf-8")
        except UnicodeDecodeError:
            return arg.data
    # A peer has no back-channel to the host to dereference a Reference or
    # fetch a Content id — fail loud rather than hand the handler an IRI
    # pretending to be a value. The detail is bare: the InvalidArgument
    # carrier already names the argument.
    raise ValueError("arrived by reference; this peer only takes inline values")


def _coerce_derived(spec: ArgSpec, value: str | bytes):
    """Honor the signature a derived spec came from: the wire delivers text,
    the handler declared a type. Explicit args= endpoints keep receiving the
    wire text untouched (backward compatible). Failure details are bare of
    the argument name — the typed InvalidArgument carrier states it."""
    if spec.one_of and spec.list is None:  # a list's items are checked by the pre-flight
        if not (isinstance(value, str) and value in spec.one_of):
            raise ValueError(f"must be one of {', '.join(spec.one_of)} (got {value!r})")
    base = spec.py_type
    if base is bytes:
        return value.encode("utf-8") if isinstance(value, str) else value
    if base is None:
        return value
    if isinstance(value, bytes):  # only invalid UTF-8 arrives as bytes
        raise ValueError("not valid UTF-8 text")
    if base is str:
        return value
    if base is bool:
        if value == "true":
            return True
        if value == "false":
            return False
        raise ValueError(f"must be `true` or `false` (got {value!r})")
    try:
        return base(value)  # int or float
    except ValueError:
        raise ValueError(f"must be an {base.__name__} (got {value!r})") from None


def _coerce_binding(spec: ArgSpec, text: str):
    """A binding's text, typed. An ``int`` accepts its ONE plain spelling —
    optional ``-``, digits, no leading zeros, no ``+``, no ``-0`` — because
    the binding is part of the name, and two names for one resource are two
    cache threads (the message is the tutorial's, word for word)."""
    if spec.py_type is int and not spec.one_of:
        try:
            value = int(text)
        except ValueError:
            value = None
        if value is None or str(value) != text:
            raise ValueError(f"`{text}` is not an integer in its plain form (e.g. 0, 2, -1)")
        return value
    return _coerce_derived(spec, text)


def _yes_no(value: bool, *, cacheable: bool) -> Resolved:
    rep = Representation(
        b"true" if value else b"false",
        TEXT_PLAIN,
        expiry=Expiry.never() if cacheable else Expiry.always(),
    )
    return Resolved(rep, CacheStatus.MISS if cacheable else CacheStatus.UNCACHEABLE)


@dataclasses.dataclass(frozen=True)
class RunContext:
    """What the calling run carried to this endpoint (wire v9 ``IssueWith``): the retry
    pool remaining (``retries``, ``None`` when the peer sent none) and the ``deadline``
    as a ``time.monotonic()`` instant on THIS host's clock (``None`` for no deadline).
    The peer sent time left, not an instant; the deadline is that plus our own now."""

    retries: int | None = None
    deadline: float | None = None

    def time_left(self) -> float | None:
        """Seconds until the deadline (``0.0`` once past), or ``None`` without one."""
        if self.deadline is None:
            return None
        return max(0.0, self.deadline - time.monotonic())

    def expired(self) -> bool:
        """Whether a deadline was carried and has passed."""
        return self.deadline is not None and time.monotonic() >= self.deadline


_RUN: contextvars.ContextVar[RunContext | None] = contextvars.ContextVar("ikigai_run", default=None)


def current_run() -> RunContext:
    """The :class:`RunContext` of the call a handler is serving: empty (no retry pool, no
    deadline) outside a served call or when the peer sent none."""
    return _RUN.get() or RunContext()


def _refuse(d, verb: Verb) -> ErrorTypedReply:
    """An undeclared verb: an Endpoint failure naming what IS answered."""
    answered = ", ".join(v.wire_name for v in d.answered_verbs())
    return ErrorTypedReply(
        EndpointError(f"verb {verb.wire_name} is not supported by `{d.id}` (it answers {answered})")
    )


class Space:
    """The served resolution space: door lookup + call dispatch.

    **Routing mirrors ``ikigai_core::EndpointSpace``: the first declared door
    whose pattern matches wins** — declare a specific door before a general
    template that would also match it; a later door it shadows is left
    unreachable, exactly as in a Rust kernel. The same pattern text declared
    twice is refused at construction. Each door is matched in two forms,
    its declared pattern and its alias-stripped one; a connection whose hello
    declared an alias mount tries the stripped forms first, every other
    caller the declared forms first."""

    def __init__(self, endpoints, *, strip_alias: bool = True):
        """``endpoints`` are ``@endpoint`` functions, :class:`EndpointDef`\\ s or
        :class:`Family`\\ s; ``strip_alias`` is the default for direct
        :meth:`entries` calls. A duplicate pattern or an endpoint declaring no verb
        raises ``ValueError``."""
        defs = [fn.ikigai_endpoint if hasattr(fn, "ikigai_endpoint") else fn for fn in endpoints]
        for d in defs:
            if not isinstance(d, _Door):
                raise TypeError(
                    f"not an @endpoint-decorated function, EndpointDef or family: {d!r}"
                )
            if not d.actions:
                raise ValueError(f"family {d.iri} ({d.id}) declares no verb")
        self.strip_alias = strip_alias
        self._declared: list[tuple[UriTemplate, _Door]] = []
        self._aliased: list[tuple[UriTemplate, _Door]] = []
        for d in defs:
            self._route(self._declared, d.template, d)
        for d in defs:
            alias = d.alias_template
            if alias is not None:
                self._route(self._aliased, alias, d)
        self._defs = defs

    @staticmethod
    def _route(routes, template: UriTemplate, d: _Door) -> None:
        for held_template, held in routes:
            if held is not d and held_template.source == template.source:
                raise ValueError(f"two endpoints answer {template.source}: {held.id} and {d.id}")
        routes.append((template, d))

    def lookup(self, target: str, alias_first: bool = False):
        """``(door, bindings)`` for ``target``, or ``(None, None)``."""
        passes = (self._aliased, self._declared) if alias_first else (self._declared, self._aliased)
        for routes in passes:
            for template, d in routes:
                bindings = template.match(target)
                if bindings is not None:
                    return d, bindings
        return None, None

    def entries(self, strip_alias: bool | None = None) -> tuple[SpaceEntry, ...]:
        """The catalog: one row per door, its PATTERN (a template for a
        family, so the host's catalog and topology see the family).
        ``strip_alias=None`` uses the server's configured default; a served
        connection always overrides it per its hello mode (a peer KNOWS how
        its mounter addresses it — the hello is required since v7)."""
        strip = self.strip_alias if strip_alias is None else strip_alias
        return tuple(
            SpaceEntry((d.alias_iri if strip else None) or d.iri, d.id) for d in self._defs
        )

    def dispatch(self, call: wire.Call, strip_alias: bool | None = None) -> Reply:
        """Answer one decoded Call. ``strip_alias`` is the connection's hello mode
        (``None`` uses the server default): an alias mount tries stripped patterns
        first, every other caller the declared ones first. Never raises for a
        well-formed call — a failure comes back as an ``ErrorTypedReply``."""
        alias_first = strip_alias is True
        if isinstance(call, EntriesCall):
            return EntriesReply(self.entries(strip_alias))
        if isinstance(call, IsCached):
            return Cached(False)  # this peer keeps no representation cache
        if isinstance(call, Issue):
            # A plain Issue resolves under ROOT, as the Rust IPC door's does
            # (`Resolver::issue` is `issue_as(root)`): the socket admits only the
            # peercred-verified owner, whose entitlement is root.
            return self._resolve(call.request, alias_first, Capability.root())
        if isinstance(call, IssueAs):
            # The carried capability, enforced. The owner's ceiling is root, so
            # resolving under the carried capability IS the clamp.
            return self._resolve(call.request, alias_first, call.capability)
        if isinstance(call, IssueWithCall):
            context = call.context
            run = RunContext(
                context.retries,
                None
                if context.time_left_ms is None
                else time.monotonic() + context.time_left_ms / 1000,
            )
            token = _RUN.set(run)
            try:
                if context.trace is None:
                    return self._resolve(call.request, alias_first, call.capability)
                return self._traced(call.request, call.capability, alias_first)
            finally:
                _RUN.reset(token)
        if isinstance(call, IssueTraced):
            return self._traced(call.request, call.capability, alias_first)
        return ErrorTypedReply(EndpointError(f"unsupported call {type(call).__name__}"))

    def _traced(self, request: Request, capability: Capability, alias_first: bool) -> Reply:
        """Resolve and record the one span (``IssueTraced``, or ``IssueWith`` with a trace)."""
        started = int(time.time() * 1000)
        reply = self._resolve(request, alias_first, capability)
        ended = int(time.time() * 1000)
        if not isinstance(reply, Resolved):
            return reply  # a typed error crosses untraced
        event = TraceEvent(
            target=request.target,
            thread=threading.current_thread().name,
            started=started,
            ended=ended,
            cache_hit=False,
            span=0,
            parent=None,
            capability=(None if capability.is_root else tuple(sorted(capability.scopes or ()))),
        )
        assert isinstance(reply, Resolved)
        return ResolvedTraced(reply.representation, reply.cache_status, (event,))

    def _resolve(
        self,
        request: Request,
        alias_first: bool = False,
        capability: Capability | None = None,
    ) -> Reply:
        """Route and answer ``request`` under ``capability`` (``None`` is root). The
        declared-capability floor runs after routing and before anything is invoked,
        as a Rust kernel's does: an unbound name is ``Unresolved`` whatever the caller
        holds, ``Meta`` is never gated, and a refused call never enters a handler."""
        d, bindings = self.lookup(request.target, alias_first)
        if d is None:
            # The same variant the Rust kernel answers with, so the host-side
            # engine rebuilds Error::Unresolved natively.
            return ErrorTypedReply(UnresolvedError(request.target))
        if request.verb == Verb.META:
            # A description does not depend on the bindings — and the
            # catalog's own pattern text matches its template, so Meta on
            # `urn:…:{x}:{y}` describes the family.
            return self._meta(d, request)
        if capability is not None and not capability.is_root:
            # Declared = enforced (ledger #1580); root satisfies every scope. Read
            # from the door's described contract on every call, so the floor is
            # computed from the same data a mounting host reads, and a verb
            # declared after this Space was built is floored on its own declaration.
            lacking = unsatisfied_scopes(d.description_json(), request.verb, capability)
            if lacking:
                return ErrorTypedReply(DeniedError(denial_message(lacking[0], request.target)))
        action = d.actions.get(request.verb)
        if action is None and request.verb == Verb.EXISTS:
            return self._exists(d, bindings, request)
        if action is None:
            return _refuse(d, request.verb)
        return self._invoke(action, bindings, request)

    def _exists(self, d: _Door, bindings: dict, request: Request) -> Reply:
        """The default Exists. A flat ``@endpoint`` is bound, so it exists
        (the handler never runs). A family's is "would Source succeed": the
        Source handler runs, a NotFound is ``false``, any other failure is
        itself, and the answer is cacheable exactly when Source is (the host
        hangs it from the same thread a Sink cuts). A family with neither
        Source nor Exists refuses Exists."""
        if d.flat:
            return _yes_no(True, cacheable=False)
        source = d.actions.get(Verb.SOURCE)
        if source is None:
            return _refuse(d, Verb.EXISTS)
        reply = self._invoke(source, bindings, request)
        if isinstance(reply, Resolved):
            return _yes_no(True, cacheable=source.cacheable)
        if isinstance(reply, ErrorTypedReply) and isinstance(reply.error, NotFoundError):
            return _yes_no(False, cacheable=source.cacheable)
        return reply

    def _invoke(self, action: Action, bindings: dict, request: Request) -> Reply:
        kwargs = {}
        for arg in action.args:
            if arg.source == "binding":
                # Bindings come from the NAME, never from request.args — an
                # argument spelled like a variable cannot override identity.
                try:
                    kwargs[arg.py_name] = _coerce_binding(arg, bindings[arg.name])
                except ValueError as e:
                    return ErrorTypedReply(InvalidArgumentError(arg.name, str(e)))
                continue
            if arg.name in request.args:
                try:
                    value = _decode_arg(arg.name, request.args[arg.name])
                    if action.derived:
                        value = _coerce_derived(arg, value)
                except ValueError as e:  # by-reference arg, or coercion failure
                    return ErrorTypedReply(InvalidArgumentError(arg.name, str(e)))
                if arg.py_deliver:
                    kwargs[arg.py_name] = value
            elif arg.required:
                return ErrorTypedReply(MissingArgumentError(arg.name))
            elif action.derived:
                # The Python default (typed, e.g. int 3 stays an int) fills
                # an absent optional argument; an Optional[T] parameter
                # without one gets None.
                if not arg.py_has_default:
                    kwargs[arg.py_name] = None
            elif arg.default is not None:
                kwargs[arg.py_name] = arg.default
        refused = self._preflight(action, bindings, request)
        if refused is not None:
            return refused
        try:
            result = action.handler(**kwargs)
        except EndpointError as e:
            # A handler may RAISE the taxonomy deliberately (NotFoundError for
            # an absent row, DeniedError for a refused grant, …) — it crosses
            # the wire typed, and an HTTP face on the far side answers
            # 404/403/… instead of a blanket 502.
            return ErrorTypedReply(e)
        except Exception as e:  # a handler bug crosses as an endpoint error
            return ErrorTypedReply(EndpointError(str(e)))
        return Resolved(*self._representation(action, result))

    # The rules of core's one checker that a call can break beyond a missing input and a
    # value outside `one_of` (which `_invoke` enforces as it reads each argument): a group,
    # a dependency, an envelope given as both carrier and fields, and a list item outside
    # `one_of`. `unknown` and `implausible` are not enforced here, as a Rust kernel's
    # engine does not either: they are what validation REPORTS.
    _ENFORCED = frozenset(
        {"exactly-one", "at-least-one", "at-most-one", "needs", "envelope", "not-one-of"}
    )

    def _preflight(self, action: Action, bindings: dict, request: Request) -> Reply | None:
        """``ikigai.check.check`` of the action's contract over the call, bindings
        included (a binding is given by the name); the first enforced violation refuses
        it before the handler runs, as ``InvalidArgument`` naming what it is about."""
        pairs = [(a.name, bindings[a.name]) for a in action.args if a.source == "binding"]
        pairs += [
            (name, arg.data.decode("utf-8", "replace"))
            for name, arg in request.args.items()
            if isinstance(arg, Inline)
        ]
        for violation in preflight.check(action.spec_json(), pairs):
            if violation.kind in self._ENFORCED:
                path = violation.path or ""
                subject = path.split(":")[1] if path.startswith("input:") else path.split(":", 2)[2]
                return ErrorTypedReply(InvalidArgumentError(subject, violation.message))
        return None

    def _representation(self, action: Action, result) -> tuple[Representation, CacheStatus]:
        media_type = action.output
        if result is None and _is_mutating(action.verb):
            result = "ok"
        if isinstance(result, bool) and action.verb == Verb.EXISTS:
            result = "true" if result else "false"
        if isinstance(result, tuple) and len(result) == 2:
            result, media_type = result
        if isinstance(result, Representation):
            rep = result
        else:
            data = result.encode("utf-8") if isinstance(result, str) else bytes(result)
            expiry = Expiry.never() if action.cacheable else Expiry.always()
            rep = Representation(data, media_type, expiry=expiry)
        if _is_mutating(action.verb):
            rep.expiry = Expiry.always()  # a write's answer is never served from a cache
        # No cache here: cacheable results report MISS ("computed now,
        # cacheable downstream" — the HOST kernel caches by the expiry),
        # everything else UNCACHEABLE.
        status = CacheStatus.MISS if rep.expiry.kind != "always" else CacheStatus.UNCACHEABLE
        return rep, status

    def _meta(self, d: _Door, request: Request) -> Reply:
        target = "text/turtle"  # the kernel's default Meta face
        as_arg = request.args.get("as")
        if isinstance(as_arg, Inline):
            try:
                target = as_arg.data.decode("utf-8")
            except UnicodeDecodeError:
                pass
        if target in ("text/turtle", "*/*", ""):
            body = d.description_turtle().encode("utf-8")
            rep = Representation(body, "text/turtle")
        elif target == "text/plain":
            rep = Representation(d.description_text().encode("utf-8"), "text/plain;charset=utf-8")
        elif target == "application/json":
            body = json.dumps(d.description_json(), separators=(",", ":")).encode("utf-8")
            rep = Representation(body, "application/json")
        else:
            return ErrorTypedReply(
                EndpointError(f"meta renderer does not support target `{target}`")
            )
        return Resolved(rep, CacheStatus.UNCACHEABLE)


# ---------------------------------------------------------------------------
# The socket server
# ---------------------------------------------------------------------------


def _peer_uid(conn: socket.socket) -> int | None:
    """The connected peer's kernel-verified UID, or ``None`` if unreadable."""
    try:
        if sys.platform == "linux":
            raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            _pid, uid, _gid = struct.unpack("3i", raw)
            return uid
        # macOS / BSD: LOCAL_PEERCRED yields a `struct xucred`
        # (u_int cr_version; uid_t cr_uid; short cr_ngroups; gid_t cr_groups[16]).
        sol_local = 0
        local_peercred = 0x001
        raw = conn.getsockopt(sol_local, local_peercred, 128)
        version, uid = struct.unpack_from("II", raw)
        if version != 0:  # XUCRED_VERSION
            return None
        return uid
    except OSError:
        return None


class Server:
    """A wire server for a set of endpoints. ``serve_forever`` blocks; call
    ``shutdown`` from another thread (or use as a context manager)."""

    def __init__(
        self,
        endpoints,
        path: str | Path,
        *,
        strip_alias: bool = True,
        check_peer_uid: bool = True,
    ):
        """Bind ``path`` (created ``0600`` in a ``0700`` parent), replacing a leftover
        socket at that path. ``check_peer_uid=False`` serves any peer — tests only."""
        self.space = Space(endpoints, strip_alias=strip_alias)
        self.path = Path(path)
        self._check_peer_uid = check_peer_uid
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.unlink(missing_ok=True)  # a leftover socket would fail the bind
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._listener.bind(str(self.path))
        self.path.chmod(0o600)
        self._listener.listen()
        self._closing = False

    def serve_forever(self) -> None:
        """Accept and serve connections until :meth:`shutdown`; each connection is
        handled on its own daemon thread."""
        while True:
            try:
                conn, _ = self._listener.accept()
            except OSError:
                if self._closing:
                    return
                raise
            if self._closing:
                conn.close()  # the shutdown wake-up connection
                return
            if self._check_peer_uid and _peer_uid(conn) != _own_uid():
                conn.close()  # not our user (or unverifiable) — drop it
                continue
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        with conn, conn.makefile("rwb") as f:
            # The FIRST frame must be the hello (required since wire v7). A
            # version this side accepts (MIN_PROTOCOL_VERSION..=PROTOCOL_VERSION)
            # is answered WITH THE PEER'S OWN VERSION and served at it — a v7
            # client requires a v7 answer — and its mode picks this
            # connection's entries form. Anything else gets our version (so
            # the client names both in its error, or a newer one redials at
            # ours) and a close. A frame WITHOUT the magic is a <= v5 client's
            # first Call and is REFUSED — the v6 serve-it-anyway tolerance is
            # over.
            try:
                first = wire.read_frame(f)
            except (EOFError, OSError):
                return
            hello = wire.decode_hello(first)
            if hello is None:
                print(
                    "ikigai-python: refused a client that connected without the "
                    f"version hello (wire <= v5; v{wire.PROTOCOL_VERSION} requires "
                    "it). Update the client.",
                    file=sys.stderr,
                )
                return
            peer_version = wire.accepted_version(hello.version)
            try:
                wire.write_frame(
                    f, wire.encode_hello(wire.Hello(peer_version or wire.PROTOCOL_VERSION))
                )
            except OSError:
                return
            if peer_version is None:
                return  # the client renders the mismatch
            strip_alias = hello.mode == wire.HelloMode.ALIAS
            while True:
                try:
                    frame = wire.read_frame(f)
                except (EOFError, OSError):
                    return  # peer hung up
                if not self._serve_one_frame(f, frame, strip_alias, peer_version):
                    return

    def _serve_one_frame(
        self,
        f,
        frame: bytes,
        strip_alias: bool | None,
        peer_version: int = wire.PROTOCOL_VERSION,
    ) -> bool:
        """Decode and answer one Call frame; ``False`` ends the connection.
        Every reply is encoded for ``peer_version`` — the version the peer's
        hello spoke — so a v7 peer never receives a v8-only variant (a
        ``ConflictError`` reaches it as ``Endpoint("conflict: …")``)."""
        try:
            call = wire.decode_call(frame)
        except wire.ProtocolError as e:
            # An undecodable frame. Answer once, loudly, then drop the
            # connection — framing after a bad frame is unreliable.
            try:
                wire.write_frame(
                    f,
                    wire.encode_reply(
                        ErrorTypedReply(EndpointError(str(e))), peer_version=peer_version
                    ),
                )
            except OSError:
                pass
            return False
        try:
            wire.write_frame(
                f,
                wire.encode_reply(
                    self.space.dispatch(call, strip_alias), peer_version=peer_version
                ),
            )
        except OSError:
            return False
        return True

    def shutdown(self) -> None:
        """Stop :meth:`serve_forever` and remove the socket file. Safe to call from
        another thread; a no-op if the server is already down."""
        self._closing = True
        # On Linux, closing a listening socket does NOT wake a thread blocked
        # in accept() — connect once to nudge the accept loop awake, so a
        # serve_forever thread exits promptly instead of only on join timeout.
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as nudge:
                nudge.settimeout(1)
                nudge.connect(str(self.path))
        except OSError:
            pass  # nothing accepting (already down) is fine
        self._listener.close()
        self.path.unlink(missing_ok=True)

    def __enter__(self) -> Server:
        """``with Server(...) as s:`` — returns this server (already bound)."""
        return self

    def __exit__(self, *exc) -> None:
        """Shut the server down on leaving the ``with`` block."""
        self.shutdown()


def _own_uid() -> int:
    import os

    return os.getuid()


def serve(
    endpoints,
    path: str | Path,
    *,
    strip_alias: bool = True,
    check_peer_uid: bool = True,
) -> None:
    """Serve ``endpoints`` (functions decorated with :func:`endpoint`) on the
    Unix socket at ``path``. Blocks until interrupted."""
    server = Server(
        endpoints,
        path,
        strip_alias=strip_alias,
        check_peer_uid=check_peer_uid,
    )
    try:
        server.serve_forever()
    finally:
        server.shutdown()
