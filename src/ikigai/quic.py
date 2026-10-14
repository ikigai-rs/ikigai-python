"""The QUIC transport: drive a remote ikigai door over QUIC with pinned certificates.

An OPTIONAL extra (``pip install 'ikigai[quic]'``, which pulls ``aioquic``). Nothing
outside this module imports it, so the Unix-socket client works without it::

    from ikigai import quic

    with quic.connect("quic://door.example:4434", cert_dir="~/.ikigai/layers-client/me") as k:
        k.source("urn:layers:me", **{"as": "application/json"}).text

It mirrors ikigai-cli's ``ikigai-quic`` crate, the normative side:

- **One bidirectional stream per call.** The stream carries the postcard ``Call`` and the
  peer's ``Reply``, each ended by the stream's FIN. There is NO length header (the stream
  boundary frames the message, unlike the Unix socket's ``u32`` prefix) and NO hello: the
  same codec, :func:`ikigai.wire.encode_call` / :func:`ikigai.wire.decode_reply`.
- **ALPN is the version gate.** The client offers ``ikigai/8`` then ``ikigai/7``; the id
  the handshake negotiates IS the connection's wire version, and a connection that
  negotiated none of them is refused.
- **Trust is mutual pinning, no CA.** The client presents ``client.crt``/``client.key``
  and accepts exactly one server certificate, ``server.crt``: the peer's leaf DER must equal
  it byte for byte. No chain is built and no host name is checked (the SNI is ``ikigai``,
  as the Rust client sends). The door pins the client's certificate the same way, and
  then maps it to a grant: a certificate it trusts but has not enrolled is refused too.
- **A cert dir** is the layout ``ikigai cert generate --dir DIR`` writes: ``server.crt``
  (the door's, to pin), ``client.crt`` and ``client.key`` (this client's identity). A
  ``server.key`` there is never read.

Liveness and deadlines are the Rust client's: the connection pings every
:data:`KEEP_ALIVE_INTERVAL` and is declared dead after :data:`LIVENESS_TIMEOUT` of
silence, while a CALL may be silent for its own deadline (:data:`DEFAULT_TIMEOUT`, or
:data:`DEFAULT_DESCRIBE_TIMEOUT` for ``Meta`` and ``Entries``). A connection the door ended
(it bounds connection age, it restarted) is redialed at the next call; a read whose reply
was lost is sent again on the new connection, a ``sink`` or ``delete`` never is.

Every refusal names its cause: :class:`PinMismatchError` (the server is not the pinned
one), :class:`ClientRefusedError` (the door's TLS refused this certificate),
:class:`NotEnrolledError` (the door trusts it but holds no grant for it),
:class:`CertSetError` (a file missing or unusable here), and
:class:`ikigai.ConnectionLost` for a door that cannot be reached or stopped answering.
"""

from __future__ import annotations

import asyncio
import hashlib
import socket
import ssl
import threading
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

try:
    from aioquic.asyncio.protocol import QuicConnectionProtocol
    from aioquic.quic import events
    from aioquic.quic.configuration import QuicConfiguration
    from aioquic.quic.connection import QuicConnection
    from cryptography.hazmat.primitives import serialization
    from cryptography.x509 import load_pem_x509_certificate
except ImportError as e:  # pragma: no cover - exercised only without the extra
    raise ImportError(
        "ikigai.quic needs the `quic` extra: pip install 'ikigai[quic]' (it pulls aioquic)"
    ) from e

from . import aio, wire
from .client import DEFAULT_TIMEOUT, Client, ConnectionLost
from .wire import (
    Call,
    EntriesCall,
    IsCached,
    Issue,
    IssueAs,
    IssueTraced,
    ProtocolError,
    Reply,
    Verb,
    WireError,
)

# The ALPN ids, newest first: `ikigai/{v}` for v from PROTOCOL_VERSION down to
# MIN_PROTOCOL_VERSION (ikigai_wire::alpns()).
ALPNS = tuple(
    f"ikigai/{v}" for v in range(wire.PROTOCOL_VERSION, wire.MIN_PROTOCOL_VERSION - 1, -1)
)

