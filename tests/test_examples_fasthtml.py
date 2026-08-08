"""Smoke tests for the FastHTML example: the hypermedia face, exercised
headlessly with Starlette's TestClient (fragments and pages are plain HTML —
no browser or JS runtime needed to verify them)."""

import pytest

import ikigai

fasthtml = pytest.importorskip("fasthtml", reason="examples extras not installed")

from starlette.testclient import TestClient  # noqa: E402

from examples.fasthtml_app import app  # noqa: E402


def test_index_offers_a_form_per_endpoint(examples_peer):
    with TestClient(app) as client:
        page = client.get("/").text
        assert 'hx-get="/hello"' in page
        assert 'hx-get="/upper"' in page
        assert 'hx-get="/reverse"' in page


def test_hello_both_faces(examples_peer):
    with TestClient(app) as client:
        assert "Hello, Ada!" in client.get("/hello/Ada").text  # the surface route
        assert "Hello, Ada!" in client.get("/hello", params={"who": "Ada"}).text  # the form's


def test_fragments_carry_the_cache_verdict(examples_peer):
    with TestClient(app) as client:
        fragment = client.get("/upper", params={"text": "abc"}).text
        assert "ABC" in fragment
        assert "cache: MISS" in fragment
        assert "cba" in client.get("/reverse", params={"text": "abc"}).text


def test_catalog_page_lists_the_space(examples_peer):
    with TestClient(app) as client:
        page = client.get("/catalog").text
        for pattern in ("urn:py:hello", "urn:py:upper", "urn:py:reverse"):
            assert pattern in page


def test_endpoint_error_maps_to_502(partial_peer):
    with TestClient(app) as client:
        response = client.get("/upper", params={"text": "abc"})
        assert response.status_code == 502
        assert "no endpoint resolved for urn:py:upper" in response.text


def test_peer_gone_maps_to_503(dying_peer):
    with TestClient(app) as client:
        response = client.get("/hello/Ada")
        assert response.status_code == 503
        assert "is the peer running?" in response.text


@pytest.mark.parametrize(
    "error,status",
    [
        (ikigai.DeniedError("needs urn:cap:demo"), 403),
        (ikigai.NotFoundError("no such row"), 404),
        (ikigai.MissingArgumentError("who"), 400),
        (ikigai.InvalidArgumentError("who", "not a name"), 400),
        (ikigai.TimeoutError("5s elapsed"), 503),
        (ikigai.UnavailableError("upstream down"), 503),
    ],
    ids=lambda v: type(v).__name__ if isinstance(v, Exception) else str(v),
)
def test_wire_taxonomy_maps_to_http_status(typed_error_peer, error, status):
    # The wire v7 payoff: the peer's typed failure picks the HTTP status.
    typed_error_peer(error)
    with TestClient(app) as client:
        response = client.get("/hello/Ada")
        assert response.status_code == status
        assert error.message in response.text
