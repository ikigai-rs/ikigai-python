"""The Python QUIC client against the REAL Rust door (``ikigai serve quic://``) on loopback.

Skips without an ``ikigai`` binary on PATH, and FAILS instead under ``--require-interop``
(CI's interop job). Every door here is a scratch one (``tests/rust_door.py``): port 0 on
127.0.0.1, a scratch cert set from ``ikigai cert generate``, a scratch config home.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

import pytest

pytest.importorskip("aioquic")

import ikigai  # noqa: E402
from ikigai import quic  # noqa: E402
from tests.rust_door import RustDoor, generate, require_ikigai  # noqa: E402


@pytest.fixture
def ikigai_bin(request):
    return require_ikigai(request)


@pytest.fixture
def tmp():
    with tempfile.TemporaryDirectory(prefix="ik-rq-") as d:
        yield Path(d)


@pytest.fixture
def door_set(ikigai_bin, tmp):
    """The door's cert set: its server pair, and the base client.crt it trusts."""
    return generate(ikigai_bin, tmp / "door")


@pytest.fixture
def door(ikigai_bin, tmp, door_set):
    started = []

    def start(home: Path | None = None):
        d = RustDoor(ikigai_bin, tmp, door_set, home=home)
        started.append(d)
        return d

    yield start
    for d in started:
        d.stop()


def client_set(directory: Path, door_set: Path, *, client_from: Path, pin_from: Path) -> Path:
    directory.mkdir(parents=True)
    shutil.copy(client_from / "client.crt", directory / "client.crt")
    shutil.copy(client_from / "client.key", directory / "client.key")
    shutil.copy(pin_from / "server.crt", directory / "server.crt")
    return directory


def test_the_five_verbs_and_typed_errors_cross_the_rust_door(door, door_set):
    d = door()
    with quic.connect(d.url, door_set) as k:
        assert k.server_version == 8
        upper = k.source("urn:iki:fn:toUpper", **{"in": "hi"})
        assert upper.text == "HI" and upper.media_type.startswith("text/plain")
        with pytest.raises(ikigai.UnresolvedError) as missing:
            k.source("urn:nope:nothing")
        assert missing.value.display() == "no endpoint resolved for urn:nope:nothing"
        assert any("toUpper" in e.pattern for e in k.entries())
        described = k.describe("urn:iki:fn:toUpper")
        assert described is not None and described.get("id")
        assert "toUpper" in k.meta("urn:iki:fn:toUpper").text
        assert k.source("urn:iki:fn:toUpper", **{"in": "again"}).text == "AGAIN"


def test_the_fingerprint_is_the_one_the_door_names(door, door_set):
    d = door()
    with quic.connect(d.url, door_set) as k:
        k.source("urn:iki:fn:toUpper", **{"in": "x"})
        assert k.fingerprint == quic.fingerprint_of_pem((door_set / "client.crt").read_text())
        # The door's banner lists each trusted client by its short fingerprint.
        assert f"base  {k.fingerprint[:16]}" in d.log


def test_an_unpinned_server_is_refused(door, door_set, ikigai_bin, tmp):
    d = door()
    impostor = generate(ikigai_bin, tmp / "impostor")
    pinned_elsewhere = client_set(
        tmp / "wrong-pin", door_set, client_from=door_set, pin_from=impostor
    )
    with pytest.raises(quic.PinMismatchError, match="is not the pinned door"):
        quic.connect(d.url, pinned_elsewhere)


def test_a_client_certificate_outside_the_trust_list_is_refused(door, door_set, ikigai_bin, tmp):
    d = door()
    stranger = generate(ikigai_bin, tmp / "stranger")
    outside = client_set(tmp / "outside", door_set, client_from=stranger, pin_from=door_set)
    with pytest.raises(quic.ClientRefusedError, match="not in the door's trust list") as refused:
        quic.connect(d.url, outside)
    assert quic.fingerprint_of_pem((stranger / "client.crt").read_text()) in str(refused.value)


def test_a_trusted_certificate_nobody_enrolled_is_refused_by_name(door, door_set, tmp):
    home = tmp / "enrolling"
    home.mkdir()
    (home / "clients.json").write_text('{"clients": {}}\n')
    (home / "grants.json").write_text("{}\n")
    d = door(home)
    with quic.connect(d.url, door_set) as k, pytest.raises(quic.NotEnrolledError) as refused:
        k.source("urn:iki:fn:toUpper", **{"in": "x"})
    assert refused.value.fingerprint == k.fingerprint
    assert "REFUSED" in d.log and k.fingerprint in d.log


def test_an_enrolled_certificate_is_served_under_its_grant(door, door_set, tmp):
    fingerprint = quic.fingerprint_of_pem((door_set / "client.crt").read_text())
    home = tmp / "enrolled"
    home.mkdir()
    clients = {"clients": {fingerprint: {"grant": "demo", "principal": "urn:example:demo"}}}
    (home / "clients.json").write_text(json.dumps(clients))
    (home / "grants.json").write_text(json.dumps({"demo": ["urn:cap:demo:read"]}))
    d = door(home)
    with quic.connect(d.url, door_set) as k:
        assert k.source("urn:iki:fn:toUpper", **{"in": "ok"}).text == "OK"