# The TLS server name the Rust client sends. Cosmetic: nothing checks it.
SERVER_NAME = "ikigai"

#: How long a dial may take before the door is declared unreachable (the Rust
#: ``CONNECT_TIMEOUT``): a peer that drops packets would otherwise block the caller.
CONNECT_TIMEOUT = 5.0

#: How long the connection may hear nothing at all before it is declared dead (the Rust
#: ``LIVENESS_TIMEOUT``). It can be this short because the client pings.
LIVENESS_TIMEOUT = 15.0

#: How often the client pings: a third of :data:`LIVENESS_TIMEOUT`, so three lost pings
#: in a row are needed to declare a live door dead.
KEEP_ALIVE_INTERVAL = LIVENESS_TIMEOUT / 3

#: The deadline for a self-description (``Meta``, ``Entries``), as opposed to a resolution
#: (the Rust ``DEFAULT_DESCRIBE_TIMEOUT``).
DEFAULT_DESCRIBE_TIMEOUT = 30.0

#: The QUIC error code a client sends when it abandons a call (the Rust ``CANCELED``).
CANCELED = 0x1335

# The door's application close codes (ikigai-quic): a certificate it trusts but holds no
# grant for, a connection that negotiated no wire version, a connection past its age bound.
_UNAUTHORIZED = 1
_NO_VERSION = 2
_AGED = 3

# TLS alerts that, arriving after this client finished its half of the handshake (so the
# door had its certificate), mean the door would not accept it (RFC 8446 section 6.2).
# rustls answers a verifier's refusal with the generic handshake_failure (40), naming the
# reason in the close's text ("client certificate does not match the pinned certificate").
_CERTIFICATE_ALERTS = {
    40: "handshake_failure",
    42: "bad_certificate",
    43: "unsupported_certificate",
    44: "certificate_revoked",
    45: "certificate_expired",
    46: "certificate_unknown",
    48: "unknown_ca",
    116: "certificate_required",
}


class CertSetError(WireError):
    """The cert set on THIS side is missing a file or holds an unusable one. Names the path
    and what it should hold; nothing was sent anywhere."""


class TrustError(WireError):
    """A trust refusal: one side would not accept the other's certificate. Permanent, so a
    client never redials past it; the subclass says which side refused and why."""


class PinMismatchError(TrustError):
    """The server presented a certificate that is not the pinned ``server.crt``. Nothing was
    sent to it: the check runs as the handshake completes, before any call."""


class ClientRefusedError(TrustError):
    """The door's TLS refused this client certificate: it is not in the door's trust list
    (or the door could not use it)."""


class NotEnrolledError(TrustError):
    """The door trusts this certificate but holds no grant for it, so it refused the
    connection. Its operator enrolls the fingerprint (:attr:`fingerprint`)."""

    def __init__(self, message: str, fingerprint: str):
        """``fingerprint`` is the SHA-256 of this client's certificate DER, lowercase hex."""
        super().__init__(message)
        self.fingerprint = fingerprint


def fingerprint(der: bytes) -> str:
    """The id a door enrolls a certificate by: lowercase hex SHA-256 of its DER (what
    ``openssl x509 -outform DER | sha256sum`` prints, and the Rust ``fingerprint``).

    >>> fingerprint(b"")
    'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'
    """
    return hashlib.sha256(der).hexdigest()


def fingerprint_of_pem(pem: str) -> str:
    """:func:`fingerprint` of the first certificate in a PEM text."""
    return fingerprint(ssl.PEM_cert_to_DER_cert(_first_pem_cert(pem)))


def _first_pem_cert(pem: str) -> str:
    begin, end = "-----BEGIN CERTIFICATE-----", "-----END CERTIFICATE-----"
    start = pem.find(begin)
    stop = pem.find(end, start)
    if start < 0 or stop < 0:
        raise ValueError("no certificate in the PEM")
    return pem[start : stop + len(end)] + "\n"


