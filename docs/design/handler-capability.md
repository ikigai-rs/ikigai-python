# The caller's capability in a served handler

Status: design note (ledger #1584). It covers both polyglot faces: `ikigai-python` (this
repository) and `ikigai-ts` (the Deno and TypeScript face). Code in this change: one test,
`tests/test_handler_capability.py`. It reproduces the gap and proves the design without
touching `src/`.

Revisions cited:
[ikigai-core `1d11151`](https://github.com/ikigai-rs/ikigai-core/tree/1d111516aa7e390110a8ad1007e96576966eb90a),
[ikigai-ts `7b5fb2a`](https://github.com/ikigai-rs/ikigai-ts/tree/7b5fb2aaac29e7aef8592381856c835cce724427),
[ikigai-fs `3004223`](https://github.com/ikigai-rs/ikigai-fs/tree/300422337f2dcf32f5a5a7cd2bc3a85aee311a5a).

## The problem

Both faces enforce the declared-capability floor (ledger #1580). Before a handler runs, the
call's capability must satisfy what the door's contract `requires`. That check is coarse.
`requires: ["urn:cap:fs:read:*"]` asks only whether the caller holds *some* read grant. It
does not ask whether that grant covers *this* path. A Rust module makes the finer decision
itself, inside the endpoint, from the caller's capability. A Python or TypeScript handler
cannot, because it never receives the capability:

- Python: a handler gets its declared inputs as keyword arguments, and
  `ikigai.serve.current_run()` gives it the retry pool and the deadline. Nothing gives it
  the capability. `Space._resolve` holds the capability for the floor and drops it before
  `_invoke` (`src/ikigai/serve.py`, `Space._resolve`).
- TypeScript: `Handler = (args: HandlerArgs, run: RunContext) => …`
  ([`serve.ts:424`](https://github.com/ikigai-rs/ikigai-ts/blob/7b5fb2aaac29e7aef8592381856c835cce724427/src/serve.ts#L424)).
  `#resolve` uses the capability for the floor and then calls
  `hit.def.answer(request, hit.bindings, run)` without it
  ([`serve.ts:2002`](https://github.com/ikigai-rs/ikigai-ts/blob/7b5fb2aaac29e7aef8592381856c835cce724427/src/serve.ts#L2002)).

So a path ACL, a host ACL or a per-principal rule cannot be written in a polyglot peer.
`test_the_gap_a_handler_sees_only_its_inputs_and_the_run` pins this today, and so does the
strict `xfail` `test_the_gap_a_path_acl_cannot_be_written_with_the_public_api`.

## What the Rust core gives an endpoint

- **The effective capability, read-only.** `Invocation` has a public field
  `capability: &'a Capability`, documented as "the capability authorizing invocation"
  ([`endpoint.rs:475`](https://github.com/ikigai-rs/ikigai-core/blob/1d111516aa7e390110a8ad1007e96576966eb90a/crates/ikigai-core/src/endpoint.rs#L475)).
  The kernel sets it after any attenuation and clamping. An endpoint gets a shared
  reference to it and cannot replace it.
- **Four reads on it**:
  - `is_root()`
    ([`capability.rs:357`](https://github.com/ikigai-rs/ikigai-core/blob/1d111516aa7e390110a8ad1007e96576966eb90a/crates/ikigai-core/src/capability.rs#L357))
  - `principal()`, the single principal the capability carries, if any
    ([`:452`](https://github.com/ikigai-rs/ikigai-core/blob/1d111516aa7e390110a8ad1007e96576966eb90a/crates/ikigai-core/src/capability.rs#L452))
  - `allows(scope)`, which asks whether a grant is held exactly, never counting an
    exclusion
    ([`:546`](https://github.com/ikigai-rs/ikigai-core/blob/1d111516aa7e390110a8ad1007e96576966eb90a/crates/ikigai-core/src/capability.rs#L546))
  - `satisfies(scope)`, the floor's predicate, where a trailing `*` means a presence
    test
    ([`:592`](https://github.com/ikigai-rs/ikigai-core/blob/1d111516aa7e390110a8ad1007e96576966eb90a/crates/ikigai-core/src/capability.rs#L592))
- **The scopes themselves.** `scopes()` returns every held scope, grants and exclusions
  alike, or `None` for root. Its documentation says it is "for modules that interpret
  their own grammar (path and host rules)"
  ([`:599`](https://github.com/ikigai-rs/ikigai-core/blob/1d111516aa7e390110a8ad1007e96576966eb90a/crates/ikigai-core/src/capability.rs#L599)).
  The documentation of `satisfies` divides the work the same way: "the floor says 'may
  hold a grant here', the module says which", and "the finer runtime gate (a path or host
  ACL) stays the module's".
- **No way to escalate.** Every sub-request form on `Invocation` issues under the
  invocation's own capability or under a weaker one. The private `issue_under`
  ([`endpoint.rs:1685`](https://github.com/ikigai-rs/ikigai-core/blob/1d111516aa7e390110a8ad1007e96576966eb90a/crates/ikigai-core/src/endpoint.rs#L1685))
  is the only thing that takes a capability. An endpoint can read its authority but cannot
  hand anyone a stronger one.
- **The cache already separates callers.** The kernel's cache key is the request's id plus
  the fingerprint of the capability the request was issued under, plus the chain's
  fingerprint
  ([`request.rs:93`](https://github.com/ikigai-rs/ikigai-core/blob/1d111516aa7e390110a8ad1007e96576966eb90a/crates/ikigai-core/src/request.rs#L93)).
  So when an answer depends on the capability, the host never serves it to a caller with a
  different capability. A face needs no new expiry rule for an ACL'd answer.

ikigai-fs is the worked example. It builds its `Acl` from `inv.capability`: it reads
`scopes()`, keeps those under `urn:cap:fs:<action>:`, maps each into the jail, and lets the
deepest rule containing the path decide, with a deny winning a tie and no match denying
([`lib.rs:1286`](https://github.com/ikigai-rs/ikigai-fs/blob/300422337f2dcf32f5a5a7cd2bc3a85aee311a5a/src/lib.rs#L1286)).
It runs this before it touches the path, so a refusal reveals nothing about what exists
([`lib.rs:419`](https://github.com/ikigai-rs/ikigai-fs/blob/300422337f2dcf32f5a5a7cd2bc3a85aee311a5a/src/lib.rs#L419)).
That rule **enumerates** the scopes under a prefix. Asking `allows` or `satisfies` about one
scope at a time cannot express it: `allows` never reports a held exclusion, and the rule
has to compare every rule's depth. A polyglot face therefore has to expose the scopes,
not only a predicate.

## What crosses the wire: nothing new

The capability already crosses. `IssueAs`, `IssueTraced` and `IssueWith` carry it, and the
floor in both faces already resolves under it. A plain `Issue` resolves under root, as the
Rust IPC door's does: the socket admits only the verified owner, whose entitlement is root.
The handler should see exactly the capability the floor checked, so:

| Call form | Capability the handler sees |
| --- | --- |
| `Issue` | root |
| `IssueAs`, `IssueTraced`, `IssueWith` | the carried capability, as decoded |

There is no wire version bump, no new call and no new reply. A peer that never reads the
capability behaves exactly as it does today.

### Never exposed

- **Secrets.** A `Capability` holds scope strings and nothing else. Transport credentials
  never reach the handler through this API: the peer-credential UID on the socket, a QUIC
  client certificate or its pin, and an `Authorization` value. None of them are part of
  the capability.
- **Authority to act as the caller.** The view is read-only. It offers no method that
  issues a request. In Python the view's scopes are a `frozenset`; in TypeScript they are a
  `ReadonlySet`, and the view object is frozen. The Rust non-escalation rule is about what
  an endpoint can *issue*. A polyglot handler that issues sub-requests through
  `ikigai.client` / `client.ts` does so on its own connection, under that connection's
  authority. The follow-up below proposes making "issue under the caller's capability"
  the documented default for that case.
- **Anything about other calls.** The view belongs to one call. It is unset outside a
  served call and does not leak into the next call on the connection.
  `test_the_capability_is_scoped_to_one_call` checks this.
- **The host's own grant or ceiling.** The peer sees what the host carried, the effective
  capability, and never the authority that capability was clamped from.

### The full grant list or only a predicate?

Expose both, as core does: `allows`, `satisfies`, `principal`, `is_root`, and the scopes.
A predicate alone cannot express a path ACL (see above). Withholding the scopes would also
protect nothing, because the peer process has already decoded the full set off the wire to
run the floor. The one convenience beyond core is `under(prefix)`. It returns the held
scopes that start with `prefix`, sorted, exclusions included. It encourages a handler to
read only its own grammar rather than walk, log or echo the whole capability.

## The API each face adds

Both additions are additive. Every existing handler keeps working unchanged.

### Python: `ikigai.serve.current_caller()`

```python
@dataclasses.dataclass(frozen=True)
class Caller:
    """The capability the served call resolves under (core's ``Invocation::capability``)."""

    capability: Capability          # the frozen wire value

    @property
    def is_root(self) -> bool: ...
    @property
    def scopes(self) -> frozenset[str] | None: ...   # None for root, as core's scopes()
    def principal(self) -> str | None: ...           # ikigai.capability.principal
    def allows(self, scope: str) -> bool: ...        # ikigai.capability.allows
    def satisfies(self, scope: str) -> bool: ...     # ikigai.capability.satisfies
    def under(self, prefix: str) -> tuple[str, ...]: ...  # sorted; () for root


def current_caller() -> Caller:
    """The caller of the call a handler is serving. Outside a served call it raises
    ``LookupError``, so a handler run directly in a test must say what it holds
    (see ``with_caller``)."""


@contextlib.contextmanager
def with_caller(capability: Capability) -> Iterator[Caller]:
    """Run handler code directly under ``capability``, for unit tests."""
```

`current_caller()` follows the `current_run()` precedent: a `contextvars.ContextVar` that
`Space._resolve` sets before routing and resets in a `finally` (exactly what the test's
`CallerSpace` does). Two choices need explaining:

- **A context variable, not a parameter.** `derive_args` turns every parameter of a
  handler into a declared input. A capability parameter would either show up in the
  contract or need a special case in derivation, binding and `Meta`. A context variable
  changes no signature and no contract digest.
- **It raises outside a served call.** It does not default to root. Called outside a
  served call, it fails closed instead of granting everything. `with_caller` is how a test
  states the authority it means.

### TypeScript: a third handler argument

```ts
/** The capability the served call resolves under (core's `Invocation::capability`). */
export interface Caller {
  readonly isRoot: boolean;
  /** Every held scope, grants and exclusions alike; `null` for root. */
  readonly scopes: ReadonlySet<string> | null;
  principal(): string | null;
  allows(scope: string): boolean;
  satisfies(scope: string): boolean;
  /** Held scopes starting with `prefix`, sorted; `[]` for root. */
  under(prefix: string): readonly string[];
}

export type Handler = (
  args: HandlerArgs,
  run: RunContext,
  caller: Caller,
) => HandlerResult | Promise<HandlerResult>;

export type ExistsHandler = (
  args: HandlerArgs,
  run: RunContext,
  caller: Caller,
) => boolean | Promise<boolean>;

/** Build a Caller for unit tests: `callerOf(Capability.scoped([...]))`. */
export function callerOf(capability: Capability): Caller;
```

The third argument is backward compatible. A function that declares fewer parameters can
be assigned to a function type with more, and JavaScript ignores extra arguments, so
`(args, run) => …` and `(args) => …` still type-check and run. This is a better fit than a
field on `RunContext`: a new required field would break every object written as a
`RunContext` literal, while a third parameter breaks nothing. It also mirrors core, where
the run and the capability are separate fields of `Invocation`. `#resolve` passes
`callerOf(capability)` through `answer` to `runHandler`, beside `run`.

## Writing and testing an argument-dependent rule

The reference rule both faces' tests (and the shared vectors) use is a deliberately small
version of ikigai-fs's. It reads scopes of the form `urn:cap:docs:read:<dir>`, where a
leading `-` on the directory marks a deny. Among the rules that contain the path, the
deepest decides. A deny wins a tie. No match denies. Root allows everything. The contract
declares the family floor `urn:cap:docs:read:*`, so the floor still turns away a caller who
holds no read grant at all, before the handler runs.

Python (as proven in `tests/test_handler_capability.py`, where `current_caller()` is the
test's stand-in):

```python
@endpoint("urn:py:docs", requires=["urn:cap:docs:read:*"])
def read_doc(path: str) -> str:
    if not acl_allows(current_caller(), path):     # before the path is looked at
        raise DeniedError(f"capability does not grant `read` on `{path}`")
    return load(path)


def test_a_deeper_exclusion_wins():
    with with_caller(Capability.scoped(["urn:cap:docs:read:/home",
                                        "urn:cap:docs:read:-/home/secret"])):
        with pytest.raises(DeniedError):
            read_doc("/home/secret/key")
```

TypeScript:

```ts
const docs = endpoint("urn:ts:docs", {
  requires: ["urn:cap:docs:read:*"],
  args: [{ name: "path", required: true }],
}, (args, _run, caller) => {
  const path = String(args.path);
  if (!aclAllows(caller, path)) {          // before the path is looked at
    throw new DeniedError(`capability does not grant \`read\` on \`${path}\``);
  }
  return load(path);
});

Deno.test("a deeper exclusion wins", async () => {
  const caller = callerOf(Capability.scoped([
    "urn:cap:docs:read:/home", "urn:cap:docs:read:-/home/secret",
  ]));
  await assertRejects(() => docs.handler({ path: "/home/secret/key" }, NO_RUN, caller),
    DeniedError);
});
```

Each face tests at two levels:

1. **Unit.** Call the handler directly under a stated caller (`with_caller` / `callerOf`).
2. **Served.** Dispatch through `Space.dispatch` / the TS server under each call form, and
   check two things: the handler sees root for `Issue` and the carried capability
   otherwise, and a floor refusal still never enters the handler. The proof test does
   this for Python today.

Two rules for authors belong in each face's README once the API lands:

- **Decide before you look.** Run the ACL before reading the target. A refusal then says
  nothing about whether the target exists, as ikigai-fs does. A door that must hide
  existence should raise `NotFoundError` rather than `DeniedError` for a path the ACL
  refuses.
- **The answer may be cached by its expiry.** The host's cache key includes the
  capability's fingerprint, so an ACL-dependent answer does not need to be made
  uncacheable. It must depend only on the capability, the arguments and what the handler
  reads, as every cacheable answer must.

## Shared conformance vectors

A new file pins both faces alike: `handler_capability_vectors.json`. Core computes it, in
ikigai-vocab's `tests/vectors/`, and both faces copy it verbatim, as they already do for
`capability_vectors.json`. Core produces each row by issuing through a kernel to a Rust
endpoint that implements the reference rule from `inv.capability`, so the expectations
are Rust's own.

```json
{
  "about": "…",
  "acl": { "prefix": "urn:cap:docs:read:", "floor": "urn:cap:docs:read:*" },
  "sees": [
    { "call": "Issue", "capability": ["urn:cap:x"], "sees": null },
    { "call": "IssueAs", "capability": ["urn:cap:x", "urn:cap:docs:read:-/a"],
      "sees": ["urn:cap:docs:read:-/a", "urn:cap:x"] }
  ],
  "under": [
    { "capability": ["urn:cap:docs:read:/a", "urn:cap:net:h"], "prefix": "urn:cap:docs:read:",
      "under": ["urn:cap:docs:read:/a"] },
    { "capability": null, "prefix": "urn:cap:docs:read:", "under": [] }
  ],
  "rules": [
    { "capability": ["urn:cap:docs:read:/home", "urn:cap:docs:read:-/home/secret"],
      "path": "/home/secret/key",
      "expect": { "denied": "capability does not grant `read` on `/home/secret/key`" } },
    { "capability": ["urn:cap:other"], "path": "/home/a",
      "expect": { "floor": ["urn:cap:docs:read:*"], "entered": false } }
  ]
}
```

- `sees` pins what each call form hands the handler. `null` means root. The scopes are
  sorted, as the wire sorts them.
- `under` pins the one convenience that goes beyond core.
- `rules` pins the reference ACL end to end. Each row's outcome is resolved, a handler
  denial with its text, or a floor denial with `lacking` and `entered: false`. Rows
  cover a directory grant, a sibling, a deeper exclusion, a tie on depth, root, and a
  holder of no read grant.

Each face runs all three arrays through its own `Space` / server, using an echo handler
(it returns the sorted scopes it sees) and the reference ACL handler. `vector-drift`
compares the copies with core's, as it does for the existing vector files.

## Proposed follow-up items

1. **Python: add `current_caller()`, `Caller` and `with_caller` to `ikigai.serve`.**
   Set a context variable in `Space._resolve` (root for `Issue`, the carried capability
   otherwise). Raise `LookupError` outside a served call. Turn the strict `xfail` in
   `tests/test_handler_capability.py` into a passing test and move the proof's
   `CallerSpace` out of the test. Document the two author rules in the README.
2. **TypeScript: pass a `Caller` as the third handler argument in ikigai-ts.**
   Add `Caller` and `callerOf`, widen `Handler` and `ExistsHandler`, and thread the
   capability from `#resolve` through `answer` to `runHandler`. Add a served test that
   `Issue` shows root and `IssueAs` the carried scopes, plus the reference path-ACL test.
3. **Core: compute `handler_capability_vectors.json` in ikigai-vocab.**
   Add an endpoint that implements the reference rule from `inv.capability` and issue
   every row through a kernel. Pin the file against drift with core's own test, as with
   `capability_vectors.json`.
4. **Both faces: consume the handler-capability vectors.**
   Copy the file verbatim, run `sees`, `under` and `rules` through the served path, and
   add it to `vector-drift`.
5. **Both faces: document issuing sub-requests under the caller's capability.**
   A handler that calls back into a host should `IssueAs` its caller's capability (or a
   weaker one) by default, the polyglot analogue of `Invocation::issue`. This keeps a peer
   from acting as a confused deputy with its own connection's authority.
