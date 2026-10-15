"""The REST surface: every admin route, every error mapping, and the app lifespan."""

from collections.abc import AsyncIterator

import httpx
import pytest

from livepeer_builder import BuilderEngine, EngineSettings
from livepeer_builder.adapters.static import StaticDiscoverySource
from livepeer_builder.contracts import ActorContext
from livepeer_builder.errors import NoCapacity, ProviderError, RunnerCallError
from livepeer_builder.http import create_app
from livepeer_builder.testing import FakeUsageSource, ScriptedTransport, runner
from tests.conftest import EngineFactory

APP = "example/echo"


@pytest.fixture
async def api(
    make_engine: EngineFactory,
) -> AsyncIterator[tuple[httpx.AsyncClient, BuilderEngine, dict[str, str], dict[str, str]]]:
    engine = await make_engine(runners=(runner(url="https://r1"), runner(url="https://r2")))
    operator = ActorContext(actor_id="operator", credential_kind="admin")
    admin = await engine.keys.bootstrap_admin()
    alice = await engine.keys.issue(operator, kind="actor", actor_id="alice")
    app = create_app(engine, manage_lifecycle=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://engine") as client:
        yield (
            client,
            engine,
            {"Authorization": f"Bearer {admin.token}"},
            {"Authorization": f"Bearer {alice.token}"},
        )


ALLOCATION = "/v1/admin/applications/default/actors/alice/allocation"


async def test_admin_allocation_lifecycle_over_rest(api, usage: FakeUsageSource) -> None:  # type: ignore[no-untyped-def]
    client, _engine, admin, alice = api
    assert (
        await client.post(ALLOCATION, json={"amount": {"amount": "0", "currency": "USD"}}, headers=admin)
    ).status_code == 201
    funded = await client.post(
        ALLOCATION + "/fund",
        json={"amount": {"amount": "5", "currency": "USD"}, "operation_ref": "t1"},
        headers=admin,
    )
    assert funded.json()["status"] == "active"
    assert (await client.post(ALLOCATION + "/status", json={"status": "paused"}, headers=admin)).json()[
        "status"
    ] == "paused"
    assert (
        await client.post(ALLOCATION + "/status", json={"status": "active"}, headers=admin)
    ).status_code == 200
    rotated = await client.post(ALLOCATION + "/rotate-key", json={"operation_ref": "r1"}, headers=admin)
    assert rotated.status_code == 200 and "lpg_" not in rotated.text
    assert (await client.get("/v1/allowance", headers=alice)).json()["granted"]["amount"] == "5"
    assert (await client.post("/v1/admin/costs/sync", headers=admin)).json()["pages"] == 1
    reconciliation = await client.get("/v1/admin/reconciliation", params={"actor_id": "alice"}, headers=admin)
    assert reconciliation.status_code == 200
    assert (await client.post(ALLOCATION + "/revoke", headers=admin)).json()["status"] == "revoked"
    bad = await client.post(
        ALLOCATION + "/fund",
        json={"amount": {"amount": "1", "currency": "USD"}, "operation_ref": "t2"},
        headers=admin,
    )
    assert bad.status_code == 404  # revoked: no longer provisioned


async def test_grant_fund_is_admin_only(api) -> None:  # type: ignore[no-untyped-def]
    client, _engine, admin, alice = api
    body = {"amount": {"amount": "5", "currency": "USD"}, "operation_ref": "g1"}
    assert (await client.post("/v1/admin/grant/fund", json=body, headers=alice)).status_code == 403
    funded = await client.post("/v1/admin/grant/fund", json=body, headers=admin)
    assert funded.status_code == 204 and funded.content == b""


async def test_keys_jobs_usage_and_costs_over_rest(api, transport: ScriptedTransport) -> None:  # type: ignore[no-untyped-def]
    client, _engine, admin, alice = api
    await client.post(ALLOCATION, json={"amount": {"amount": "5", "currency": "USD"}}, headers=admin)
    keys = (await client.get("/v1/admin/keys", headers=admin)).json()
    assert {k["kind"] for k in keys} == {"admin", "actor"}
    transport.steps = [ScriptedTransport.ok()]
    ran = await client.post("/v1/jobs", json={"app": APP}, headers=alice)
    job_id = ran.headers["livepeer-job-id"]
    assert len((await client.get("/v1/jobs", headers=alice)).json()) == 1
    assert (
        await client.post(f"/v1/jobs/{job_id}/usage", json={"quantities": {"tokens": 3}}, headers=alice)
    ).status_code == 200
    assert (await client.get(f"/v1/jobs/{job_id}/result", headers=alice)).status_code == 501
    summary = await client.get("/v1/costs/summary", headers=alice)
    assert summary.status_code == 200 and summary.json()["jobs_pending"] == 1
    actor_key = next(k["key_id"] for k in keys if k["kind"] == "actor")
    assert (await client.post(f"/v1/admin/keys/{actor_key}/revoke", headers=admin)).status_code == 204
    assert (await client.get("/v1/jobs", headers=alice)).status_code == 401  # revoked key


async def test_error_mapping(api, transport: ScriptedTransport, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    client, engine, admin, alice = api
    await client.post(ALLOCATION, json={"amount": {"amount": "5", "currency": "USD"}}, headers=admin)

    transport.steps = [
        RunnerCallError("http", "bad input", payment_sent=True, status_code=422, body="field x")
    ]
    rejected = await client.post("/v1/jobs", json={"app": APP}, headers=alice)
    assert rejected.status_code == 422  # the runner's own 4xx is forwarded

    transport.steps = [RunnerCallError("refused", "busy", payment_sent=False)] * 2
    busy = await client.post("/v1/jobs", json={"app": APP}, headers=alice)
    assert (busy.status_code, busy.headers["retry-after"]) == (503, "5")

    transport.steps = [ScriptedTransport.ok()]
    first = await client.post(
        "/v1/jobs", json={"app": APP, "payload": {"a": 1}, "operation_ref": "o1"}, headers=alice
    )
    again = await client.post(
        "/v1/jobs", json={"app": APP, "payload": {"a": 1}, "operation_ref": "o1"}, headers=alice
    )
    changed = await client.post(
        "/v1/jobs", json={"app": APP, "payload": {"a": 2}, "operation_ref": "o1"}, headers=alice
    )
    assert (again.status_code, again.headers["livepeer-job-id"]) == (409, first.headers["livepeer-job-id"])
    assert changed.status_code == 409 and "another request" in changed.json()["error"]

    async def full(*_: object, **__: object) -> object:
        raise NoCapacity("engine is at its concurrent job limit", retry_after_s=7)

    monkeypatch.setattr(engine.jobs, "run", full)
    assert (await client.post("/v1/jobs", json={"app": APP}, headers=alice)).headers["retry-after"] == "7"

    for status, expected in ((409, 409), (500, 502)):

        async def provider_fails(*_: object, status: int = status) -> object:
            raise ProviderError(status, "nope")

        monkeypatch.setattr(engine.payments, "fund", provider_fails)
        response = await client.post(
            ALLOCATION + "/fund",
            json={"amount": {"amount": "1", "currency": "USD"}, "operation_ref": "x"},
            headers=admin,
        )
        assert response.status_code == expected

    bad = await client.post(
        "/v1/admin/keys", json={"actor_id": "x", "permissions": ["everything"]}, headers=admin
    )
    assert bad.status_code == 400


async def test_lifespan_starts_and_stops_the_engine() -> None:
    engine = await BuilderEngine.from_settings(
        EngineSettings(database_url="sqlite+aiosqlite:///:memory:", log_json=False),
        discovery_source=StaticDiscoverySource([runner()]),
    )
    app = create_app(engine)
    async with app.router.lifespan_context(app):  # what an ASGI server runs at startup and shutdown
        assert engine.health().ready and len(engine._tasks) == 3
    assert engine._tasks == []


async def test_readyz_is_503_before_the_first_discovery(make_engine: EngineFactory) -> None:
    engine = await BuilderEngine.from_settings(
        EngineSettings(database_url="sqlite+aiosqlite:///:memory:", log_json=False),
        discovery_source=StaticDiscoverySource(),
    )
    app = create_app(engine, manage_lifecycle=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://engine") as client:
        assert (await client.get("/readyz")).status_code == 503
    await engine.stop()