@dataclass(frozen=True)
class CertSet:
    """Where this client's identity and its pin live: ``client.crt`` + ``client.key`` (what
    it presents) and ``server.crt`` (the one server certificate it accepts)."""

    client_cert: Path
    client_key: Path
    server_cert: Path

    @classmethod
    def from_dir(
        cls,
        cert_dir: str | Path,
        *,
        client_cert: str | Path | None = None,
        client_key: str | Path | None = None,
        server_cert: str | Path | None = None,
    ) -> CertSet:
        """The set in ``cert_dir`` (``~`` expanded), in the layout ``ikigai cert generate
        --dir`` writes. Each keyword replaces one file, as the cli's ``--client-cert``,
        ``--client-key`` and ``--server-cert`` do."""
        base = Path(cert_dir).expanduser()

        def pick(given, name):
            return Path(given).expanduser() if given is not None else base / name

        return cls(
            client_cert=pick(client_cert, "client.crt"),
            client_key=pick(client_key, "client.key"),
            server_cert=pick(server_cert, "server.crt"),
        )

    def load(self) -> _Loaded:
        """Read and check the three files, or raise :class:`CertSetError` naming the one
        that is wrong."""
        server_pem = _read(self.server_cert, "the door's certificate, to pin (we send it to you)")
        try:
            pinned = ssl.PEM_cert_to_DER_cert(_first_pem_cert(server_pem))
        except ValueError as e:
            raise CertSetError(f"{self.server_cert} holds no PEM certificate ({e})") from e
        cert_pem = _read(self.client_cert, "this client's certificate")
        key_pem = _read(self.client_key, "this client's private key")
        try:
            certificate = load_pem_x509_certificate(cert_pem.encode())
        except ValueError as e:
            raise CertSetError(f"{self.client_cert} holds no PEM certificate ({e})") from e
        try:
            key = serialization.load_pem_private_key(key_pem.encode(), password=None)
        except (ValueError, TypeError) as e:
            raise CertSetError(f"{self.client_key} holds no usable private key ({e})") from e
        mine = certificate.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        its = key.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        if mine != its:
            raise CertSetError(
                f"{self.client_key} is not the key of {self.client_cert}: "
                "the pair must come from one `cert generate`"
            )
        der = certificate.public_bytes(serialization.Encoding.DER)
        return _Loaded(
            pinned=pinned, certificate=certificate, key=key, fingerprint=fingerprint(der)
        )


@dataclass(frozen=True)
class _Loaded:
    pinned: bytes
    certificate: object
    key: object
    fingerprint: str


def _read(path: Path, what: str) -> str:
    try:
        return path.read_text()
    except FileNotFoundError as e:
        raise CertSetError(f"{path} is missing: it should hold {what}") from e
    except OSError as e:
        raise CertSetError(f"cannot read {path} ({e}): it should hold {what}") from e


def parse_door(url: str) -> tuple[str, int]:
    """``quic://host:port`` (an IPv6 host in brackets) as ``(host, port)``.

    >>> parse_door("quic://127.0.0.1:4434")
    ('127.0.0.1', 4434)
    >>> parse_door("quic://[::1]:9")
    ('::1', 9)
    """
    parts = urlsplit(url)
    if parts.scheme != "quic":
        raise ValueError(f"{url!r} is not a quic:// address")
    try:
        port = parts.port
    except ValueError as e:
        raise ValueError(f"{url!r} has no usable port ({e})") from e
    if not parts.hostname or port is None or parts.path not in ("", "/"):
        raise ValueError(f"{url!r} is not quic://host:port")
    return parts.hostname, port


def replayable(call: Call) -> bool:
    """Whether ``call`` may be sent again after its reply was lost: the enumeration, the
    cache probe and the read verbs; never a ``sink`` or ``delete`` (the Rust
    ``replay_may_follow``)."""
    if isinstance(call, EntriesCall | IsCached):
        return True
    request = call.request
    return request.verb in (Verb.SOURCE, Verb.EXISTS, Verb.META)


def is_describe(call: Call) -> bool:
    """Whether ``call`` asks the door to describe itself (``Entries``, a ``Meta`` issue),
    which runs under :data:`DEFAULT_DESCRIBE_TIMEOUT` (the Rust ``is_describe``)."""
    if isinstance(call, EntriesCall):
        return True
    if isinstance(call, Issue | IssueAs | IssueTraced):
        return call.request.verb == Verb.META
    return False


