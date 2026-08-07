"""Smoke tests for the Litestar example: HTTP -> handler -> wire -> served
space, pure Python end to end (no Rust binary needed)."""

import pytest

litestar = pytest.importorskip("litestar", reason="examples extras not installed")

from litestar.testing import TestClient  # noqa: E402

from examples.litestar_app import app  # noqa: E402


def test_hello_path_param(examples_peer):
    with TestClient(app=app) as client:
        response = client.get("/hello/Ada")
        assert response.status_code == 200
        assert response.text == "Hello, Ada!"
        assert response.headers["x-ikigai-cache"] == "MISS"  # direct: no kernel cache upstream


def test_upper_and_reverse_query_params(examples_peer):
    with TestClient(app=app) as client:
        assert client.get("/upper", params={"text": "abc"}).text == "ABC"
        assert client.get("/reverse", params={"text": "abc"}).text == "cba"


def test_missing_query_param_is_a_client_error(examples_peer):
    # The typed signature IS the contract: Litestar rejects the request
    # before the handler runs — the framework analog of a required ArgSpec.
    with TestClient(app=app) as client:
        assert client.get("/upper").status_code == 400


def test_catalog_lists_the_space(examples_peer):
    with TestClient(app=app) as client:
        patterns = {entry["pattern"] for entry in client.get("/catalog").json()}
        assert {"urn:py:hello", "urn:py:upper", "urn:py:reverse"} <= patterns


def test_endpoint_error_maps_to_502(partial_peer):
    with TestClient(app=app) as client:
        response = client.get("/upper", params={"text": "abc"})
        assert response.status_code == 502
        assert "no endpoint resolved for urn:py:upper" in response.text


def test_peer_gone_maps_to_503(dying_peer):
    with TestClient(app=app) as client:
        response = client.get("/hello/Ada")
        assert response.status_code == 503
        assert "is the peer running?" in response.text
