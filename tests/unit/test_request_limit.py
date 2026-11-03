"""The request-size limit on the engine's own API (D59): a body over ``LPB_HTTP_MAX_REQUEST_BYTES`` is
refused with 413 before it is read into memory, whether it declares its length or streams without one.
Routes outside ``/v1/`` (an operator's own, or a persistent endpoint's proxy) are not limited here."""

import base64
import json
from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI, Request

from livepeer_builder import EngineSettings
from livepeer_builder.http import create_app
from livepeer_builder.http.server import build_app
from livepeer_builder.store import SqlStore
from tests.conftest import EngineFactory

LIMIT = 1024


async def client_for(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://engine")


@pytest.fixture
async def limited(make_engine: EngineFactory) -> AsyncIterator[httpx.AsyncClient]:
    engine = await make_engine()
    app = create_app(engine, manage_lifecycle=False, max_request_bytes=LIMIT)

    @app.post("/custom/echo")  # an operator's own route, outside /v1/
    async def echo(request: Request) -> dict[str, int]:
        return {"bytes": len(await request.body())}

    async with await client_for(app) as client:
        yield client


def job(size: int) -> bytes:
    """A job body of exactly ``size`` bytes."""
    shell = json.dumps({"app": "example/echo", "payload": {"pad": ""}}).encode()
    return shell.replace(b'"pad": ""', b'"pad": "' + b"x" * (size - len(shell)) + b'"')


def refused(response: httpx.Response) -> None:
    assert response.status_code == 413, response.text
    body = response.json()
    assert (body["code"], body["limit_bytes"]) == ("request_too_large", LIMIT)
    assert str(LIMIT) in body["error"]


async def test_a_declared_length_over_the_limit_is_413_before_anything_else(
    limited: httpx.AsyncClient,
) -> None:
    """No credential either: the size is refused first, so an unauthenticated caller cannot make the engine
    hold a large body to find out it is not allowed."""
    response = await limited.post(
        "/v1/jobs", content=job(LIMIT + 1), headers={"content-type": "application/json"}
    )
    refused(response)


async def test_a_streamed_body_without_a_length_is_cut_off_at_the_limit(limited: httpx.AsyncClient) -> None:
    async def chunks() -> AsyncIterator[bytes]:
        body = job(LIMIT * 4)
        for start in range(0, len(body), 256):
            yield body[start : start + 256]

    response = await limited.post("/v1/jobs", content=chunks(), headers={"content-type": "application/json"})
    assert "content-length" not in response.request.headers
    refused(response)


async def test_a_body_at_the_limit_is_read_as_usual(limited: httpx.AsyncClient) -> None:
    response = await limited.post(
        "/v1/jobs", content=job(LIMIT), headers={"content-type": "application/json"}
    )
    assert response.status_code == 401  # read and parsed; refused for the missing credential, not the size


async def test_routes_outside_v1_are_not_limited(limited: httpx.AsyncClient) -> None:
    response = await limited.post("/custom/echo", content=b"x" * (LIMIT * 4))
    assert response.status_code == 200
    assert response.json() == {"bytes": LIMIT * 4}


def test_the_default_admits_the_largest_raw_body_a_job_may_carry() -> None:
    """raw_body may decode to 8 MiB; base64 inside the JSON makes it about 10.7 MiB on the wire."""
    largest = json.dumps(
        {
            "app": "example/echo",
            "content_type": "application/octet-stream",
            "raw_body": base64.b64encode(b"\0" * (8 * 1024 * 1024)).decode(),
        }
    )
    default = EngineSettings.model_validate({"database_url": "sqlite+aiosqlite:///:memory:"})
    assert len(largest.encode()) < default.http_max_request_bytes == 16 * 1024 * 1024


async def test_the_service_takes_the_limit_from_its_settings(store: SqlStore) -> None:
    del store  # the engine below opens its own in-memory database
    settings = EngineSettings.from_env(
        {
            "LPB_DATABASE_URL": "sqlite+aiosqlite:///:memory:",
            "LPB_LOG_JSON": "false",
            "LPB_HTTP_MAX_REQUEST_BYTES": str(LIMIT),
        }
    )
    app = await build_app(settings)
    async with app.router.lifespan_context(app), await client_for(app) as client:
        response = await client.post(
            "/v1/jobs", content=job(LIMIT + 1), headers={"content-type": "application/json"}
        )
    refused(response)
