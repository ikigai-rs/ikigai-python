"""Wire v9's ``Call::IssueWith`` (variant 5): the byte vectors from ikigai-cli 0.1.66's
hand-back, both directions; the server's run context (built at ITS OWN now from time
left); the client's rule: ``IssueWith`` only to a v9 peer and only when the run
carries a pool or a deadline, otherwise the exact pre-v9 bytes; and the real door
(``ikigai serve``, 0.1.66) answering a run and relaying the new error classes."""

import socket
import subprocess
import threading
import time

import pytest

import ikigai
from ikigai import wire
from ikigai.postcard import encode_varint
from ikigai.serve import Server, current_run, endpoint
from ikigai.wire import (
    Capability,
    Hello,
    Inline,
    IssueWith,
    IssueWithCall,
    Request,
    TraceContext,
    Verb,
    decode_hello,
    encode_hello,
)
from tests.rust_door import require_ikigai

VECTORS = [
    (IssueWith(), "000000"),
    (IssueWith(None, 2, 1500), "0001020 1dc0b".replace(" ", "")),
    (IssueWith(TraceContext(7, None), 2, 1500), "01070001020 1dc0b".replace(" ", "")),
]


@pytest.mark.parametrize(("context", "hexed"), VECTORS)
def test_issue_with_struct_vectors_both_directions(context, hexed):
    assert wire.encode_issue_with(context).hex() == hexed
    assert wire.decode_issue_with(bytes.fromhex(hexed)) == context


def test_a_parent_span_rides_inside_the_trace():
    context = IssueWith(TraceContext(7, 9), None, 0)
    raw = wire.encode_issue_with(context)
    assert raw.hex() == "0107010900" + "0100"
    assert wire.decode_issue_with(raw) == context


def test_a_truncated_or_overlong_issue_with_is_a_protocol_error():
    with pytest.raises(wire.ProtocolError):
        wire.decode_issue_with(bytes.fromhex("0001"))
    with pytest.raises(wire.ProtocolError):
        wire.decode_issue_with(bytes.fromhex("00000000"))


def _request(verb=Verb.SOURCE):
    return Request(verb, "urn:py:probe", {"x": Inline(b"1")})


def test_call_variant_5_is_request_capability_then_issue_with():
    call = IssueWithCall(_request(), Capability.root(), IssueWith(None, 2, 1500))
    raw = wire.encode_call(call)
    assert raw[0] == 5
    assert raw.endswith(bytes.fromhex("0001020 1dc0b".replace(" ", "")))
    # Exactly where IssueTraced puts its TraceContext.
    traced = wire.encode_call(wire.IssueTraced(_request(), Capability.root(), TraceContext(1)))
    assert raw[1 : -len(bytes.fromhex("000102 01dc0b"))] == traced[1:-2]
    assert wire.decode_call(raw) == call


def s(text: str) -> bytes:
    raw = text.encode()
    return encode_varint(len(raw)) + raw


ERROR_VECTORS = [
    (ikigai.DepthExceededError(65, "urn:x"), "05094105" + b"urn:x".hex()),
    (ikigai.RetryAfterError(30000, "x"), "050a01b0ea010178"),
    (ikigai.RetryAfterError(None, "x"), "050a000178"),
    (ikigai.OverBudgetError("x"), "050b0178"),
    (ikigai.RefusedError("x"), "050c0178"),
    (ikigai.UnsupportedError("x", Verb.SINK), "050d01010178"),
    (ikigai.UnsupportedError("x"), "050d000178"),
]


@pytest.mark.parametrize(("error", "hexed"), ERROR_VECTORS, ids=lambda v: str(v)[:24])
def test_error_vectors_both_directions(error, hexed):
    raw = bytes.fromhex(hexed)
    assert wire.encode_reply(wire.ErrorTypedReply(error), peer_version=9) == raw
    assert wire.decode_reply(raw) == wire.ErrorTypedReply(error)
    # The default is this side's own version, v9.
    assert wire.encode_reply(wire.ErrorTypedReply(error)) == raw


@pytest.mark.parametrize(("error", "hexed"), ERROR_VECTORS, ids=lambda v: str(v)[:24])
def test_a_v8_peer_gets_the_error_as_endpoint_display(error, hexed):
    sent = wire.encode_reply(wire.ErrorTypedReply(error), peer_version=8)
    assert sent == wire.encode_reply(
        wire.ErrorTypedReply(ikigai.EndpointError(error.display())), peer_version=8
    )
    assert sent[:2] == b"\x05\x03"