# -- the connection -----------------------------------------------------------


class _Sent(Exception):
    """A call failed after its request was handed over whole: the door may have run it."""

    def __init__(self, error: Exception):
        super().__init__(str(error))
        self.error = error


class _DoorProtocol(QuicConnectionProtocol):
    """One QUIC connection to a door: the pin and version checks as the handshake completes,
    one future per call stream, and a terminal error once the connection ends."""

    def __init__(self, quic: QuicConnection, stream_handler=None, *, door: str, loaded: _Loaded):
        super().__init__(quic, stream_handler)
        self._door = door
        self._loaded = loaded
        self._calls: dict[int, tuple[asyncio.Future, bytearray]] = {}
        self._handshook = False
        self._ready = asyncio.Event()
        #: The wire version the ALPN negotiation settled on.
        self.version: int | None = None
        #: Why this connection cannot carry calls: set on a refusal or when it ended.
        self.failure: Exception | None = None

    # -- the checks --------------------------------------------------------

    def _refuse(self, error: Exception, code: int, reason: str) -> None:
        self.failure = error
        self._ready.set()
        self.close(error_code=code, reason_phrase=reason)

    def _handshake_completed(self, event: events.HandshakeCompleted) -> None:
        self._handshook = True
        peer = getattr(self._quic.tls, "_peer_certificate", None)
        if peer is None:
            # aioquic keeps the verified leaf here; without it nothing can be pinned, and an
            # unpinned connection is never used.
            self._refuse(
                PinMismatchError(
                    f"the door at {self._door} presented no certificate this client could pin"
                ),
                0x100 + 42,
                "no server certificate to pin",
            )
            return
        presented = peer.public_bytes(serialization.Encoding.DER)
        if presented != self._loaded.pinned:
            self._refuse(
                PinMismatchError(
                    f"the server at {self._door} is not the pinned door: it presented "
                    f"certificate {fingerprint(presented)}, and server.crt pins "
                    f"{fingerprint(self._loaded.pinned)}; nothing was sent to it"
                ),
                0x100 + 42,
                "server certificate does not match the pinned certificate",
            )
            return
        alpn = event.alpn_protocol
        if alpn not in ALPNS:
            self._refuse(
                ProtocolError(
                    f"the door at {self._door} negotiated no ikigai wire version this client "
                    f"speaks (v{wire.MIN_PROTOCOL_VERSION}..=v{wire.PROTOCOL_VERSION}; it "
                    f"answered {alpn!r}): update the older side"
                ),
                _NO_VERSION,
                "no ikigai wire version was negotiated",
            )
            return
        self.version = int(alpn.split("/", 1)[1])
        if getattr(self._quic, "_handshake_confirmed", True):
            self._ready.set()

    def datagram_received(self, data, addr) -> None:
        """Feed a datagram in, then release :meth:`wait_ready` once the door has CONFIRMED
        the handshake: in TLS 1.3 the client finishes before the door has judged its
        certificate, so a refusal arrives only after that."""
        super().datagram_received(data, addr)
        closing = getattr(self._quic, "_close_event", None)
        if closing is not None and self.failure is None:
            # The door closed the connection. aioquic reports that only after its draining
            # period (three probe timeouts); every waiting call learns why now instead.
            self._terminated(closing)
        elif (
            self._handshook
            and self.failure is None
            and getattr(self._quic, "_handshake_confirmed", True)
        ):
            self._ready.set()

    async def wait_ready(self) -> None:
        """Until the door confirmed the handshake and every check passed, or raise why not."""
        await self._ready.wait()
        if self.failure is not None:
            raise self.failure

    # -- events ------------------------------------------------------------

    def quic_event_received(self, event: events.QuicEvent) -> None:
        """Route aioquic's events: the handshake checks, reply bytes to their call, and a
        terminal error to every call still waiting."""
        if isinstance(event, events.HandshakeCompleted):
            self._handshake_completed(event)
        elif isinstance(event, events.StreamDataReceived):
            pending = self._calls.get(event.stream_id)
            if pending is None:
                return  # the door never opens streams; a stray one is ignored
            future, buffer = pending
            buffer.extend(event.data)
            if len(buffer) > wire.MAX_FRAME:
                self._fail_call(
                    event.stream_id,
                    ProtocolError(f"a reply exceeded the {wire.MAX_FRAME}-byte limit"),
                )
                self._quic.stop_stream(event.stream_id, CANCELED)
                self.transmit()
            elif event.end_stream and not future.done():
                del self._calls[event.stream_id]
                future.set_result(bytes(buffer))
        elif isinstance(event, events.StreamReset):
            self._fail_call(
                event.stream_id,
                ConnectionLost(
                    f"the door at {self._door} reset the reply stream (code {event.error_code})"
                ),
            )
        elif isinstance(event, events.ConnectionTerminated):
            self._terminated(event)

    def _terminated(self, event: events.ConnectionTerminated) -> None:
        if self.failure is None:
            self.failure = self._why_ended(event)
        self._ready.set()
        for stream_id in list(self._calls):
            self._fail_call(stream_id, self.failure)

    def _why_ended(self, event: events.ConnectionTerminated) -> Exception:
        code, reason = event.error_code, event.reason_phrase
        said = f": {reason}" if reason else ""
        if 0x100 <= code <= 0x1FF:
            alert = code - 0x100
            # no_application_protocol (120): no ALPN id in common. aioquic before 1.3 sends
            # handshake_failure (40) for it instead, saying so in the reason.
            if alert == 120 or "ALPN" in reason:
                return ProtocolError(
                    f"the door at {self._door} speaks no ikigai wire version this client "
                    f"speaks (it offered v{wire.MIN_PROTOCOL_VERSION}..=v{wire.PROTOCOL_VERSION} "
                    f"as ALPN{said}): update the older side"
                )
            name = _CERTIFICATE_ALERTS.get(alert)
            if self._handshook and name is not None:
                return ClientRefusedError(
                    f"the door at {self._door} refused this client certificate (TLS alert "
                    f"{alert} {name}{said}): it is not in the door's trust list. Send its "
                    f"client.crt (fingerprint {self._loaded.fingerprint}) to the door's "
                    "operator to be enrolled"
                )
            return ConnectionLost(
                f"the TLS handshake with {self._door} failed (alert {alert}"
                f"{' ' + name if name else ''}{said})"
            )
        if event.frame_type is None and self._handshook:
            if code == _UNAUTHORIZED:
                return NotEnrolledError(
                    f"the door at {self._door} trusts this certificate but has not enrolled it, "
                    f"so it refused the connection{said}. Its operator enrolls fingerprint "
                    f"{self._loaded.fingerprint}",
                    self._loaded.fingerprint,
                )
            if code == _NO_VERSION:
                return ProtocolError(
                    f"the door at {self._door} negotiated no ikigai wire version{said}: update "
                    "the older side"
                )
            if code == _AGED:
                return ConnectionLost(
                    f"the door at {self._door} closed a connection past its age bound{said}"
                )
        return ConnectionLost(f"the connection to {self._door} ended (code {code}{said})")

    def _fail_call(self, stream_id: int, error: Exception) -> None:
        pending = self._calls.pop(stream_id, None)
        if pending is not None and not pending[0].done():
            pending[0].set_exception(error)

    # -- calls -------------------------------------------------------------

    def send_call(self, payload: bytes) -> tuple[int, asyncio.Future]:
        """Open a stream, send ``payload`` and FIN: the stream id and the reply's future."""
        if self.failure is not None:
            raise self.failure
        stream_id = self._quic.get_next_available_stream_id(is_unidirectional=False)
        future = self._loop.create_future()
        self._calls[stream_id] = (future, bytearray())
        self._quic.send_stream_data(stream_id, payload, end_stream=True)
        self.transmit()
        return stream_id, future

    def cancel_call(self, stream_id: int) -> None:
        """Abandon a call: ``STOP_SENDING`` (and ``RESET_STREAM``) with :data:`CANCELED`,
        ordinary QUIC frames any door understands."""
        self._calls.pop(stream_id, None)
        if self.failure is not None:
            return
        for stop in (self._quic.stop_stream, self._quic.reset_stream):
            try:
                stop(stream_id, CANCELED)
            except Exception:  # noqa: BLE001 - a stream that already ended has nothing to stop
                pass
        self.transmit()

    def ping_now(self) -> None:
        """Send one PING (its acknowledgment restarts both idle timers)."""
        if self.failure is None:
            self._quic.send_ping(0)
            self.transmit()

    @property
    def ended(self) -> bool:
        """Whether this connection can no longer carry a call."""
        return self.failure is not None


