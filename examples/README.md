# REST faces over the ikigai wire client

Three small web apps, one pattern: **a framework handler is a thin face over
`kernel.source(...)`**. The framework does HTTP (routing, parameter
validation, status codes); ikigai does resolution (naming, arguments,
caching, self-description). The demo endpoints are humble — hello / upper /
reverse — because the point is the wiring, not the endpoints.

| module | framework | the face it shows |
| --- | --- | --- |
| `litestar_app.py` | [Litestar](https://litestar.dev) | typed handlers — the signature is the contract, the closest Python analog to an ArgSpec |
| `falcon_app.py` | [Falcon](https://falcon.readthedocs.io) | the minimal end — bare ASGI resource classes, `get_param(required=True)` |
| `fasthtml_app.py` | [FastHTML](https://fastht.ml) | the hypermedia face — server-driven HTML + htmx, a form per endpoint, an HTML catalog |

Every app serves the same surface:

- `GET /hello/{who}` → resolves `urn:py:hello`
- `GET /upper?text=…` → resolves `urn:py:upper`
- `GET /reverse?text=…` → resolves `urn:py:reverse`
- `GET /catalog` → `kernel.entries()` as JSON (HTML in FastHTML) — the app
  *discovers* what it can reach instead of hard-coding it
- a typed wire error picks its HTTP status (the wire v7 payoff, shared as
  `examples.error_status`): `DeniedError` → **403**, `NotFoundError` →
  **404**, `MissingArgumentError`/`InvalidArgumentError` → **400**,
  transient (`TimeoutError`/`UnavailableError`) → **503**, anything else
  (`UnresolvedError`, a handler fault) → **502** — each carrying the
  endpoint's own message; a `ConnectionLost` → **503** ("is the peer
  running?")

`examples/endpoints.py` is the endpoint set they resolve: three
`@endpoint`-decorated pure functions, `cacheable=True`, ArgSpecs declared.

## Setup

```sh
pip install -e '.[dev,examples]'
```

Each app reads the kernel socket path from `IKIGAI_SOCKET`, defaulting to
`ikigai.client.default_socket_path()` — the Rust CLI's per-user socket.

## Run mode 1: direct (pure Python, no Rust)

Point an app straight at a served Python space:

```sh
python -m examples.endpoints /tmp/py-examples.sock &
IKIGAI_SOCKET=/tmp/py-examples.sock uvicorn examples.litestar_app:app
# …or examples.falcon_app:app, or examples.fasthtml_app:app
curl localhost:8000/hello/Ada          # Hello, Ada!
curl 'localhost:8000/upper?text=roc'   # ROC   (X-Ikigai-Cache: MISS every time)
curl localhost:8000/catalog
```

There is no cache in this mode: the Python peer computes every request and
`X-Ikigai-Cache` stays `MISS` (`cacheable=True` only *marks* the result).

## Run mode 2: through the kernel (the same call, plus caching)

Put a Rust kernel in the middle and let IT own the topology — the REST call
then traverses **Python app → Rust kernel → mount → Python peer**, and the
kernel caches the pure results:

```sh
python -m examples.endpoints /tmp/py-examples.sock &
ikigai serve /tmp/kernel.sock --prefer urn:py:=/tmp/py-examples.sock &
IKIGAI_SOCKET=/tmp/kernel.sock uvicorn examples.litestar_app:app
curl -i 'localhost:8000/upper?text=roc'   # X-Ikigai-Cache: MISS
curl -i 'localhost:8000/upper?text=roc'   # X-Ikigai-Cache: HIT — the kernel cached the peer's result
```

Nothing in the app changed — same code, same IRIs; only `IKIGAI_SOCKET`
moved. `/catalog` now lists the kernel's whole space (`urn:fn:*`,
`urn:kernel:*`, …) with the `urn:py:*` entries composed in. The Litestar app
surfaces the cache verdict as an `X-Ikigai-Cache` response header (Falcon
too); the FastHTML app prints it in each result fragment.

## Connection lifecycle

Each app opens **one** wire connection at startup (Litestar `lifespan`,
Falcon lifespan middleware, FastHTML lifespan) and closes it at shutdown —
never per request. A single `ikigai.aio.AsyncClient` serializes concurrent
requests internally (the wire is strictly call/reply per connection); that is
a deliberate simplification at example scale — a pool would be the next step,
not a different pattern.

## Tests

`tests/test_examples_*.py` smoke-test each app with its framework's own test
client against an in-process `ikigai.serve` space on a temp socket — the
whole stack (HTTP → handler → wire → served space) is pure Python, so it
runs on CI with no Rust binary. The tests skip themselves when the
`examples` extras are not installed.
