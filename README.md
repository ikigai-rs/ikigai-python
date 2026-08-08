# ikigai-python

A **pure-Python** (stdlib-only) client and servable peer for the
[ikigai](https://github.com/ikigai-rs) wire protocol over Unix domain sockets.
This is **L0** of the polyglot ladder: zero Rust, zero core changes — a Python
process can *drive* a running ikigai kernel, and a Python process can *be*
resources that a Rust host mounts.

A binding = client + servable peer space; the module mechanism IS
mount-over-wire.

Wire protocol version: **5** (`ikigai.PROTOCOL_VERSION`). Developed and
integration-tested against `ikigai-cli 0.1.9` (the binary has no `--version`
flag yet; the version comes from the install metadata).

## Install

```sh
pip install .          # zero runtime dependencies (socket/asyncio/struct only)
```

Dev setup: `pip install -e '.[dev]'`, then `ruff check .`,
`ruff format --check .`, `pytest`. The integration tests drive the real
`ikigai` binary and skip themselves when it is not on `PATH`.

## Client (the notebook front door)

```python
import ikigai

k = ikigai.connect()          # default socket path, same as the Rust CLI
rep = k.source("urn:fn:toUpper", **{"in": "hi"})
rep.text                      # "HI"
rep.media_type                # "text/plain;charset=utf-8"
rep.cache_status              # how the server's cache answered (HIT/MISS/UNCACHEABLE)
k.sink("urn:file:notes.txt", "content goes as the `content` arg")
k.exists("urn:file:notes.txt")  # "true" — the file the sink just wrote
# NB exists still routes through the endpoint, so a function endpoint wants its
# required args: k.exists("urn:fn:toUpper", **{"in": "hi"})
k.meta("urn:fn:toUpper")      # self-description, text/turtle by default
k.describe("urn:fn:toUpper")  # the JSON Meta face, parsed — ArgSpecs and all
k.entries()                   # the catalog: [SpaceEntry(pattern, endpoint, origin)]
k.is_cached("urn:fn:toUpper", **{"in": "hi"})
k.source_traced("urn:fn:toUpper", **{"in": "hi"})   # (rep, [TraceEvent…])
k.close()                     # or use it as a context manager
```

Notes:

- `in` is a Python keyword, so pass it as `**{"in": ...}` (or name your own
  endpoint arguments something friendlier).
- `connect(capability=ikigai.Capability.scoped([...]))` sends requests as
  `Call::IssueAs` under that capability; the server clamps it to the
  principal the channel authenticated.
- Errors surface as `ikigai.EndpointError` carrying the server's error string
  (`endpoint error: ` prefix stripped, as the Rust wire clients do). A dead
  socket raises `ikigai.ConnectionLost`; a hung server trips the read
  deadline (default 300 s — long resolutions are silent, so silence is not
  proof of death; same rationale as the Rust client).

`ikigai.aio` exposes the same surface as `async` methods over asyncio
streams, sharing the same codec:

```python
from ikigai import aio

k = await aio.connect()
rep = await k.source("urn:fn:toUpper", **{"in": "hi"})
await k.close()
```

## Serve (the peer-module seed)

```python
from ikigai import serve, endpoint

@endpoint("urn:py:hello", summary="Greet someone")
def hello(who: str, greeting: str = "Hello") -> str:
    return f"{greeting}, {who}!"

serve([hello], "/tmp/py.sock")   # blocks; speaks the wire protocol
```

**The signature is the contract.** With no `args=` list the ArgSpecs are
derived from the function signature:

- `who: str` → required, `xsd:string`; `greeting: str = "Hello"` → optional
  with that default. `int` → `xsd:integer`, `float` → `xsd:double`, `bool` →
  `xsd:boolean`, `bytes` → accepted with no class (raw bytes).
- Incoming wire text is **coerced back to the annotated type** before the
  handler runs (`times="3"` arrives as `int` 3, `loud="true"` as `True` —
  the REPL's `true`/`false` convention); a value that will not coerce is an
  endpoint error, not a handler crash.
- `typing.Literal["fast", "slow"]` → `one_of` (enforced at invocation).
- `Optional[T]` / `T | None` → optional; absent without a default, the
  handler receives `None`.
- `Annotated[str, "the name to greet"]` → the per-argument summary.
- A trailing underscore maps a reserved word onto the wire: `def rev(in_:
  str)` declares and receives the argument `in` (PEP 8's own convention) —
  no `**kwargs` workaround needed.
- Unannotated parameters are accepted with no class — gradual typing,
  gradually rewarded; annotations are never *required*.

An explicit `args=` list of spec dicts still works unchanged and wins
wholesale over the signature (no merging; handlers then receive the wire
text uncoerced, exactly as before). A name mismatch between the explicit
list and the signature raises at decoration time.

Then from a Rust host:

```sh
ikigai --mount urn:py:=/tmp/py.sock -c 'source urn:py:hello who=Ada'
# Hello, Ada!
ikigai --mount urn:py:=/tmp/py.sock -c list
# urn:py:hello  → hello   [/tmp/py.sock]
```

Or run the packaged demo: `python -m ikigai.demo [socket-path]`.

What a served endpoint gets for free, because its describe face is real:

- **Named-arg routing**: the host engine fetches the JSON Meta face and
  routes `who=Ada` by the declared ArgSpecs — names, `required`/optional,
  `class` (XSD datatype or rdfs:Class IRI), `default`, `one_of`.
- **Catalog membership**: `list` on the host shows the Python endpoints with
  their mount origin.
- **Host-side caching**: declare `cacheable=True` on a pure function and the
  representation crosses the wire with `Expiry::Never` — the *host* kernel
  caches it (this peer keeps no cache; `IsCached` answers false).
- **Tracing**: a traced resolution through the mount gets a span for the
  Python invocation stitched into the host's execution tree.
- Meta faces: `text/turtle` (default — skolemized `ik:` graph, no blank
  nodes), `text/plain`, `application/json`.

### Alias mounts strip the prefix (important)

`--mount urn:py:=<socket>` is an **alias** mount: the host rewrites
`urn:py:hello` → `urn:hello` before forwarding, and re-prefixes catalog
patterns coming back. This server therefore answers **both** the declared IRI
and its alias-stripped form, and by default lists the alias-stripped form in
`entries` so an alias mount's catalog reads correctly. If you serve for an
`--override urn:py:=<socket>` mount (IRIs forwarded verbatim) pass
`strip_alias=False` — with the default, an override's catalog view of this
peer is empty (invocation still works); with `strip_alias=False` under an
*alias* mount, patterns double-prefix in `list` (invocation still works).
One `entries` answer cannot serve both mount modes correctly at once.

### Handlers

- Return `str` or `bytes` (encoded with the endpoint's declared `output`
  media type), a `(value, media_type)` tuple, or a full
  `ikigai.Representation`.
- A raised exception crosses the wire as `endpoint error: …` — never a hang.
- Missing required arguments are reported with the exact error text the Rust
  kernel uses, so the host-side experience is native.
- Arguments arrive utf-8-decoded (bytes if not valid utf-8). By-reference
  arguments (`ArgRef::Reference`/`Content`) are refused loudly: an L0 peer
  has no back-channel to the host to dereference them.

## Examples: REST faces over the client

`examples/` shows three web frameworks built on this client — Litestar
(typed handlers), Falcon (bare ASGI), FastHTML (hypermedia/htmx) — each a
thin face over `kernel.source(...)`, with a browsable catalog and wire
errors mapped to 502/503. They run pure-Python against
`python -m examples.endpoints`, or through a Rust kernel to pick up its
caching unchanged. See `examples/README.md`; install with
`pip install -e '.[dev,examples]'`.

## Security posture

A UDS peer trusts its connections: the socket is `0600`, and both the Python
server and the Rust server refuse peers whose kernel-verified UID (SO_PEERCRED
/ LOCAL_PEERCRED) differs from their own. A capability carried on
`IssueAs`/`IssueTraced` is accepted and surfaced (e.g. in trace spans) but
**not enforced per-scope** — capability-on-the-wire for IPC is a known TODO on
the Rust side too; do not treat a Python peer as a capability boundary.

## Wire-protocol notes (for implementors)

`ikigai.wire` mirrors `ikigai-wire` (Rust) field-for-field; its docstrings
record the layout. Highlights that a public ABI document should state:

- Framing: `u32` **big-endian** length + [postcard](https://postcard.jamesmunns.com)
  payload; 64 MiB frame cap, checked before allocation.
- **No version handshake on the wire.** `PROTOCOL_VERSION` (5) is a
  compile-time constant; a mismatch surfaces as a decode failure. This
  package pins v5 and raises `ProtocolError` naming its version on any
  undecodable message.
- Enum discriminants are the **declaration index** as a varint —
  `Verb::Source` is `0` on the wire even though it is declared
  `#[repr(u8)] Source = 1` (those codes are only for identity hashing).
- `ContentId` crosses as the *string* `b3:<hex>` (serde `into = "String"`),
  not 32 raw bytes.
- `Representation.threads` (golden threads) is `#[serde(skip)]` — cache
  provenance never crosses the wire; `expiry` does, and drives host caching.
- Map/set order is Rust `BTreeMap`/`BTreeSet` order: lexicographic over
  UTF-8 bytes.

## License

MIT OR Apache-2.0, at your option.