async def _dial(host: str, port: int, loaded: _Loaded, timeout: float) -> _DoorProtocol:
    """Dial the door, pin it, settle the version: a ready connection, or the refusal."""
    loop = asyncio.get_running_loop()
    door = f"quic://{host}:{port}" if ":" not in host else f"quic://[{host}]:{port}"
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_DGRAM)
    except OSError as e:
        raise ConnectionLost(f"cannot resolve {door} ({e})") from e
    last: Exception | None = None
    for family, _, _, _, addr in infos:
        try:
            return await asyncio.wait_for(_dial_one(family, addr, door, loaded), timeout)
        except TrustError:
            raise
        except ProtocolError:
            raise
        except TimeoutError:
            last = ConnectionLost(f"connect {door}: timed out after {timeout:g}s")
        except ConnectionLost as e:
            last = e
        except OSError as e:
            last = ConnectionLost(f"cannot reach {door} ({e})")
    raise last or ConnectionLost(f"cannot reach {door}: no address")


async def _dial_one(family, addr, door: str, loaded: _Loaded) -> _DoorProtocol:
    loop = asyncio.get_running_loop()
    configuration = QuicConfiguration(
        is_client=True,
        alpn_protocols=list(ALPNS),
        server_name=SERVER_NAME,
        verify_mode=ssl.CERT_NONE,  # the pin is checked instead: see _handshake_completed
        idle_timeout=LIVENESS_TIMEOUT,
    )
    configuration.certificate = loaded.certificate
    configuration.private_key = loaded.key
    quic = QuicConnection(configuration=configuration)
    sock = socket.socket(family, socket.SOCK_DGRAM)
    try:
        sock.bind(("::", 0) if family == socket.AF_INET6 else ("0.0.0.0", 0))
        transport, protocol = await loop.create_datagram_endpoint(
            lambda: _DoorProtocol(quic, door=door, loaded=loaded), sock=sock
        )
    except BaseException:
        sock.close()
        raise
    try:
        protocol.connect(addr)
        await protocol.wait_ready()
    except BaseException:
        protocol.close()
        transport.close()
        raise
    protocol.transport_handle = transport
    return protocol


