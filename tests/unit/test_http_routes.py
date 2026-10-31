"""The REST surface: every admin route, every error mapping, and the app lifespan."""

import base64
from collections.abc import AsyncIterator

import httpx
import pytest

from livepeer_builder import BuilderEngine, EngineSettings, EngineSupply
from livepeer_builder.adapters.static import StaticDiscoverySource
from livepeer_builder.contracts import ActorContext, Job, JobRequest
from livepeer_builder.errors import (
    NoCapacity,
    OperationInProgress,
    ProviderError,
    ProviderUnavailable,
    RunnerCallError,
)
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
    alice = await engine.keys.issue(
        operator, kind="actor", actor_id="alice", operation_ref="issue-http_routes-34"
    )
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
            json={"amount": {"amount": "0", "currency": "USD"}, "grant_id": "grant_1", "operation_ref": "p1"},
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
        json={"name": "ops", "amount": {"amount": "5", "currency": "USD"}, "operation_ref": "grant-1"},
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
        ALLOCATION,
        json={"amount": {"amount": "5", "currency": "USD"}, "grant_id": "grant_1", "operation_ref": "p1"},
        headers=admin,
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


async def test_a_signing_that_may_have_charged_is_503_without_retry_after(
    api, transport: ScriptedTransport
) -> None:  # type: ignore[no-untyped-def]
    """The signer was asked and gave no answer: retrying could pay twice, so no ``Retry-After`` (D53)."""
    client, _engine, admin, alice = api
    await client.post(
        ALLOCATION,
        json={"amount": {"amount": "5", "currency": "USD"}, "grant_id": "grant_1", "operation_ref": "p1"},
        headers=admin,
    )
    transport.steps = [
        RunnerCallError("payment", "no answer from the signer", payment_sent=False, payment_unknown=True)
    ]
    response = await client.post("/v1/jobs", json={"app": APP}, headers=alice)
    assert response.status_code == 503
    assert "retry-after" not in response.headers