# -- the client's choice of Call --------------------------------------------------------


def test_build_issue_call_is_pre_v9_unless_the_run_carries_something():
    req = _request()
    cap = Capability(frozenset({"urn:cap:x"}))
    build = wire.build_issue_call
    assert build(req, None, server_version=9) == wire.Issue(req)
    assert build(req, cap, server_version=9) == wire.IssueAs(req, cap)
    trace = TraceContext(1)
    assert build(req, cap, server_version=9, trace=trace) == wire.IssueTraced(req, cap, trace)
    # A pool or a deadline, but a peer below v9 (or unknown): the same pre-v9 bytes.
    for version in (7, 8, None):
        assert build(req, None, server_version=version, retries=3) == wire.Issue(req)
        assert build(req, cap, server_version=version, deadline=time.monotonic() + 5) == (
            wire.IssueAs(req, cap)
        )
    # v9 with a pool: IssueWith, root capability when none was set.
    got = build(req, None, server_version=9, retries=3)
    assert got == IssueWithCall(req, Capability.root(), IssueWith(None, 3, None))


def test_the_deadline_crosses_as_time_left_never_an_instant():
    call = wire.build_issue_call(
        _request(), None, server_version=9, deadline=time.monotonic() + 1.5
    )
    left = call.context.time_left_ms
    assert 1000 <= left <= 1500  # an instant would be a monotonic reading, in the millions
    past = wire.build_issue_call(_request(), None, server_version=9, deadline=time.monotonic() - 5)
    assert past.context.time_left_ms == 0


def test_downgrade_call_for_an_older_door():
    req, cap = _request(), Capability.root()
    traced = IssueWithCall(req, cap, IssueWith(TraceContext(4, 2), 1, 10))
    assert wire.downgrade_call(traced, 8) == wire.IssueTraced(req, cap, TraceContext(4, 2))
    assert wire.downgrade_call(IssueWithCall(req, cap, IssueWith(None, 1, 10)), 7) == (
        wire.IssueAs(req, cap)
    )
    assert wire.downgrade_call(traced, 9) is traced


# -- the server ---------------------------------------------------------------------------

SEEN = []


@endpoint("urn:py:probe", summary="reports the run it serves")
def probe() -> str:
    run = current_run()
    SEEN.append(run)
    left = run.time_left()
    return f"retries={run.retries} left={'none' if left is None else round(left, 1)}"


def _raw(path, version):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(5)
    sock.connect(str(path))
    f = sock.makefile("rwb")
    wire.write_frame(f, encode_hello(Hello(version)))
    assert decode_hello(wire.read_frame(f)) == Hello(version)
    return sock, f


def _send(f, call):
    wire.write_frame(f, wire.encode_call(call))
    return wire.decode_reply(wire.read_frame(f))


@pytest.fixture
def served(socket_dir):
    path = socket_dir / "iw.sock"
    SEEN.clear()
    with Server([probe], path) as server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        yield path


def test_a_served_endpoint_sees_the_pool_and_a_deadline_at_the_servers_own_now(served):
    sock, f = _raw(served, 9)
    with sock, f:
        # A raw v9 frame, byte for byte: Request, Capability, then the IssueWith vector.
        call = IssueWithCall(_request(), Capability.root(), IssueWith(None, 2, 1500))
        before = time.monotonic()
        reply = _send(f, call)
    assert reply.representation.text == "retries=2 left=1.5"
    (run,) = SEEN
    assert run.retries == 2
    assert before + 1.4 <= run.deadline <= time.monotonic() + 1.5


def test_a_peer_that_sends_none_gets_todays_behavior(served):
    sock, f = _raw(served, 9)
    with sock, f:
        reply = _send(f, wire.Issue(_request()))
        reply2 = _send(f, IssueWithCall(_request(), Capability.root(), IssueWith()))
    assert reply.representation.text == "retries=None left=none"
    assert reply2.representation.text == "retries=None left=none"
    # And the run does not leak into the next call.
    assert current_run().retries is None and current_run().deadline is None


def test_issue_with_a_trace_answers_traced(served):
    sock, f = _raw(served, 9)
    with sock, f:
        call = IssueWithCall(_request(), Capability.root(), IssueWith(TraceContext(7), 1, None))
        reply = _send(f, call)
    assert isinstance(reply, wire.ResolvedTraced)
    assert reply.events[0].target == "urn:py:probe"