class _Wire:
    """The connection state one client keeps: the live connection, a redial when it has
    ended, and the replay rule. Lives on one event loop."""

    def __init__(
        self,
        host: str,
        port: int,
        loaded: _Loaded,
        *,
        timeout: float | None,
        describe_timeout: float | None,
        connect_timeout: float,
    ):
        self.host, self.port, self.loaded = host, port, loaded
        self.timeout = timeout
        self.describe_timeout = describe_timeout
        self.connect_timeout = connect_timeout
        self.protocol: _DoorProtocol | None = None
        self._stale = False
        self._lock = asyncio.Lock()
        self._pinger: asyncio.Task | None = None

    async def open(self) -> None:
        """The first dial: a refusal or an unreachable door raises here."""
        await self._redial()

    async def _redial(self) -> None:
        self._drop()
        self.protocol = await _dial(self.host, self.port, self.loaded, self.connect_timeout)
        self._stale = False
        if self._pinger is None:
            self._pinger = asyncio.get_running_loop().create_task(self._ping())

    def _drop(self) -> None:
        old, self.protocol = self.protocol, None
        if old is not None:
            old.close()
            handle = getattr(old, "transport_handle", None)
            if handle is not None:
                asyncio.get_running_loop().call_later(1.0, handle.close)

    async def _ping(self) -> None:
        while True:
            await asyncio.sleep(KEEP_ALIVE_INTERVAL)
            if self.protocol is not None:
                self.protocol.ping_now()

    async def round_trip(self, call: Call) -> Reply:
        """Send ``call`` on a fresh stream and decode the reply, healing onto a new
        connection when the old one has ended."""
        payload = wire.encode_call(call)
        deadline = self.describe_timeout if is_describe(call) else self.timeout
        async with self._lock:
            if self.protocol is None or self.protocol.ended or self._stale:
                await self._heal()
            try:
                return await self._attempt(payload, deadline, call)
            except _Sent as sent:
                if not replayable(call):
                    raise sent.error from None
                await self._heal(sent.error)
                try:
                    return await self._attempt(payload, deadline, call)
                except _Sent as again:
                    raise again.error from None

    async def _heal(self, cause: Exception | None = None) -> None:
        if self.protocol is not None and isinstance(self.protocol.failure, TrustError):
            raise self.protocol.failure
        try:
            await self._redial()
        except ConnectionLost as e:
            if cause is not None:
                raise ConnectionLost(f"{cause}; and the redial failed: {e}") from e
            raise

    async def _attempt(self, payload: bytes, deadline: float | None, call: Call) -> Reply:
        protocol = self.protocol
        assert protocol is not None
        if protocol.ended:
            await self._heal()
            protocol = self.protocol
        stream_id, future = protocol.send_call(payload)
        try:
            body = await asyncio.wait_for(future, deadline)
        except TimeoutError as e:
            protocol.cancel_call(stream_id)
            if is_describe(call):
                self._stale = True  # a door that cannot describe itself in time: redial next
            raise ConnectionLost(
                f"no answer from the door at {_door_name(self.host, self.port)} within "
                f"{deadline:g}s (the call was canceled)"
            ) from e
        except (TrustError, ProtocolError):
            raise
        except ConnectionLost as e:
            raise _Sent(e) from e
        return wire.decode_reply(body)

    async def close(self) -> None:
        """Close the connection and stop pinging; final."""
        if self._pinger is not None:
            self._pinger.cancel()
            self._pinger = None
        old, self.protocol = self.protocol, None
        if old is not None:
            old.close()
            try:
                await asyncio.wait_for(old.wait_closed(), 1.0)
            except TimeoutError:
                pass
            handle = getattr(old, "transport_handle", None)
            if handle is not None:
                handle.close()


