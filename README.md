# ikigai-python

A **pure-Python** (stdlib-only) client and servable peer for the
[ikigai](https://github.com/ikigai-rs) wire protocol over Unix domain sockets.
This is **L0** of the polyglot ladder: zero Rust, zero core changes — a Python
process can *drive* a running ikigai kernel, and a Python process can *be*
resources that a Rust host mounts.

A binding = client + servable peer space; the module mechanism IS
mount-over-wire.

Wire protocol version: **6** (`ikigai.PROTOCOL_VERSION`): the connection
opens with a version hello each way (see the wire-protocol notes below), and
pre-v6 peers are tolerated for one version, until v7. Developed and
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

@endpoint("urn:py:hello", summary="Greet someone",
          args=[{"name": "who", "required": True,
                 "class": "http://www.w3.org/2001/XMLSchema#string"}])
def hello(who: str) -> str:
    return f"Hello, {who}!"

serve([hello], "/tmp/py.sock")   # blocks; speaks the wire protocol
```

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
and its alias-stripped form. Since wire v6 each connection's hello declares
its mount mode, and `entries` answers accordingly *per connection*: an alias
mount sees the stripped patterns, a verbatim client (plain `--connect`,
`--override`, `--prefer`) sees the declared IRIs — from the same server, at
the same time. `strip_alias` remains only as the default for legacy (≤ v5)
clients that send no hello: the default (`True`) reads correctly under an
alias mount; pass `strip_alias=False` if the legacy peers you expect are
override/verbatim. Either way invocation always works — only the catalog
view is affected.

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
- **Version hello (since v6).** The first frame each way is
  `b"IKWH"` + `u32` big-endian version + `u8` mode — deliberately *not*
  postcard, because the codec whose version is being negotiated must not be
  needed to negotiate it. Readers ignore trailing bytes; that is the
  extension mechanism. The mode byte (0 = verbatim, 1 = alias mount) tells a
  served peer how its mounter addresses it. A version mismatch is a clean
  error naming both sides instead of garbled postcard. Legacy (≤ v5) peers
  are tolerated for one version, until v7: a server that hangs up on the
  hello is taken to be ≤ v5 and the client reconnects without one (with a
  warning; `Client.server_version` is `None` for such a server), and a first
  frame without the magic is a ≤ v5 client's `Call`, served in legacy mode
  (with a warning). This package speaks v6 (`ikigai.PROTOCOL_VERSION`) and
  still raises `ProtocolError` naming its version on any undecodable
  message.
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
