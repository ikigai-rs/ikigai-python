# ikigai-python

A **pure-Python** (stdlib-only) client and servable peer for the
[ikigai](https://github.com/ikigai-rs) wire protocol over Unix domain sockets.
This is **L0** of the polyglot ladder: zero Rust, zero core changes — a Python
process can *drive* a running ikigai kernel, and a Python process can *be*
resources that a Rust host mounts.

A binding = client + servable peer space; the module mechanism IS
mount-over-wire.

Wire protocol version: **7** (`ikigai.PROTOCOL_VERSION`): the connection
opens with a version hello each way — REQUIRED since v7 (the pre-v6
tolerances are gone) — and failures cross the wire **typed** (see the
wire-protocol notes below). A v6 peer still fails cleanly: the hello itself
names both versions.

## Install

```sh
pip install .          # zero runtime dependencies (socket/asyncio/struct only)
```

Dev setup: `pip install -e '.[dev]'`, then `ruff check .`,
`ruff format --check .`, `pytest`. The integration tests drive the real
`ikigai` binary and skip themselves when it is not on `PATH`.

### You also need a host, and it must be recent enough

This package only speaks to a running kernel; it binds nothing itself. Every
example below names resources in the **`urn:iki:`** namespace, which the Rust
host adopted in **`ikigai-cli` 0.1.18**. So:

> **Requires `ikigai-cli` >= 0.1.18.** Nothing mechanical checks this — Python
> packaging cannot express a floor on a Rust binary — so it is stated here
> instead. On an older host every example in this README fails with
> `no endpoint resolved for urn:iki:fn:toUpper`, and *that message is the only
> symptom*: the name is simply unknown there. Older hosts used `urn:fn:`.

```sh
cargo install ikigai-cli --locked     # NOT `cargo install ikigai` — that is an
                                      # unrelated crate by another author; ours
                                      # publishes as `ikigai-cli` and installs a
                                      # binary named `ikigai`.
ikigai -c 'source urn:iki:fn:toUpper in="hi"'    # HI  ⇒ your host is new enough
```

0.1.18 also aliases the old spelling, so `urn:fn:toUpper` still resolves there
— but the alias **canonicalizes before anything observes the name**. A request
for `urn:fn:nope` comes back as `no endpoint resolved for urn:iki:fn:nope`, and
trace events report `urn:iki:fn:toUpper` whichever spelling you sent. Code that
matches on returned IRIs must expect the canonical form.

## Client (the notebook front door)

```python
import ikigai

k = ikigai.connect()          # default socket path, same as the Rust CLI
rep = k.source("urn:iki:fn:toUpper", **{"in": "hi"})
rep.text                      # "HI"
rep.media_type                # "text/plain;charset=utf-8"
rep.cache_status              # how the server's cache answered (HIT/MISS/UNCACHEABLE)
k.sink("urn:file:notes.txt", "content goes as the `content` arg")
k.exists("urn:file:notes.txt")  # "true" — the file the sink just wrote
# NB exists still routes through the endpoint, so a function endpoint wants its
# required args: k.exists("urn:iki:fn:toUpper", **{"in": "hi"})
k.meta("urn:iki:fn:toUpper")      # self-description, text/turtle by default
k.describe("urn:iki:fn:toUpper")  # the JSON Meta face, parsed — ArgSpecs and all
k.entries()                   # the catalog: [SpaceEntry(pattern, endpoint, origin)]
k.is_cached("urn:iki:fn:toUpper", **{"in": "hi"})
k.source_traced("urn:iki:fn:toUpper", **{"in": "hi"})   # (rep, [TraceEvent…])
k.close()                     # or use it as a context manager
```

Notes:

- `in` is a Python keyword, so pass it as `**{"in": ...}` (or name your own
  endpoint arguments something friendlier).
- `connect(capability=ikigai.Capability.scoped([...]))` sends requests as
  `Call::IssueAs` under that capability; the server clamps it to the
  principal the channel authenticated.
- Errors surface **typed** (wire v7): the server's failure crosses with its
  taxonomy intact and is raised as the matching subclass of
  `ikigai.EndpointError` — `UnresolvedError`, `MissingArgumentError`,
  `InvalidArgumentError` (with `.name`/`.detail`), `DeniedError`,
  `NotFoundError`, `ikigai.TimeoutError` (also a `builtins.TimeoutError`),
  `UnavailableError`. `.message` is the endpoint's own message; `.transient`
  is `True` only for Timeout/Unavailable (re-issuing may succeed — what
  retry/failover logic gates on). A plain `except ikigai.EndpointError`
  still catches everything. A dead socket raises `ikigai.ConnectionLost`; a
  hung server trips the read deadline (default 300 s — long resolutions are
  silent, so silence is not proof of death; same rationale as the Rust
  client).

`ikigai.aio` exposes the same surface as `async` methods over asyncio
streams, sharing the same codec:

```python
from ikigai import aio

k = await aio.connect()
rep = await k.source("urn:iki:fn:toUpper", **{"in": "hi"})
await k.close()
```

For web apps, `aio.lifespan(path)` packages the connect/publish/close cycle
as an ASGI-style lifespan — one kernel connection for the app's lifetime,
published on `app.state.kernel`. It is usable directly by Litestar
(`lifespan=[aio.lifespan(path)]`) and adapts in one line for
FastHTML/Starlette and Falcon; the docstring carries a snippet per
framework.

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

### Families and verbs

One door can answer a whole **family** of names, and more than one **verb**.
`family()` declares a URI template; each verb gets its own handler and its
own contract (core's per-verb `ActionSpec` form):

```python
from ikigai import family, serve, NotFoundError

cell = family("urn:iki:tutorial:ttt:stored:{x}:{y}", id="ttt-stored")
marks = {}

@cell.source(cacheable=True)
def read(x: int, y: int) -> str:
    if (x, y) not in marks:
        raise NotFoundError(f"nothing has been played at {x},{y}")
    return marks[(x, y)]

@cell.sink
def play(x: int, y: int, content: str) -> str:
    marks[(x, y)] = content.strip()
    return "ok"

@cell.delete
def clear(x: int, y: int) -> str:
    marks.pop((x, y), None)
    return "ok"

serve([cell], "/tmp/ttt.sock")
```

- **Templates** mirror `ikigai_core::UriTemplate` exactly (`ikigai.UriTemplate`;
  the rule is in `src/ikigai/template.py`). Level 1 `{var}` only; a variable
  captures up to the *leftmost* occurrence of the literal after it, a trailing
  one takes the rest, and every capture is non-empty. The **first declared door
  that matches wins**, as in an `EndpointSpace` — declare the specific before the
  general (a door an earlier template swallows is unreachable, as in Rust; the
  same pattern declared twice is refused).
  `@endpoint` takes a template too, for a Source-only family.
- **Bindings** arrive as the handler parameters of the same name, typed by
  their annotations, and are described as `ik:source "binding"` inputs of every
  action. The catalog lists the **template**, so the host's `list`, topology
  and selection see the family. A binding is part of the resource's *name*, so
  an `int` binding accepts one spelling per integer: `01`, `+1`, `-0` are
  `InvalidArgument` (two spellings would be two cache entries and two golden
  threads over one piece of state).
- **Verbs**: `.source`, `.sink`, `.delete`, `.exists`, bare or with
  `(summary=…, args=…, output=…, requires=…)`. A Sink's body arrives as
  `content` — the host engine routes every piped or trailing value there, so
  every Sink declares a required `content` even if its handler does not ask.
  `source`/`exists` may be `cacheable=True`; a Sink or Delete answer never is.
  **Exists** defaults to "Source would succeed" (`NotFoundError` → `false`,
  cacheable exactly when Source is); a family with no Source refuses it.
  An undeclared verb is refused, naming the verbs the door does answer.
- **Invalidation is the host's**: a Rust host (ikigai-core ≥ 0.1.73) cuts the
  target's golden thread after every Sink or Delete it forwards, so the cached
  read above needs no code here. `tests/test_integration.py` proves it
  through the installed host.
- **Mount a family with `--override`**, which forwards IRIs unchanged:
  `ikigai --override urn:iki:tutorial:ttt:stored:=/tmp/ttt.sock`. An alias
  `--mount` works only at the first segment (`--mount urn:iki:=…`), because
  this server can strip only what it can guess — see below.

`examples/tictactoe_store.py` is the ikigai book's tic-tac-toe atom served
this way — the Rust `stored_cell`'s contract, message for message — for a
Rust host to mount under everything else the game computes.

### Alias mounts strip the prefix (important)

`--mount urn:py:=<socket>` is an **alias** mount: the host rewrites
`urn:py:hello` → `urn:hello` before forwarding, and re-prefixes catalog
patterns coming back. This server therefore answers **both** the declared IRI
and its alias-stripped form (`urn:py:echo/{m}` also answers as `urn:echo/{m}`).
It strips exactly the first segment — the only prefix a peer can guess, since
the hello says THAT the mount aliases but not at which prefix — so a deeper
alias prefix reaches nothing here; use `--override` for that. Each connection's hello declares its mount mode
(the hello is required since wire v7), and `entries` answers accordingly
*per connection*: an alias mount sees the stripped patterns, a verbatim
client (plain `--connect`, `--override`, `--prefer`) sees the declared IRIs
— from the same server, at the same time. The `strip_alias` constructor
default now only governs direct `Space.entries()` calls. Either way
invocation always works — only the catalog view is affected.

### Handlers

- Return `str` or `bytes` (encoded with the endpoint's declared `output`
  media type), a `(value, media_type)` tuple, or a full
  `ikigai.Representation`.
- Failures cross the wire **typed** (wire v7): an unknown IRI is
  `Unresolved`, a missing required argument `MissingArgument`, an unusable
  value `InvalidArgument`, and a raised exception an `Endpoint` error —
  never a hang, and the host rebuilds the same variant natively.
- A handler may **raise the taxonomy deliberately** — `raise
  ikigai.NotFoundError("no such row")`, `DeniedError`, `TimeoutError`,
  `UnavailableError` — and the variant crosses intact: the far side's HTTP
  face answers 404/403/503 instead of a blanket 502, and transient failures
  stay transient for retry/failover logic.
- Arguments arrive utf-8-decoded (bytes if not valid utf-8). By-reference
  arguments (`ArgRef::Reference`/`Content`) are refused loudly (as
  `InvalidArgument`): an L0 peer has no back-channel to the host to
  dereference them.

## Tic-tac-toe, all in Python except the middle

The ikigai book builds tic-tac-toe as resources (the tutorial's
`crates/tic-tac-toe`), and ships its HTML as **template resources** so that
any host in any language can render the same board. This package plays both
ends of that game around a Rust kernel:

- **the state**: `examples/tictactoe_store.py` serves the stored cell,
  `urn:iki:tutorial:ttt:stored:{x}:{y}` — the only state the game has;
- **the rendering**: `examples/tictactoe_app.py` is a standard-library web
  app (`http.server`) that fills the game's templates, in Python, from the
  host's raw resources — `template:{name}`, `cell:{x}:{y}`, `winner`,
  `turn` — and serves the page, the vendored htmx and the book's stylesheet;
- **the middle**: `ttt-host` (the tutorial's `crates/ttt-host`) holds the
  rules, the lines, the board and the turn, and does the resolution, the
  composition, the caching and the invalidation.

```sh
python -m examples.tictactoe_store /tmp/ttt/store.sock &
ttt-host --socket /tmp/ttt/host.sock --game py=/tmp/ttt/store.sock &
python -m examples.tictactoe_app --socket /tmp/ttt/host.sock
# open http://127.0.0.1:8072/game/py/
```

(Keep socket paths short: macOS allows 104 bytes. `ttt-host` also serves the
same board itself on port 8070; the Deno face's app uses 8071, this one
8072.)

The app's fragments are byte-for-byte the host's own Rust `view:board` and
`view:status`: `tests/test_tictactoe_app.py` plays one game through the app
and a twin game through the Rust views and compares every board, status and
reply, through a won game, refusals and a draw — and the page and its three
static files against `ttt-host`'s own HTTP face (it skips when `ttt-host` is
not installed). Every other request to a view is answered as `ttt-host`
answers it, status and body: a trailing slash, an unknown game, a wrong
method, a coordinate spelled `01` or `+1`. The test asks both of them the
same table of edge requests (ikigai-deno's rows, with this face's added) and
requires the same status, body, Content-Type and `Allow`. A path that is not
a view gets `404 not found`, because the app is not a proxy for the host's
other names. The renderer is the template format's tokenizer (it refuses a
malformed slot, as the Rust filler does), an escape table and three short
functions. It keeps no state and caches nothing, because every read it makes
is a cache hit in the host until a move cuts it.

Two honest limits, both about the kernel in the middle:

- **Always write through the host.** The app Sinks the host's `move:{x}:{y}`
  and `reset`, never the store. The host cuts a stored cell's golden thread
  when ITS kernel issues the write; a write straight to the Python store
  (another client of that socket) would leave the host serving the old board
  until something else cut it. There is no golden thread over the wire yet.
- **Render from raw resources, never compute the game.** The app asks the
  host for `winner` rather than working it out from the cells. A Python
  composite that read the cells back through the host and decided the winner
  itself would be a traccessor: an answer built from reads the host never saw
  it make, so the host could not track its dependencies or ever cut it.

## Examples: REST faces over the client

`examples/` shows three web frameworks built on this client — Litestar
(typed handlers), Falcon (bare ASGI), FastHTML (hypermedia/htmx) — each a
thin face over `kernel.source(...)`, with a browsable catalog and the wire's
typed errors mapped onto HTTP statuses: `DeniedError`→403,
`NotFoundError`→404, `MissingArgumentError`/`InvalidArgumentError`→400,
transient (`TimeoutError`/`UnavailableError`)→503, anything else→502, and
`ConnectionLost`→503. They run pure-Python against
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
- **Version hello (since v6, REQUIRED since v7).** The first frame each way
  is `b"IKWH"` + `u32` big-endian version + `u8` mode — deliberately *not*
  postcard, because the codec whose version is being negotiated must not be
  needed to negotiate it. Readers ignore trailing bytes; that is the
  extension mechanism. The mode byte (0 = verbatim, 1 = alias mount) tells a
  served peer how its mounter addresses it. A version mismatch is a clean
  error naming both sides instead of garbled postcard. The v6 one-version
  tolerances are gone: a server that hangs up on the hello is refused with
  the pre-v6 diagnosis (no legacy reconnect) — while a server that is merely
  *silent* is reported as hung or overloaded, never misdiagnosed as ancient
  — and a first frame without the magic is refused by the server. This
  package speaks v7 (`ikigai.PROTOCOL_VERSION`) and still raises
  `ProtocolError` naming its version on any undecodable message.
- **Typed errors (since v7).** A failure crosses as `Reply::ErrorTyped`
  (postcard discriminant 5) carrying the `WireError` enum — variants 0–7 in
  declaration order: `Unresolved(iri)`, `MissingArgument(name)`,
  `InvalidArgument{name, detail}`, `Endpoint(message)`, `Denied(message)`,
  `NotFound(message)`, `Timeout(message)`, `Unavailable(message)` — an
  append-only, wire-local mirror of `ikigai_core::Error` (a taxonomy
  addition is a wire-version event). Timeout/Unavailable are transient;
  the rest permanent. An unknown future variant degrades to the base
  `EndpointError`, loudly named. The flat `Reply::Error` string (variant 3)
  remains decodable but is no longer sent.
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