def _door_name(host: str, port: int) -> str:
    return f"quic://[{host}]:{port}" if ":" in host else f"quic://{host}:{port}"


# -- the clients ----------------------------------------------------------------


class AsyncQuicClient(aio.AsyncClient):
    """The awaitable client over QUIC: the same surface as :class:`ikigai.aio.AsyncClient`
    (the five verbs, ``describe``, ``entries``, ``is_cached``, ``source_traced``).

    Build one with :func:`connect_async`. Calls on one connection are answered one at a time
    by the door, so this client sends them one at a time."""

    def __init__(self, wire_state: _Wire, *, capability: wire.Capability | None = None):
        """Adopt an OPEN connection state; prefer :func:`connect_async`."""
        self._wire = wire_state
        self.capability = capability
        #: The wire version the ALPN negotiation settled on (7 or 8).
        self.server_version = wire_state.protocol.version if wire_state.protocol else None
        #: This client's certificate fingerprint, as the door enrolls it.
        self.fingerprint = wire_state.loaded.fingerprint

    async def _round_trip(self, call: Call) -> Reply:
        reply = await self._wire.round_trip(call)
        if self._wire.protocol is not None:
            self.server_version = self._wire.protocol.version
        return reply

    async def close(self) -> None:
        """Close the connection; final."""
        await self._wire.close()

    async def __aenter__(self) -> AsyncQuicClient:
        """``async with await quic.connect_async(…) as k:`` — returns this client."""
        return self


