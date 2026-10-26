"""The REST surface: every admin route, every error mapping, and the app lifespan."""

from collections.abc import AsyncIterator

import httpx
import pytest

from livepeer_builder import BuilderEngine, EngineSettings, EngineSupply
from livepeer_builder.adapters.static import StaticDiscoverySource
from livepeer_builder.contracts import ActorContext
from livepeer_builder.errors import NoCapacity, ProviderError, RunnerCallError
from livepeer_builder.http import create_app
from livepeer_builder.testing import FakeAllocationManager, FakeUsageSource, ScriptedTransport, runner
from tests.conftest import EngineFactory
from tests.unit.test_runtime import LOOPS, running_loops

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
        await client.post(
            ALLOCATION,
            json={"amount": {"amount": "0", "currency": "USD"}, "grant_id": "grant_1"},
            headers=admin,
        )
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


async def test_grants_are_listed_and_created_over_rest(api) -> None:  # type: ignore[no-untyped-def]
    client, _engine, admin, alice = api
    assert (await client.get("/v1/admin/grants", headers=alice)).status_code == 403
    assert (await client.get("/v1/admin/grants", headers=admin)).json() == []
    created = await client.post(
        "/v1/admin/grants",
        json={"name": "ops", "amount": {"amount": "5", "currency": "USD"}},
        headers=admin,
    )
    assert created.status_code == 201
    assert created.json()["name"] == "ops" and created.json()["total"] == "5"
    assert (await client.get("/v1/admin/grants", headers=admin)).json() == [created.json()]


async def test_grant_fund_is_admin_only(api, manager: FakeAllocationManager) -> None:  # type: ignore[no-untyped-def]
    client, _, admin, alice = api
    body = {"amount": {"amount": "5", "currency": "USD"}, "operation_ref": "g1", "grant_id": "grant_chosen"}
    assert (await client.post("/v1/admin/grant/fund", json=body, headers=alice)).status_code == 403
    missing = {"amount": {"amount": "5", "currency": "USD"}, "operation_ref": "g2"}
    assert (await client.post("/v1/admin/grant/fund", json=missing, headers=admin)).status_code == 400
    funded = await client.post("/v1/admin/grant/fund", json=body, headers=admin)
    assert funded.status_code == 204 and funded.content == b""
    assert manager.funded_grants[0][0] == "grant_chosen"


async def test_keys_jobs_usage_and_costs_over_rest(api, transport: ScriptedTransport) -> None:  # type: ignore[no-untyped-def]
    client, _engine, admin, alice = api
    await client.post(
        ALLOCATION, json={"amount": {"amount": "5", "currency": "USD"}, "grant_id": "grant_1"}, headers=admin
    )
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
    # Paid, usage not here yet: pending at once, before any sync (#33, ISS-17), and the same after it.
    before = await client.get("/v1/costs/summary", headers=alice)
    assert before.status_code == 200 and before.json()["jobs_pending"] == 1
    early = (await client.get("/v1/costs/projections", headers=alice)).json()
    assert [(cost["job_id"], cost["status"]) for cost in early] == [(job_id, "pending")]
    await client.post("/v1/admin/costs/sync", headers=admin)
    summary = await client.get("/v1/costs/summary", headers=alice)
    assert summary.status_code == 200 and summary.json()["jobs_pending"] == 1
    projections = (await client.get("/v1/costs/projections", headers=alice)).json()
    assert len(projections) == 1 and projections[0]["job_id"] == job_id
    actor_key = next(k["key_id"] for k in keys if k["kind"] == "actor")
    assert (await client.post(f"/v1/admin/keys/{actor_key}/revoke", headers=admin)).status_code == 204
    assert (await client.get("/v1/jobs", headers=alice)).status_code == 401  # revoked key


async def test_error_mapping(api, transport: ScriptedTransport, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    client, engine, admin, alice = api
    await client.post(
        ALLOCATION, json={"amount": {"amount": "5", "currency": "USD"}, "grant_id": "grant_1"}, headers=admin
    )

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
    engine = await BuilderEngine.create(
        EngineSettings(database_url="sqlite+aiosqlite:///:memory:", log_json=False),
        supply=EngineSupply(discovery_source=StaticDiscoverySource([runner()])),
    )
    app = create_app(engine)
    async with app.router.lifespan_context(app):  # what an ASGI server runs at startup and shutdown
        assert engine.health().ready and running_loops() == LOOPS
    assert running_loops() == set()


async def test_readyz_is_503_before_the_first_discovery(make_engine: EngineFactory) -> None:
    engine = await BuilderEngine.create(
        EngineSettings(database_url="sqlite+aiosqlite:///:memory:", log_json=False),
        supply=EngineSupply(discovery_source=StaticDiscoverySource()),
    )
    app = create_app(engine, manage_lifecycle=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://engine") as client:
        assert (await client.get("/readyz")).status_code == 503
    await engine.stop()


async def test_keys_are_inspected_and_rotated_over_rest(api) -> None:  # type: ignore[no-untyped-def]
    client, _engine, admin, alice = api
    actor_key = next(
        k for k in (await client.get("/v1/admin/keys", headers=admin)).json() if k["kind"] == "actor"
    )
    alice_token = alice["Authorization"].removeprefix("Bearer ")

    shown = await client.get(f"/v1/admin/keys/{actor_key['key_id']}", headers=admin)
    assert shown.status_code == 200 and shown.json() == actor_key
    assert alice_token not in shown.text and "token" not in shown.json()  # inspection never shows a secret
    assert (await client.get("/v1/admin/keys/key_unknown", headers=admin)).status_code == 404
    assert (await client.get(f"/v1/admin/keys/{actor_key['key_id']}", headers=alice)).status_code == 403

    rotated = await client.post(
        f"/v1/admin/keys/{actor_key['key_id']}/rotate", json={"label": "ci"}, headers=admin
    )
    assert rotated.status_code == 201
    new = rotated.json()
    assert new["token"].startswith("lpk_") and new["key"]["label"] == "ci"
    assert (new["key"]["actor_id"], new["key"]["permissions"]) == (
        actor_key["actor_id"],
        actor_key["permissions"],
    )
    assert (await client.get("/v1/jobs", headers=alice)).status_code == 401  # the old token, at once
    assert (
        await client.get("/v1/jobs", headers={"Authorization": f"Bearer {new['token']}"})
    ).status_code == 200

    again = await client.post(f"/v1/admin/keys/{new['key']['key_id']}/rotate", headers=admin)  # no body
    assert again.status_code == 201 and again.json()["key"]["label"] == "ci"
    stale = await client.post(f"/v1/admin/keys/{actor_key['key_id']}/rotate", headers=admin)
    assert stale.status_code == 400  # already rotated, so revoked
