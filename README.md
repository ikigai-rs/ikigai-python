# ikigai-python

A **pure-Python** (stdlib-only) client and servable peer for the
[ikigai](https://github.com/ikigai-rs) wire protocol over Unix domain sockets.
This is **L0** of the polyglot ladder: zero Rust, zero core changes — a Python
process can *drive* a running ikigai kernel, and a Python process can *be*
resources that a Rust host mounts (`ikigai --mount urn:py:=<socket>`).

A binding = client + servable peer space; the module mechanism IS
mount-over-wire.

Wire protocol version: **5** (see `ikigai.PROTOCOL_VERSION`). Tested against
`ikigai-cli 0.1.9`.

## Install

```sh
pip install .          # zero runtime dependencies (socket/asyncio/struct only)
```

## Client (the notebook front door)

```python
import ikigai

k = ikigai.connect()                       # default socket path, same as the Rust CLI
rep = k.source("urn:fn:toUpper", **{"in": "hi"})
rep.text                                   # "HI"
rep.media_type                             # "text/plain;charset=utf-8"
k.entries()                                # the catalog: [SpaceEntry(pattern, endpoint, origin)]
k.describe("urn:fn:toUpper")               # Meta face, parsed JSON
k.close()                                  # (context-manager support too)
```

`ikigai.aio` exposes the same surface as `async` methods over asyncio streams,
sharing the same codec.

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
```

## Security posture

A Unix-domain-socket peer trusts its connections: the socket directory is
`0700`, the socket `0600`, and the server refuses peers whose kernel-verified
UID differs from its own — the same posture as the Rust IPC transport.
Capability-on-the-wire for IPC is a known Rust-side TODO; a carried capability
is accepted (and clamped to nothing narrower than transport trust) but not yet
enforced per-scope here.

## License

MIT OR Apache-2.0, at your option.
