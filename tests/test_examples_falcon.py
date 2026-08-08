"""Smoke tests for the Falcon example. Falcon's ASGIConductor runs the
lifespan cycle (which is where the app connects), so each scenario is an
async block driven by asyncio.run — no external HTTP client involved."""

import asyncio

import pytest

import ikigai

falcon = pytest.importorskip("falcon", reason="examples extras not installed")

from falcon import testing  # noqa: E402

from examples.falcon_app import create_app  # noqa: E402


def scenario(fn):
    """Run one conductor scenario against a fresh app."""

    async def run():
        async with testing.ASGIConductor(create_app()) as conductor:
            await fn(conductor)

    asyncio.run(run())


def test_hello_path_param(examples_peer):
    async def check(conductor):
        result = await conductor.simulate_get("/hello/Ada")
        assert result.status_code == 200
        assert result.text == "Hello, Ada!"
        assert result.headers["x-ikigai-cache"] == "MISS"

    scenario(check)


def test_upper_and_reverse_query_params(examples_peer):
    async def check(conductor):
        assert (await conductor.simulate_get("/upper", params={"text": "abc"})).text == "ABC"
        assert (await conductor.simulate_get("/reverse", params={"text": "abc"})).text == "cba"

    scenario(check)


def test_missing_query_param_is_a_client_error(examples_peer):
    async def check(conductor):
        # required=True on get_param: Falcon 400s before the wire is touched.
        assert (await conductor.simulate_get("/upper")).status_code == 400

    scenario(check)


def test_catalog_lists_the_space(examples_peer):
    async def check(conductor):
        result = await conductor.simulate_get("/catalog")
        patterns = {entry["pattern"] for entry in result.json}
        assert {"urn:py:hello", "urn:py:upper", "urn:py:reverse"} <= patterns

    scenario(check)


def test_endpoint_error_maps_to_502(partial_peer):
    async def check(conductor):
        result = await conductor.simulate_get("/upper", params={"text": "abc"})
        assert result.status_code == 502
        assert "no endpoint resolved for urn:py:upper" in result.text

    scenario(check)


def test_peer_gone_maps_to_503(dying_peer):
    async def check(conductor):
        result = await conductor.simulate_get("/hello/Ada")
        assert result.status_code == 503
        assert "is the peer running?" in result.text

    scenario(check)


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

    async def check(conductor):
        result = await conductor.simulate_get("/hello/Ada")
        assert result.status_code == status
        assert error.message in result.text

    scenario(check)