def test_issue_with_still_enforces_the_capability(served):
    sock, f = _raw(served, 9)
    with sock, f:
        # Meta needs nothing; a Sink to an undeclared verb under an empty capability is refused.
        call = IssueWithCall(_request(Verb.SINK), Capability(frozenset()), IssueWith(None, 1, 100))
        reply = _send(f, call)
    assert isinstance(reply, wire.ErrorTypedReply)


def test_v9_errors_cross_typed_to_a_v9_peer_and_flat_to_a_v8_peer(socket_dir):
    @endpoint("urn:py:slow", summary="rate limited")
    def slow() -> str:
        raise ikigai.RetryAfterError(30000, "x")

    path = socket_dir / "err.sock"
    with Server([slow], path) as server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        for version, expected in (
            (9, ikigai.RetryAfterError(30000, "x")),
            (8, ikigai.EndpointError("retry after 30000 ms: x")),
        ):
            sock, f = _raw(path, version)
            with sock, f:
                reply = _send(f, wire.Issue(Request(Verb.SOURCE, "urn:py:slow", {})))
            assert reply == wire.ErrorTypedReply(expected)


# -- the client, end to end -------------------------------------------------------------------


class RecordingServer:
    """A fake server that answers the hello at ``version`` and records each Call's raw frame."""

    def __init__(self, path, version):
        self.version = version
        self.frames: list[bytes] = []
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._listener.bind(str(path))
        self._listener.listen()
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self._listener.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        with conn, conn.makefile("rwb") as f:
            try:
                wire.read_frame(f)
                wire.write_frame(f, encode_hello(Hello(self.version)))
                while True:
                    self.frames.append(wire.read_frame(f))
                    rep = wire.Representation(b"ok", "text/plain")
                    wire.write_frame(f, wire.encode_reply(wire.Resolved(rep, wire.CacheStatus(0))))
            except (EOFError, OSError):
                return

    def close(self):
        self._listener.close()


def test_a_v9_client_against_a_v8_server_sends_issue_as(socket_dir):
    path = socket_dir / "v8.sock"
    server = RecordingServer(path, 8)
    cap = Capability(frozenset({"urn:cap:x"}))
    try:
        with ikigai.connect(path, capability=cap) as k:
            assert k.server_version == 8
            k.retries, k.deadline = 3, time.monotonic() + 5
            k.source("urn:py:probe")
        (frame,) = server.frames
        assert frame == wire.encode_call(
            wire.IssueAs(Request(Verb.SOURCE, "urn:py:probe", {}), cap)
        )
        assert frame[0] == 3
    finally:
        server.close()


def test_a_v9_client_sends_issue_with_only_when_the_run_carries_something(socket_dir):
    path = socket_dir / "v9.sock"
    server = RecordingServer(path, 9)
    try:
        with ikigai.connect(path) as k:
            assert k.server_version == 9
            k.source("urn:py:probe")  # nothing carried: the plain Issue
            k.retries = 3
            k.source("urn:py:probe")
            k.retries, k.deadline = None, time.monotonic() + 5
            k.source("urn:py:probe")
        plain, pooled, timed = (wire.decode_call(f) for f in server.frames)
        assert server.frames[0][0] == 0 and isinstance(plain, wire.Issue)
        assert server.frames[1][0] == 5
        assert pooled.context == IssueWith(None, 3, None)
        assert timed.context.retries is None and 4000 <= timed.context.time_left_ms <= 5000
    finally:
        server.close()


# -- the real Rust door -----------------------------------------------------------------------
#
# ``ikigai serve`` (ikigai-cli 0.1.66, the first release on wire v9) fronting a Python peer
# with ``--prefer``: a v9 client with a run, and the five new error classes as the door
# actually relays them. Skips without an ``ikigai`` binary, FAILS instead under
# ``--require-interop``; skips a door that does not speak v9, which is a door too old for
# these tests, not a defect.

#: The new failure classes (wire tags 9-13), each raised by its own peer endpoint.
DOOR_ERRORS = {
    "depth": ikigai.DepthExceededError(65, "urn:x"),
    "retry": ikigai.RetryAfterError(30000, "slow down"),
    "retry-unknown": ikigai.RetryAfterError(None, "slow down"),
    "budget": ikigai.OverBudgetError("spent"),
    "refused": ikigai.RefusedError("no thanks"),
    "unsupported": ikigai.UnsupportedError("not here"),
}