async def test_a_repeated_operation_ref_answers_409_with_a_stable_code(  # type: ignore[no-untyped-def]
    api, transport: ScriptedTransport, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D57: one 409 per answer, told apart by ``code``; every one names the job in the job id header."""
    client, engine, admin, alice = api
    await client.post(
        ALLOCATION,
        json={"amount": {"amount": "5", "currency": "USD"}, "grant_id": "grant_1", "operation_ref": "p1"},
        headers=admin,
    )
    body = {"app": APP, "payload": {"a": 1}, "operation_ref": "o1"}
    transport.steps = [ScriptedTransport.ok()]
    first = await client.post("/v1/jobs", json=body, headers=alice)
    job_id = first.headers["livepeer-job-id"]
    exists = await client.post("/v1/jobs", json=body, headers=alice)
    changed = await client.post("/v1/jobs", json=body | {"payload": {"a": 2}}, headers=alice)
    assert (exists.status_code, exists.json()["code"], exists.json()["job"]["id"]) == (
        409, "operation_exists", job_id,
    )  # fmt: skip
    assert "retry-after" not in exists.headers
    assert (changed.status_code, changed.json()["code"], changed.headers["livepeer-job-id"]) == (
        409, "operation_conflict", job_id,
    )  # fmt: skip

    running = Job.model_validate(exists.json()["job"]).model_copy(update={"state": "running"})

    async def still_running(*_: object, **__: object) -> object:
        raise OperationInProgress(running)

    monkeypatch.setattr(engine.jobs, "run", still_running)
    busy = await client.post("/v1/jobs", json=body, headers=alice)
    assert (busy.status_code, busy.json()["code"], busy.headers["retry-after"]) == (
        409, "operation_in_progress", "1",
    )  # fmt: skip
    assert busy.headers["livepeer-job-id"] == job_id and busy.json()["job"]["state"] == "running"


async def test_error_mapping(api, transport: ScriptedTransport, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    client, engine, admin, alice = api
    await client.post(
        ALLOCATION,
        json={"amount": {"amount": "5", "currency": "USD"}, "grant_id": "grant_1", "operation_ref": "p1"},
        headers=admin,
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
        "/v1/admin/keys",
        json={"actor_id": "x", "permissions": ["everything"], "operation_ref": "k1"},
        headers=admin,
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
        f"/v1/admin/keys/{actor_key['key_id']}/rotate",
        json={"label": "ci", "operation_ref": "rk1"},
        headers=admin,
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

    again = await client.post(  # no label: the key keeps its own
        f"/v1/admin/keys/{new['key']['key_id']}/rotate", json={"operation_ref": "rk2"}, headers=admin
    )
    assert again.status_code == 201 and again.json()["key"]["label"] == "ci"
    stale = await client.post(
        f"/v1/admin/keys/{actor_key['key_id']}/rotate", json={"operation_ref": "rk3"}, headers=admin
    )
    assert stale.status_code == 400  # already rotated, so revoked


async def test_a_missing_permission_is_403_and_another_actors_job_404_over_rest(  # type: ignore[no-untyped-def]
    api, transport: ScriptedTransport
) -> None:
    client, engine, admin, alice = api
    await client.post(
        ALLOCATION,
        json={"amount": {"amount": "5", "currency": "USD"}, "grant_id": "grant_1", "operation_ref": "p1"},
        headers=admin,
    )
    transport.steps = [ScriptedTransport.ok()]
    job_id = (await client.post("/v1/jobs", json={"app": APP}, headers=alice)).headers["livepeer-job-id"]
    operator = ActorContext(actor_id="operator", credential_kind="admin")
    bob = await engine.keys.issue(
        operator, kind="actor", actor_id="bob", operation_ref="issue-http_routes-320"
    )
    runner_only = await engine.keys.issue(
        operator,
        kind="actor",
        actor_id="alice",
        permissions=["jobs.run"],
        operation_ref="issue-http_routes-321",
    )
    as_bob = {"Authorization": f"Bearer {bob.token}"}
    as_runner = {"Authorization": f"Bearer {runner_only.token}"}
    for path in (f"/v1/jobs/{job_id}", f"/v1/jobs/{job_id}/result", f"/v1/jobs/{job_id}/cost"):
        assert (await client.get(path, headers=as_runner)).status_code == 403, path
        assert (await client.get(path, headers=as_bob)).status_code == 404, path
    usage = {"quantities": {"tokens": 1}}
    assert (await client.post(f"/v1/jobs/{job_id}/usage", json=usage, headers=as_bob)).status_code == 404
    assert (await client.post(f"/v1/jobs/{job_id}/usage", json=usage, headers=as_runner)).status_code == 200


async def test_a_raw_body_arrives_as_base64_over_rest(  # type: ignore[no-untyped-def]
    api, transport: ScriptedTransport, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The REST envelope carries ``raw_body`` as URL-safe base64 (#55); the runner gets the bytes."""
    client, _engine, admin, alice = api
    await client.post(
        ALLOCATION,
        json={"amount": {"amount": "5", "currency": "USD"}, "grant_id": "grant_1", "operation_ref": "p1"},
        headers=admin,
    )
    seen: list[JobRequest] = []
    call = transport.call

    async def capture(runner, request, credential, on_signed=None):  # type: ignore[no-untyped-def]
        seen.append(request)
        return await call(runner, request, credential, on_signed)

    monkeypatch.setattr(transport, "call", capture)
    transport.steps = [ScriptedTransport.ok()]
    body = b' \x00\xff{"a": 1} '
    job = {
        "app": APP,
        "raw_body": base64.urlsafe_b64encode(body).decode(),
        "content_type": "application/octet-stream",
        "headers": [["x-trace", "t1"]],
    }
    ran = await client.post("/v1/jobs", json=job, headers=alice)
    assert ran.status_code == 200, ran.text
    [request] = seen
    assert (request.raw_body, request.content_type, request.headers) == (
        body,
        "application/octet-stream",
        (("x-trace", "t1"),),
    )
    bad = await client.post("/v1/jobs", json=job | {"raw_body": "not base64!"}, headers=alice)
    assert bad.status_code == 422


async def test_actors_are_listed_and_read_over_rest(api) -> None:  # type: ignore[no-untyped-def]
    client, engine, admin, alice = api
    provisioned = await client.post(
        ALLOCATION,
        json={
            "amount": {"amount": "5", "currency": "USD"},
            "grant_id": "grant_1",
            "operation_ref": "p1",
            "label": "Alice Smith",
        },
        headers=admin,
    )
    assert provisioned.status_code == 201 and provisioned.json()["label"] == "Alice Smith"
    listed = await client.get("/v1/admin/actors", headers=admin)
    assert listed.status_code == 200
    assert [
        (a["actor_id"], a["label"], a["provisioned"], a["allocation"]["status"]) for a in listed.json()
    ] == [
        ("alice", "Alice Smith", True, "active")
    ]  # D48: each row says whether it is provisioned; the allocation is nested
    assert (await client.get("/v1/admin/actors?provisioned=false", headers=admin)).json() == []
    assert (await client.get("/v1/admin/actors?status=paused", headers=admin)).json() == []
    one = await client.get(ALLOCATION, headers=admin)
    assert one.status_code == 200 and one.json()["actor_id"] == "alice"
    assert (await client.get(ALLOCATION.replace("alice", "nobody"), headers=admin)).status_code == 404
    assert (await client.get("/v1/admin/actors", headers=alice)).status_code == 403
    operator = ActorContext(actor_id="operator", credential_kind="admin")
    auditor = await engine.keys.issue(
        operator, kind="auditor", actor_id="dashboard", operation_ref="issue-http_routes-397"
    )
    as_auditor = {"Authorization": f"Bearer {auditor.token}"}
    assert (await client.get("/v1/admin/actors", headers=as_auditor)).status_code == 200
    assert (
        await client.get("/v1/admin/grants", headers=as_auditor)
    ).status_code == 403  # every application's
    funded = await client.post(
        ALLOCATION + "/fund",
        json={"amount": {"amount": "1", "currency": "USD"}, "operation_ref": "x"},
        headers=as_auditor,
    )
    assert funded.status_code == 403  # an auditor reads; it never moves money


async def test_a_label_too_long_to_store_is_refused_before_any_money_moves(  # type: ignore[no-untyped-def]
    api, manager: FakeAllocationManager
) -> None:
    """The stored label holds 200 characters. A longer one is a 422 before the provider creates a key,
    so a retry cannot find money moved and no actor saved (John, #53 review)."""
    client, _engine, admin, _alice = api
    body = {
        "amount": {"amount": "5", "currency": "USD"},
        "grant_id": "grant_1",
        "operation_ref": "p1",
        "label": "x" * 201,
    }
    refused = await client.post(ALLOCATION, json=body, headers=admin)
    assert refused.status_code == 422
    assert "create_key_for_grant" not in manager.calls
    accepted = await client.post(ALLOCATION, json=body | {"label": "x" * 200}, headers=admin)
    assert accepted.status_code == 201 and accepted.json()["label"] == "x" * 200


async def test_an_admin_ref_reused_with_another_body_is_409_operation_conflict(api) -> None:  # type: ignore[no-untyped-def]
    """D57, ISS-11: the same code as for jobs; an admin command has no job, so no job id header."""
    client, _engine, admin, _alice = api
    provisioned = {"amount": {"amount": "0", "currency": "USD"}, "grant_id": "grant_1", "operation_ref": "p1"}
    assert (await client.post(ALLOCATION, json=provisioned, headers=admin)).status_code == 201
    fund = {"amount": {"amount": "5", "currency": "USD"}, "operation_ref": "t1"}
    assert (await client.post(ALLOCATION + "/fund", json=fund, headers=admin)).status_code == 200
    other = fund | {"amount": {"amount": "6", "currency": "USD"}}
    conflict = await client.post(ALLOCATION + "/fund", json=other, headers=admin)
    assert conflict.status_code == 409 and "Livepeer-Job-Id" not in conflict.headers
    assert conflict.json()["code"] == "operation_conflict"
    assert conflict.json()["operation"] == {"command": "fund", "operation_ref": "t1", "state": "done"}


async def test_an_admin_repeat_while_the_first_may_still_run_is_409_in_progress(  # type: ignore[no-untyped-def]
    api, manager: FakeAllocationManager
) -> None:
    client, _engine, admin, _alice = api
    provisioned = {"amount": {"amount": "0", "currency": "USD"}, "grant_id": "grant_1", "operation_ref": "p1"}
    assert (await client.post(ALLOCATION, json=provisioned, headers=admin)).status_code == 201
    fund = {"amount": {"amount": "5", "currency": "USD"}, "operation_ref": "t1"}
    manager.fail_once("fund_allocation", ProviderUnavailable("Batteries unreachable: timed out"))
    assert (await client.post(ALLOCATION + "/fund", json=fund, headers=admin)).status_code == 503
    running = await client.post(ALLOCATION + "/fund", json=fund, headers=admin)
    # a repeat may resume it once it is LPB_OPERATION_STALE_S (120 s) old, not before
    assert running.status_code == 409 and running.headers["Retry-After"] == "120"
    assert running.json()["code"] == "operation_in_progress"
    assert running.json()["operation"] == {"command": "fund", "operation_ref": "t1", "state": "started"}


async def test_an_admin_replay_has_the_original_status_and_says_it_is_a_replay(api) -> None:  # type: ignore[no-untyped-def]
    client, _engine, admin, _alice = api
    provisioned = {"amount": {"amount": "0", "currency": "USD"}, "grant_id": "grant_1", "operation_ref": "p1"}
    assert (await client.post(ALLOCATION, json=provisioned, headers=admin)).status_code == 201
    fund = {"amount": {"amount": "5", "currency": "USD"}, "operation_ref": "t1"}
    first = await client.post(ALLOCATION + "/fund", json=fund, headers=admin)
    again = await client.post(ALLOCATION + "/fund", json=fund, headers=admin)
    assert "Idempotent-Replayed" not in first.headers
    assert (again.status_code, again.json()) == (first.status_code, first.json())
    assert again.headers["Idempotent-Replayed"] == "true"


async def test_a_replayed_grant_funding_is_204_and_says_it_is_a_replay(api) -> None:  # type: ignore[no-untyped-def]
    client, _engine, admin, _alice = api
    body = {"amount": {"amount": "10", "currency": "USD"}, "operation_ref": "g1", "grant_id": "grant_chosen"}
    first = await client.post("/v1/admin/grant/fund", json=body, headers=admin)
    again = await client.post("/v1/admin/grant/fund", json=body, headers=admin)
    assert (first.status_code, again.status_code) == (204, 204)
    assert "Idempotent-Replayed" not in first.headers and again.headers["Idempotent-Replayed"] == "true"


async def test_every_command_that_moves_money_or_makes_a_credential_needs_a_ref(  # type: ignore[no-untyped-def]
    api, manager: FakeAllocationManager
) -> None:
    """Q4: 422 before anything runs when the reference is missing or empty; the routes that cannot apply
    twice take none."""
    client, engine, admin, _alice = api
    usd = {"amount": "5", "currency": "USD"}
    key_id = (await engine.keys.list_keys(ActorContext(actor_id="operator", credential_kind="admin")))[
        0
    ].key_id
    commands = [
        (ALLOCATION, {"amount": usd, "grant_id": "grant_1"}),
        (ALLOCATION + "/fund", {"amount": usd}),
        ("/v1/admin/grant/fund", {"amount": usd, "grant_id": "grant_1"}),
        ("/v1/admin/grants", {"name": "ops", "amount": usd}),
        (ALLOCATION + "/rotate-key", {}),
        ("/v1/admin/keys", {"actor_id": "bob"}),
        (f"/v1/admin/keys/{key_id}/rotate", {"label": "ci"}),
    ]
    for path, body in commands:
        for missing in (body, body | {"operation_ref": ""}):
            answer = await client.post(path, json=missing, headers=admin)
            assert answer.status_code == 422, (path, missing, answer.text)
    assert manager.calls == []


async def test_a_replayed_key_issue_is_201_without_the_token(api) -> None:  # type: ignore[no-untyped-def]
    client, _engine, admin, _alice = api
    body = {"actor_id": "bob", "operation_ref": "k1"}
    first = await client.post("/v1/admin/keys", json=body, headers=admin)
    again = await client.post("/v1/admin/keys", json=body, headers=admin)
    assert (first.status_code, again.status_code) == (201, 201)
    assert first.json()["token"].startswith("lpk_") and "Idempotent-Replayed" not in first.headers
    assert again.json() == first.json() | {"token": None, "replayed": True}
    assert again.headers["Idempotent-Replayed"] == "true"
    rotate = {"operation_ref": "rk1"}
    rotated = await client.post(
        f"/v1/admin/keys/{first.json()['key']['key_id']}/rotate", json=rotate, headers=admin
    )
    replayed = await client.post(
        f"/v1/admin/keys/{first.json()['key']['key_id']}/rotate", json=rotate, headers=admin
    )
    assert replayed.status_code == 201 and replayed.json() == rotated.json() | {
        "token": None,
        "replayed": True,
    }
    assert replayed.headers["Idempotent-Replayed"] == "true"


async def test_replayed_provisioning_and_grant_creation_say_so(api) -> None:  # type: ignore[no-untyped-def]
    client, _engine, admin, _alice = api
    body = {"amount": {"amount": "5", "currency": "USD"}, "grant_id": "grant_1", "operation_ref": "p1"}
    grant = {"name": "ops", "amount": {"amount": "5", "currency": "USD"}, "operation_ref": "g1"}
    for path, sent in ((ALLOCATION, body), ("/v1/admin/grants", grant)):
        first = await client.post(path, json=sent, headers=admin)
        again = await client.post(path, json=sent, headers=admin)
        assert (again.status_code, again.json()) == (first.status_code, first.json()) == (201, first.json())
        assert again.headers.get("Idempotent-Replayed") == "true", path


async def test_provisioning_an_actor_already_provisioned_is_409_already_provisioned(api) -> None:  # type: ignore[no-untyped-def]
    """Under a new reference nothing is applied, so the amount is not silently dropped: fund it instead."""
    client, _engine, admin, _alice = api
    body = {"amount": {"amount": "5", "currency": "USD"}, "grant_id": "grant_1", "operation_ref": "p1"}
    first = await client.post(ALLOCATION, json=body, headers=admin)
    refused = await client.post(ALLOCATION, json=body | {"operation_ref": "p2"}, headers=admin)
    assert refused.status_code == 409 and refused.json()["code"] == "already_provisioned"
    assert refused.json()["actor"] == first.json()
    assert "Idempotent-Replayed" not in refused.headers