async def connect_async(
    door: str,
    cert_dir: str | Path | None = None,
    *,
    certs: CertSet | None = None,
    capability: wire.Capability | None = None,
    timeout: float | None = DEFAULT_TIMEOUT,
    describe_timeout: float | None = DEFAULT_DESCRIBE_TIMEOUT,
    connect_timeout: float = CONNECT_TIMEOUT,
) -> AsyncQuicClient:
    """Connect to the door at ``door`` (``quic://host:port``) with the cert set in
    ``cert_dir`` (or ``certs``). ``timeout`` bounds each call start to finish (``None``:
    unbounded), ``describe_timeout`` a ``Meta`` or ``Entries`` call, ``connect_timeout``
    a dial. Raises :class:`CertSetError`, a :class:`TrustError`, :class:`ProtocolError`
    or :class:`ikigai.ConnectionLost`, each naming its cause."""
    if certs is None:
        if cert_dir is None:
            raise CertSetError("no cert set: pass cert_dir (or certs)")
        certs = CertSet.from_dir(cert_dir)
    host, port = parse_door(door)
    state = _Wire(
        host,
        port,
        certs.load(),
        timeout=timeout,
        describe_timeout=describe_timeout,
        connect_timeout=connect_timeout,
    )
    await state.open()
    return AsyncQuicClient(state, capability=capability)


class QuicClient(Client):
    """The synchronous client over QUIC: the same surface as :class:`ikigai.Client`.

    aioquic is asynchronous, so this client runs its own event loop on a daemon thread
    (as the Rust ``QuicResolver`` hides a tokio runtime) and every call blocks on it.
    Build one with :func:`connect`."""

    def __init__(self, inner: AsyncQuicClient, loop: asyncio.AbstractEventLoop, thread):
        """Adopt a client living on ``loop``; prefer :func:`connect`."""
        self._inner = inner
        self._loop = loop
        self._thread = thread
        self._closed = False

    # The verbs read ``self.capability`` and ``self.server_version``; both live on the
    # async client, so a change here is seen by the next call.
    @property
    def capability(self):
        """When set, requests go as ``Call::IssueAs`` under this capability (which the door
        clamps to the authority it minted for this certificate)."""
        return self._inner.capability

    @capability.setter
    def capability(self, value) -> None:
        self._inner.capability = value

    @property
    def server_version(self) -> int | None:
        """The wire version the ALPN negotiation settled on (7 or 8)."""
        return self._inner.server_version

    @property
    def fingerprint(self) -> str:
        """This client's certificate fingerprint, as the door enrolls it."""
        return self._inner.fingerprint

    def _round_trip(self, call: Call) -> Reply:
        if self._closed:
            raise ConnectionLost("this client is closed")
        future = asyncio.run_coroutine_threadsafe(self._inner._round_trip(call), self._loop)
        return future.result()

    def close(self) -> None:
        """Close the connection and stop the client's loop; final."""
        if self._closed:
            return
        self._closed = True
        try:
            asyncio.run_coroutine_threadsafe(self._inner.close(), self._loop).result(5)
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(5)

    def __enter__(self) -> QuicClient:
        """``with quic.connect(…) as k:`` — returns this client."""
        return self


def connect(
    door: str,
    cert_dir: str | Path | None = None,
    *,
    certs: CertSet | None = None,
    capability: wire.Capability | None = None,
    timeout: float | None = DEFAULT_TIMEOUT,
    describe_timeout: float | None = DEFAULT_DESCRIBE_TIMEOUT,
    connect_timeout: float = CONNECT_TIMEOUT,
) -> QuicClient:
    """Connect to the door at ``door`` (``quic://host:port``) with the cert set in
    ``cert_dir``: the blocking twin of :func:`connect_async`, with the same arguments and
    the same refusals."""
    loop = asyncio.new_event_loop()
    ready = threading.Event()

    def run() -> None:
        asyncio.set_event_loop(loop)
        loop.call_soon(ready.set)
        loop.run_forever()
        loop.close()

    thread = threading.Thread(target=run, name="ikigai-quic", daemon=True)
    thread.start()
    ready.wait()
    try:
        inner = asyncio.run_coroutine_threadsafe(
            connect_async(
                door,
                cert_dir,
                certs=certs,
                capability=capability,
                timeout=timeout,
                describe_timeout=describe_timeout,
                connect_timeout=connect_timeout,
            ),
            loop,
        ).result()
    except BaseException:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(5)
        raise
    return QuicClient(inner, loop, thread)
