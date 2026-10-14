"""A scripted QUIC door for the transport's tests: aioquic on loopback, in a thread.

NOT the Rust door (``tests/test_quic_interop.py`` runs against that). This stub answers each
call stream with whatever ``respond`` returns, so the client's framing, pinning, version
negotiation, deadlines and redial can be tested where no Rust host is installed (CI).
"""

from __future__ import annotations

import asyncio
import datetime
import threading
from pathlib import Path

from aioquic.asyncio import serve
from aioquic.asyncio.protocol import QuicConnectionProtocol
from aioquic.quic import events
from aioquic.quic.configuration import QuicConfiguration
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from ikigai import wire
from ikigai.quic import ALPNS


def identity() -> tuple[str, str]:
    """A self-signed ECDSA P-256 certificate (SAN ``ikigai``) and its key, as PEM."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "stub")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("ikigai")]), critical=False)
        .sign(key, hashes.SHA256())
    )
    return (
        cert.public_bytes(serialization.Encoding.PEM).decode(),
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode(),
    )


def write_set(directory: Path, server_cert: str, client: tuple[str, str]) -> Path:
    """A cert dir: ``server.crt`` to pin and ``client.{crt,key}`` to present."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "server.crt").write_text(server_cert)
    (directory / "client.crt").write_text(client[0])
    (directory / "client.key").write_text(client[1])
    return directory


class Close:
    """A ``respond`` answer: close the connection with this application code."""

    def __init__(self, code: int, reason: str = ""):
        self.code = code
        self.reason = reason


class StubDoor:
    """Serves ``respond(call) -> Reply | None | Close | (Reply, Close)`` on ``127.0.0.1``,
    port 0: ``None`` never answers, a pair answers and then closes. Every decoded call is
    appended to :attr:`calls` before ``respond`` sees it."""

    def __init__(self, server: tuple[str, str], respond, *, alpns=ALPNS, tmp: Path):
        self.respond = respond
        self.calls: list = []
        self.connections = 0
        self._loop = asyncio.new_event_loop()
        cert_file, key_file = tmp / "stub.crt", tmp / "stub.key"
        cert_file.write_text(server[0])
        key_file.write_text(server[1])
        configuration = QuicConfiguration(is_client=False, alpn_protocols=list(alpns))
        configuration.load_cert_chain(cert_file, key_file)
        started = threading.Event()
        door = self

        class Protocol(QuicConnectionProtocol):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self._buffers: dict[int, bytearray] = {}
                door.connections += 1

            def quic_event_received(self, event):
                if isinstance(event, events.StreamDataReceived):
                    buffer = self._buffers.setdefault(event.stream_id, bytearray())
                    buffer.extend(event.data)
                    if event.end_stream:
                        call = wire.decode_call(bytes(buffer))
                        door.calls.append(call)
                        answer = door.respond(call)
                        then = None
                        if isinstance(answer, tuple):
                            answer, then = answer
                        if isinstance(answer, Close):
                            self.close(error_code=answer.code, reason_phrase=answer.reason)
                        elif answer is not None:
                            self._quic.send_stream_data(
                                event.stream_id, wire.encode_reply(answer), end_stream=True
                            )
                            self.transmit()
                        if then is not None:
                            self.close(error_code=then.code, reason_phrase=then.reason)

        async def start():
            self._server = await serve(
                "127.0.0.1", 0, configuration=configuration, create_protocol=Protocol
            )
            self.port = self._server._transport.get_extra_info("sockname")[1]
            started.set()

        def run():
            asyncio.set_event_loop(self._loop)
            self._loop.run_until_complete(start())
            self._loop.run_forever()

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()
        if not started.wait(10):
            raise RuntimeError("the stub door did not start")

    @property
    def url(self) -> str:
        """The door's ``quic://127.0.0.1:{port}``."""
        return f"quic://127.0.0.1:{self.port}"

    def stop(self) -> None:
        """Close the listener and stop its loop."""
        self._loop.call_soon_threadsafe(self._server.close)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(5)
