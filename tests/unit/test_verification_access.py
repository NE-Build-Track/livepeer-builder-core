"""ISS-23 gap tests for access: cross-actor isolation, revoked credentials, and repeated admin commands.

Each one is a row in docs/product-specs/m3-traceability.md. Rows other guides own (D57's repeat rules,
D58's payment refusals) are cited there, not repeated here.
"""

import logging
from collections.abc import AsyncIterator
from datetime import timedelta
from decimal import Decimal

import httpx
import pytest

from livepeer_builder import BuilderEngine
from livepeer_builder.contracts import ActorContext, FundRequest, JobRequest, Money, ProvisionRequest
from livepeer_builder.errors import AccessDenied, InvalidRequest, JobFailed, NotFound
from livepeer_builder.http import create_app
from livepeer_builder.testing import FakeAllocationManager, FakeUsageSource, FixedClock, ScriptedTransport
from tests.conftest import EngineFactory
from tests.unit.test_costs import row

APP = "example/echo"


async def provisioned(make_engine: EngineFactory, admin: ActorContext, *actors: str) -> BuilderEngine:
    engine = await make_engine()
    for actor_id in actors:
        await engine.payments.provision(
            admin,
            ProvisionRequest(
                operation_ref=f"provision:default:{actor_id}", application_id="default", actor_id=actor_id,
                amount=Money(amount=Decimal("5"), currency="USD"), grant_id="grant_1",
            ),
        )  # fmt: skip
    return engine


