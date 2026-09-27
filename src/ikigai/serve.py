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
``TimeoutError``, ``UnavailableError`` from :mod:`ikigai.wire`) and the
variant crosses intact — the far side's HTTP face can answer 404/403/…
instead of a blanket 502.

**Security posture**: the socket is ``0600`` in a ``0700`` directory and
peers are refused unless their kernel-verified UID matches the server's —
the same transport trust as the Rust IPC server. A capability carried on
``IssueAs``/``IssueTraced`` is accepted but not enforced per-scope
(capability-on-the-wire for IPC is a known TODO on the Rust side too).
"""

from __future__ import annotations

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

from . import wire
from .template import TemplateError, UriTemplate
from .wire import (
    Cached,
    CacheStatus,
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
    ):
        if source not in ("argument", "binding"):
            raise ValueError(f"ArgSpec source must be `argument` or `binding` (got {source!r})")
        self.name = name
        self.summary = summary
        # A declared default implies the argument is optional (as in Rust).
        self.required = required if default is None else False
        self.cls = cls
        self.default = default
        self.one_of = list(one_of or [])
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
        if self.one_of:
            out["one_of"] = self.one_of
        return out


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
            one_of=one_of,
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
    ):
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
        if self._flat_outputs():
            s += f"outputs: {', '.join(self._flat_outputs())}\n"
        return s

    def description_turtle(self) -> str:
        """The graph face (mirrors ``ikigai_vocab::to_turtle``): skolemized
        node IRIs, no blank nodes, the shared ``ik:`` vocabulary. A flat
        door's synthesized action REFERENCES the endpoint-level input nodes;
        an explicit action (a family's) scopes its own under the action."""

        def lit(s: str) -> str:
            return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'

        def cap_term(scope: str) -> str:
            if scope.startswith(("urn:", "http://", "https://")):
                return f"<{scope}>"
            return lit(scope)

        def input_predicates(arg: ArgSpec) -> str:
            node = (
                f"ik:inputName {lit(arg.name)} ;\n"
                f"    ik:source {lit(arg.source)} ;\n"
                f"    ik:required {'true' if arg.required else 'false'}"
            )
            if arg.summary:
                node += f" ;\n    ik:summary {lit(arg.summary)}"
            if arg.cls is not None:
                node += f" ;\n    ik:class <{arg.cls}>"
            if arg.default is not None:
                node += f" ;\n    ik:default {lit(arg.default)}"
            for value in arg.one_of:
                node += f" ;\n    ik:oneOf {lit(value)}"
            return node

        endpoint_iri = f"urn:ikigai:endpoint:{self.id}"
        preds = [
            "a ik:Endpoint",
            f"ik:id {lit(self.id)}",
        ]
        if self.title:
            preds.append(f"ik:title {lit(self.title)}")
        if self.summary:
            preds.append(f"ik:summary {lit(self.summary)}")
        preds.append("ik:verb " + ", ".join(lit(v.wire_name) for v in self.verbs))
        if self._flat_outputs():
            preds.append("ik:output " + ", ".join(lit(o) for o in self._flat_outputs()))
        if self._flat_requires():
            preds.append("ik:requires " + ", ".join(cap_term(c) for c in self._flat_requires()))

        extra_nodes: list[str] = []
        for arg in self._flat_inputs():
            node_iri = f"{endpoint_iri}:input:{arg.name}"
            preds.append(f"ik:input <{node_iri}>")
            extra_nodes.append(f"<{node_iri}> {input_predicates(arg)} .")

        # The per-verb ACTION view — the unit of selection.
        for verb in self.verbs:
            action = self.actions.get(verb)
            if action is None:
                continue  # Meta is never a selectable action
            action_iri = f"{endpoint_iri}:action:{verb.wire_name.lower()}"
            preds.append(f"ik:action <{action_iri}>")
            action_preds = ["a ik:Action", f"ik:verb {lit(verb.wire_name)}"]
            if action.summary and not self.flat:
                action_preds.append(f"ik:summary {lit(action.summary)}")
            action_preds.append(f"ik:output {lit(action.output)}")
            for cap in action.requires:
                action_preds.append(f"ik:requires {cap_term(cap)}")
            for arg in action.args:
                if self.flat:
                    node_iri = f"{endpoint_iri}:input:{arg.name}"
                else:
                    node_iri = f"{action_iri}:input:{arg.name}"
                    extra_nodes.append(f"<{node_iri}> {input_predicates(arg)} .")
                action_preds.append(f"ik:input <{node_iri}>")
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
    ):
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
        )

    # The flat form's contract, read through (the names predate actions).

    @property
    def handler(self):
        return self.actions[Verb.SOURCE].handler

    @property
    def args(self) -> list[ArgSpec]:
        return self.actions[Verb.SOURCE].args

    @property
    def derived(self) -> bool:
        return self.actions[Verb.SOURCE].derived

    @property
    def output(self) -> str:
        return self.actions[Verb.SOURCE].output

    @property
    def cacheable(self) -> bool:
        return self.actions[Verb.SOURCE].cacheable

    @property
    def requires(self) -> list[str]:
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
    ):
        """Declare the Source verb (bare ``@f.source`` or ``@f.source(...)``)."""
        contract = dict(
            summary=summary, args=args, output=output, cacheable=cacheable, requires=requires
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
    ):
        """Declare the Sink verb; the body arrives as ``content``."""
        contract = dict(summary=summary, args=args, output=output, requires=requires)
        return self._decorator(Verb.SINK, fn, contract)

    def delete(
        self,
        fn=None,
        *,
        summary: str = "",
        args: list | None = None,
        output: str = TEXT_PLAIN,
        requires: list[str] | None = None,
    ):
        """Declare the Delete verb."""
        contract = dict(summary=summary, args=args, output=output, requires=requires)
        return self._decorator(Verb.DELETE, fn, contract)

    def exists(
        self,
        fn=None,
        *,
        summary: str = "",
        args: list | None = None,
        cacheable: bool = False,
        requires: list[str] | None = None,
    ):
        """Declare the Exists verb, replacing the "Source would succeed" default."""
        contract = dict(summary=summary, args=args, cacheable=cacheable, requires=requires)
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
    if spec.one_of:
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
        alias_first = strip_alias is True
        if isinstance(call, EntriesCall):
            return EntriesReply(self.entries(strip_alias))
        if isinstance(call, IsCached):
            return Cached(False)  # this peer keeps no representation cache
        if isinstance(call, Issue | IssueAs):
            return self._resolve(call.request, alias_first)
        if isinstance(call, IssueTraced):
            started = int(time.time() * 1000)
            reply = self._resolve(call.request, alias_first)
            ended = int(time.time() * 1000)
            if not isinstance(reply, Resolved):
                return reply  # a typed error crosses untraced
            capability = call.capability
            event = TraceEvent(
                target=call.request.target,
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
        return ErrorTypedReply(EndpointError(f"unsupported call {type(call).__name__}"))

    def _resolve(self, request: Request, alias_first: bool = False) -> Reply:
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
            # The FIRST frame must be the hello (required since wire v7). It
            # is answered with ours — equal versions proceed (and its mode
            # picks this connection's entries form), unequal versions get the
            # answer (so the client names both in its error) and a close. A
            # frame WITHOUT the magic is a <= v5 client's first Call and is
            # REFUSED — the v6 serve-it-anyway tolerance is over.
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
            try:
                wire.write_frame(f, wire.encode_hello(wire.Hello(wire.PROTOCOL_VERSION)))
            except OSError:
                return
            if hello.version != wire.PROTOCOL_VERSION:
                return  # the client renders the mismatch
            strip_alias = hello.mode == wire.HelloMode.ALIAS
            while True:
                try:
                    frame = wire.read_frame(f)
                except (EOFError, OSError):
                    return  # peer hung up
                if not self._serve_one_frame(f, frame, strip_alias):
                    return

    def _serve_one_frame(self, f, frame: bytes, strip_alias: bool | None) -> bool:
        """Decode and answer one Call frame; ``False`` ends the connection."""
        try:
            call = wire.decode_call(frame)
        except wire.ProtocolError as e:
            # An undecodable frame. Answer once, loudly, then drop the
            # connection — framing after a bad frame is unreliable.
            try:
                wire.write_frame(f, wire.encode_reply(ErrorTypedReply(EndpointError(str(e)))))
            except OSError:
                pass
            return False
        try:
            wire.write_frame(f, wire.encode_reply(self.space.dispatch(call, strip_alias)))
        except OSError:
            return False
        return True

    def shutdown(self) -> None:
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
        return self

    def __exit__(self, *exc) -> None:
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