def _raiser(name, error):
    @endpoint(f"urn:py:err:{name}", summary=f"raises {type(error).__name__}")
    def raiser() -> str:
        raise error

    return raiser


@pytest.fixture
def door(request, socket_dir):
    """A real ``ikigai serve`` on a Unix socket, preferring ``urn:py:`` from a Python peer."""
    binary = require_ikigai(request)
    SEEN.clear()
    peer_path = socket_dir / "peer.sock"
    endpoints = [probe] + [_raiser(name, error) for name, error in DOOR_ERRORS.items()]
    with Server(endpoints, peer_path) as server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        path = socket_dir / "door.sock"
        process = subprocess.Popen(
            [binary, "serve", str(path), "--prefer", f"urn:py:={peer_path}"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            deadline = time.monotonic() + 15
            while not path.exists():
                if process.poll() is not None:
                    pytest.fail(f"ikigai serve exited: {process.stderr.read()}")
                if time.monotonic() > deadline:
                    pytest.fail("ikigai serve did not come up")
                time.sleep(0.1)
            with ikigai.connect(path) as k:
                if k.server_version != 9:
                    pytest.skip(f"the installed door speaks wire v{k.server_version}, not v9")
            yield path
        finally:
            process.terminate()
            process.wait(timeout=10)


def test_the_real_door_accepts_a_run_and_its_mounted_peer_answers(door):
    with ikigai.connect(door) as k:
        assert k.server_version == 9
        k.retries, k.deadline = 2, time.monotonic() + 10
        assert k.source("urn:py:probe").text == "retries=None left=none"
        # ⚠ Observed on 0.1.66: the door takes the IssueWith but does not forward the run to
        # the peer it mounts (it calls the peer with a plain Issue), so the peer serves with
        # no pool and no deadline. Pinned so a door that starts forwarding says so here.
        (run,) = SEEN
        assert run.retries is None and run.deadline is None
        # The same run on the door's own resources, and a typed failure, cross as before.
        assert k.source("urn:iki:fn:toUpper", **{"in": "hi"}).text == "HI"
        with pytest.raises(ikigai.UnresolvedError):
            k.source("urn:iki:fn:nope")


def test_the_real_door_answers_a_spent_deadline(door):
    with ikigai.connect(door) as k:
        k.retries, k.deadline = 0, time.monotonic() - 1
        # The call goes as IssueWith with zero time left. ⚠ Observed on 0.1.66: the door
        # still answers, on the peer and on its own resources; it does not enforce the
        # carried time. Either way the connection stays usable for the next call.
        assert k.source("urn:py:probe").text == "retries=None left=none"
        assert k.source("urn:iki:fn:toUpper", **{"in": "hi"}).text == "HI"
        k.retries, k.deadline = None, None
        assert k.source("urn:iki:fn:toUpper", **{"in": "ok"}).text == "OK"


@pytest.mark.parametrize("name", list(DOOR_ERRORS))
def test_the_real_door_relays_each_new_error_byte_for_byte(door, name):
    error = DOOR_ERRORS[name]
    call = wire.Issue(Request(Verb.SOURCE, f"urn:py:err:{name}", {}))
    # To a v9 client: the peer's typed error, re-encoded by the door to the same bytes this
    # side encodes for it (the vectors above), whether the call was Issue or IssueWith.
    sock, f = _raw(door, 9)
    with sock, f:
        wire.write_frame(f, wire.encode_call(call))
        assert wire.read_frame(f) == wire.encode_reply(wire.ErrorTypedReply(error))
        with_run = IssueWithCall(call.request, Capability.root(), IssueWith(None, 2, 1500))
        wire.write_frame(f, wire.encode_call(with_run))
        assert wire.read_frame(f) == wire.encode_reply(wire.ErrorTypedReply(error))
    # To a v8 client: flattened to Endpoint(display), the form this side downgrades to.
    sock, f = _raw(door, 8)
    with sock, f:
        wire.write_frame(f, wire.encode_call(call))
        assert wire.read_frame(f) == wire.encode_reply(
            wire.ErrorTypedReply(ikigai.EndpointError(error.display()))
        )
    # And the client raises the class itself.
    with ikigai.connect(door) as k, pytest.raises(type(error)) as raised:
        k.source(f"urn:py:err:{name}")
    assert raised.value == error