@pytest.fixture
async def rest(
    make_engine: EngineFactory, admin: ActorContext
) -> AsyncIterator[tuple[httpx.AsyncClient, BuilderEngine]]:
    engine = await provisioned(make_engine, admin, "alice", "bob")
    app = create_app(engine, manage_lifecycle=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://engine") as client:
        yield client, engine


async def bearer(engine: BuilderEngine, admin: ActorContext, kind: str, actor_id: str) -> dict[str, str]:
    issued = await engine.keys.issue(admin, kind=kind, actor_id=actor_id, operation_ref=f"key-{actor_id}")  # type: ignore[arg-type]
    return {"Authorization": f"Bearer {issued.token}"}


# -- cross-actor ----------------------------------------------------------------------------------------


async def test_another_actors_operation_ref_runs_their_own_job_and_never_shows_the_first(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, bob: ActorContext,
    transport: ScriptedTransport,
) -> None:  # fmt: skip
    engine = await provisioned(make_engine, admin, "alice", "bob")
    transport.steps = [ScriptedTransport.ok(), ScriptedTransport.ok()]
    alices = (await engine.jobs.run(alice, JobRequest(app=APP, operation_ref="order-1"))).job
    bobs = (await engine.jobs.run(bob, JobRequest(app=APP, operation_ref="order-1"))).job
    assert bobs.id != alices.id and bobs.actor_id == "bob" and bobs.state == "succeeded"
    assert len(transport.calls) == 2  # Bob's job ran; it was not answered from Alice's
    assert [job.id for job in await engine.jobs.list_jobs(bob)] == [bobs.id]


async def test_another_actors_operation_ref_over_rest_never_shows_the_first_job(
    rest: tuple[httpx.AsyncClient, BuilderEngine], admin: ActorContext, transport: ScriptedTransport
) -> None:
    client, engine = rest
    as_alice, as_bob = (
        await bearer(engine, admin, "actor", "alice"),
        await bearer(engine, admin, "actor", "bob"),
    )
    transport.steps = [ScriptedTransport.ok(), ScriptedTransport.ok()]
    body = {"app": APP, "operation_ref": "order-1"}
    first = await client.post("/v1/jobs", json=body, headers=as_alice)
    second = await client.post("/v1/jobs", json=body, headers=as_bob)
    assert (first.status_code, second.status_code) == (200, 200)
    alices = first.headers["livepeer-job-id"]
    assert second.headers["livepeer-job-id"] != alices and alices not in second.text
    listed = (await client.get("/v1/jobs", headers=as_bob)).json()
    assert [job["id"] for job in listed] == [second.headers["livepeer-job-id"]]
    assert (await client.get(f"/v1/jobs/{alices}", headers=as_bob)).status_code == 404


async def test_cost_summary_recent_costs_and_allowance_leave_out_another_actors_fees(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, bob: ActorContext,
    transport: ScriptedTransport, usage: FakeUsageSource, clock: FixedClock,
) -> None:  # fmt: skip
    engine = await provisioned(make_engine, admin, "alice", "bob")
    await engine.payments.fund(
        admin,
        "default",
        "bob",
        FundRequest(amount=Money(amount=Decimal("2"), currency="USD"), operation_ref="b"),
    )
    transport.steps = [
        ScriptedTransport.ok(manifest_ids=("m-alice",)),
        ScriptedTransport.ok(manifest_ids=("m-bob",)),
    ]
    alices = (await engine.jobs.run(alice, JobRequest(app=APP))).job
    bobs = (await engine.jobs.run(bob, JobRequest(app=APP))).job
    allocation = {a: (await engine.store.get_actor("default", a)).allocation_id for a in ("alice", "bob")}  # type: ignore[union-attr]
    usage.rows = [
        row("u-a", "s-alice", "0", usd="0.25", allocation=allocation["alice"], manifest="m-alice"),
        row("u-b", "s-bob", "0", usd="4", allocation=allocation["bob"], manifest="m-bob"),
    ]
    await engine.costs.sync_once()

    window = (clock.now() - timedelta(days=1), clock.now() + timedelta(days=1))
    summary = await engine.costs.summary(alice, *window)
    assert summary.by_class["delivered"].fee_usd == Decimal("0.25") and summary.jobs_with_cost == 1
    assert [cost.job_id for cost in await engine.costs.recent(alice)] == [alices.id]
    assert (await engine.payments.allowance(alice)).granted == Money(amount=Decimal("5"), currency="USD")
    assert (await engine.costs.summary(bob, *window)).by_class["delivered"].fee_usd == Decimal("4")
    assert (await engine.payments.allowance(bob)).granted == Money(amount=Decimal("7"), currency="USD")
    assert [cost.job_id for cost in await engine.costs.recent(bob)] == [bobs.id]
    with pytest.raises(AccessDenied):  # naming the other actor is refused, not answered
        await engine.costs.summary(alice, *window, actor_id="bob")
    with pytest.raises(AccessDenied):
        await engine.payments.allowance(alice, "bob")


# -- revoked credentials --------------------------------------------------------------------------------


async def test_a_job_after_the_allocation_is_revoked_fails_not_provisioned_without_a_runner_call(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, transport: ScriptedTransport
) -> None:
    engine = await provisioned(make_engine, admin, "alice")
    await engine.payments.revoke(admin, "default", "alice")
    transport.steps = [ScriptedTransport.ok()]
    with pytest.raises(JobFailed) as failed:
        await engine.jobs.run(alice, JobRequest(app=APP))
    assert failed.value.job.failure is not None and failed.value.job.failure.kind == "not_provisioned"
    assert transport.calls == []  # no payment, and no fallback to another key


@pytest.mark.parametrize("kind", ["admin", "auditor"])
async def test_a_revoked_admin_or_auditor_key_is_401_on_admin_routes(
    rest: tuple[httpx.AsyncClient, BuilderEngine], admin: ActorContext, kind: str
) -> None:
    client, engine = rest
    issued = await engine.keys.issue(admin, kind=kind, actor_id=f"{kind}-1", operation_ref=f"key-{kind}")  # type: ignore[arg-type]
    headers = {"Authorization": f"Bearer {issued.token}"}
    reads = ("/v1/admin/keys", "/v1/admin/actors", "/v1/admin/applications/default/actors/alice/allocation")
    assert [(await client.get(path, headers=headers)).status_code for path in reads] == [200, 200, 200]
    await engine.keys.revoke(admin, issued.key.key_id)
    assert [(await client.get(path, headers=headers)).status_code for path in reads] == [401, 401, 401]
    status = "/v1/admin/applications/default/actors/alice/allocation/status"
    assert (await client.post(status, json={"status": "paused"}, headers=headers)).status_code == 401


# -- repeated admin commands that take no operation_ref (set-to-state or repeat-safe, D57) ---------------


async def test_repeating_set_status_revoke_allocation_or_revoke_key_changes_nothing_more(
    make_engine: EngineFactory, admin: ActorContext, manager: FakeAllocationManager,
    caplog: pytest.LogCaptureFixture,
) -> None:  # fmt: skip
    engine = await provisioned(make_engine, admin, "alice")
    allocation = (await engine.store.get_actor("default", "alice")).allocation_id  # type: ignore[union-attr]
    paused = [await engine.payments.set_status(admin, "default", "alice", "paused") for _ in range(2)]
    assert [actor.status for actor in paused] == ["paused", "paused"]
    assert manager.allocations[allocation].status == "paused"

    revoked = await engine.payments.revoke(admin, "default", "alice")
    manager.fail_once("revoke_allocation", AssertionError("the repeat reached the provider"))
    # a retry after a lost answer gets the first answer: the revoked actor, with nothing revoked twice (D61)
    assert await engine.payments.revoke(admin, "default", "alice") == revoked
    assert await engine.store.get_actor("default", "alice") == revoked
    with pytest.raises(NotFound):  # an actor never provisioned is still not found
        await engine.payments.revoke(admin, "default", "nobody")
    with pytest.raises(NotFound):
        await engine.payments.set_status(admin, "default", "alice", "active")  # no un-revoking by status

    key = (await engine.keys.issue(admin, kind="actor", actor_id="alice", operation_ref="k")).key
    caplog.set_level(logging.INFO, logger="livepeer_builder")
    await engine.keys.revoke(admin, key.key_id)
    first = await engine.keys.get(admin, key.key_id)
    await engine.keys.revoke(admin, key.key_id)  # a retry succeeds as the first did (D61)
    assert await engine.keys.get(admin, key.key_id) == first  # revoked_at kept
    with pytest.raises(InvalidRequest, match="no active key"):  # a key that never existed
        await engine.keys.revoke(admin, "lpk_unknown")
    assert len([r for r in caplog.records if getattr(r, "event", "") == "keys.revoked"]) == 1


async def test_repeating_set_status_revoke_allocation_or_revoke_key_over_rest(
    rest: tuple[httpx.AsyncClient, BuilderEngine], admin: ActorContext
) -> None:
    client, engine = rest
    as_admin = await bearer(engine, admin, "admin", "ops")
    key = (await engine.keys.issue(admin, kind="actor", actor_id="bob", operation_ref="k")).key
    allocation = "/v1/admin/applications/default/actors/alice/allocation"

    async def twice(path: str, body: dict[str, str] | None = None) -> list[int]:
        return [(await client.post(path, json=body, headers=as_admin)).status_code for _ in range(2)]

    assert await twice(allocation + "/status", {"status": "paused"}) == [200, 200]
    assert await twice(allocation + "/revoke") == [200, 200]  # a retry gets the first answer (D61)
    assert (await client.get(allocation, headers=as_admin)).json()["status"] == "revoked"
    assert await twice(f"/v1/admin/keys/{key.key_id}/revoke") == [204, 204]
    assert (
        await client.post(allocation.replace("alice", "nobody") + "/revoke", headers=as_admin)
    ).status_code == 404
    assert (await client.post("/v1/admin/keys/lpk_unknown/revoke", headers=as_admin)).status_code == 400
    assert (await client.get(f"/v1/admin/keys/{key.key_id}", headers=as_admin)).json()["revoked_at"]
