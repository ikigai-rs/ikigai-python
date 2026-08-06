"""Integration against the installed Rust host, both directions.

Skips cleanly when the ``ikigai`` binary is absent (CI has no Rust host;
these run locally against ``~/.cargo/bin/ikigai``).
"""

from __future__ import annotations

import shutil
import subprocess
import threading
import time

import pytest

import ikigai
from ikigai.serve import Server, endpoint

IKIGAI = shutil.which("ikigai")

pytestmark = pytest.mark.skipif(IKIGAI is None, reason="no `ikigai` binary on PATH")


def run_repl(*commands: str, mount: str | None = None, timeout: float = 60.0) -> str:
    argv = [IKIGAI]
    if mount:
        argv += ["--mount", mount]
    for command in commands:
        argv += ["-c", command]
    done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    assert done.returncode == 0, f"{argv} failed:\n{done.stdout}\n{done.stderr}"
    # Cache annotations and the batch tally go to stderr; return both streams.
    return done.stdout + done.stderr


# -- direction 1: Python serves, the Rust host mounts ----------------------


@endpoint(
    "urn:py:hello",
    summary="Greet someone",
    args=[
        {
            "name": "who",
            "required": True,
            "summary": "the name to greet",
            "class": "http://www.w3.org/2001/XMLSchema#string",
        }
    ],
)
def hello(who: str) -> str:
    return f"Hello, {who}!"


@endpoint("urn:py:reverse", summary="Reverse a string", args=["in"], cacheable=True)
def reverse(**kwargs) -> str:
    return kwargs["in"][::-1]


@pytest.fixture
def python_peer(socket_dir):
    path = socket_dir / "py.sock"
    server = Server([hello, reverse], path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield path
    server.shutdown()
    thread.join(timeout=5)


def test_rust_host_sources_a_python_endpoint(python_peer):
    out = run_repl("source urn:py:hello who=Ada", mount=f"urn:py:={python_peer}")
    assert "Hello, Ada!" in out


def test_rust_host_lists_python_endpoints_with_origin(python_peer):
    out = run_repl("list", mount=f"urn:py:={python_peer}")
    assert "urn:py:hello" in out
    assert "hello" in out
    assert str(python_peer) in out  # the mount origin, shown per binding


def test_rust_host_caches_a_cacheable_python_result(python_peer):
    # Expiry::Never crosses the wire; the HOST kernel caches the peer's result.
    out = run_repl(
        "source urn:py:reverse in=abc",
        "source urn:py:reverse in=abc",
        mount=f"urn:py:={python_peer}",
    )
    assert "cba" in out
    assert "1 cached · 1 computed" in out


def test_rust_host_describes_a_python_endpoint(python_peer):
    out = run_repl("describe urn:py:hello", mount=f"urn:py:={python_peer}")
    assert "<urn:ikigai:endpoint:hello>" in out
    assert 'ik:inputName "who"' in out


def test_python_error_text_reaches_the_rust_user(python_peer):
    argv = [
        IKIGAI,
        "--mount",
        f"urn:py:={python_peer}",
        "-c",
        "source urn:py:hello",  # missing required `who`
    ]
    done = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    combined = done.stdout + done.stderr
    assert "missing required argument `who`" in combined


# -- direction 2: the Rust host serves, Python connects --------------------


@pytest.fixture
def rust_server(socket_dir):
    path = socket_dir / "kernel.sock"
    process = subprocess.Popen(
        [IKIGAI, "serve", str(path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 15
        while not path.exists():
            if time.monotonic() > deadline or process.poll() is not None:
                pytest.fail("ikigai serve did not come up")
            time.sleep(0.1)
        yield path
    finally:
        process.terminate()
        process.wait(timeout=10)


def test_python_client_drives_the_rust_kernel(rust_server):
    with ikigai.connect(rust_server) as k:
        rep = k.source("urn:fn:toUpper", **{"in": "hi"})
        assert rep.text == "HI"
        assert rep.media_type.startswith("text/plain")
        entries = k.entries()
        assert any(e.endpoint == "toUpper" for e in entries)
        description = k.describe("urn:fn:toUpper")
        assert description["id"] == "toUpper"
        assert any(i["name"] == "in" for i in description["inputs"])


def test_python_client_sees_the_rust_cache(rust_server):
    with ikigai.connect(rust_server) as k:
        first = k.source("urn:fn:toUpper", **{"in": "cache me"})
        assert first.cache_status == ikigai.CacheStatus.MISS
        second = k.source("urn:fn:toUpper", **{"in": "cache me"})
        assert second.cache_status == ikigai.CacheStatus.HIT
        assert k.is_cached("urn:fn:toUpper", **{"in": "cache me"})


def test_python_client_traces_the_rust_kernel(rust_server):
    with ikigai.connect(rust_server) as k:
        rep, events = k.source_traced("urn:fn:toUpper", **{"in": "hi"})
        assert rep.text == "HI"
        assert any(e.target == "urn:fn:toUpper" for e in events)


def test_rust_error_string_crosses_to_python(rust_server):
    with ikigai.connect(rust_server) as k:
        with pytest.raises(ikigai.EndpointError, match="no endpoint resolved for urn:fn:nope"):
            k.source("urn:fn:nope")
