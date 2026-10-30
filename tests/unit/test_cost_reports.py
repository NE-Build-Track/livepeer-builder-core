"""ISS-18 (D60): cost reports a builder can rely on. Reporting failures never touch execution state, the
summary totals every class in its window and scope, and ``by_app`` attributes the same fees per app."""

import asyncio
from datetime import timedelta
from decimal import Decimal

import httpx
import pytest

from livepeer_builder.contracts import (
    ActorContext,
    CostSummary,
    Fees,
    JobRequest,
    Money,
    ProvisionRequest,
    UsagePage,
    UsageRow,
    add_fees,
)
from livepeer_builder.errors import (
    AccessDenied,
    InvalidRequest,
    JobFailed,
    NotFound,
    NotImplementedInMilestone,
    ProviderUnavailable,
    RunnerCallError,
)
from livepeer_builder.http import create_app
from livepeer_builder.runtime import BuilderEngine
from livepeer_builder.testing import FakeUsageSource, FixedClock, ScriptedTransport, runner
from tests.conftest import EngineFactory

APP = "example/echo"


async def provision(engine: BuilderEngine, admin: ActorContext, actor_id: str = "alice") -> str:
    request = ProvisionRequest(
        operation_ref=f"provision:default:{actor_id}",
        application_id="default",
        actor_id=actor_id,
        amount=Money(amount=Decimal(1), currency="ETH"),
        grant_id="grant_1",
    )
    return (await engine.payments.provision(admin, request)).allocation_id


async def test_batteries_down_leaves_a_succeeded_job_succeeded_with_its_cost_pending(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, transport: ScriptedTransport,
    usage: FakeUsageSource,
) -> None:  # fmt: skip  # the starter: reporting failures preserve execution state
    engine = await make_engine(default_currency="ETH")
    await provision(engine, admin)
    transport.steps = [ScriptedTransport.ok(manifest_ids=("m-1",))]
    job = (await engine.jobs.run(alice, JobRequest(app=APP))).job
    stop = asyncio.Event()

    async def batteries_down(cursor: str | None, limit: int = 1000) -> UsagePage:
        stop.set()  # one pass of the loop, then it stops
        raise ProviderUnavailable("batteries down")

    usage.page = batteries_down  # type: ignore[method-assign]
    await engine.costs.run(stop)  # the background sync loop: it logs and keeps the error for health

    assert engine.health().cost_sync_error == "batteries down"
    assert await engine.jobs.get(alice, job.id) == job  # the job record is unchanged
    cost = await engine.costs.for_job(alice, job.id)
    assert (cost.status, [attempt.cost_class for attempt in cost.attempts]) == ("pending", ["pending"])


async def test_a_refused_usage_report_leaves_the_job_and_its_usage_unchanged(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, bob: ActorContext,
    transport: ScriptedTransport,
) -> None:  # fmt: skip
    engine = await make_engine(default_currency="ETH")
    await provision(engine, admin)
    transport.steps = [
        ScriptedTransport.ok(manifest_ids=("m-ok",)),
        RunnerCallError("http", "bad input", payment_sent=True, manifest_ids=("m-bad",), status_code=400),
    ]
    succeeded = (await engine.jobs.run(alice, JobRequest(app=APP))).job
    with pytest.raises(JobFailed) as failed:
        await engine.jobs.run(alice, JobRequest(app=APP))
    reported = await engine.jobs.report_usage(alice, succeeded.id, {"tokens": 3})
    before = [await engine.jobs.get(alice, job_id) for job_id in (succeeded.id, failed.value.job.id)]

    with pytest.raises(NotFound):  # another actor's job: as if it did not exist
        await engine.jobs.report_usage(bob, succeeded.id, {"tokens": 9})
    with pytest.raises(InvalidRequest, match="succeeded"):
        await engine.jobs.report_usage(alice, failed.value.job.id, {"tokens": 9})
    with pytest.raises(InvalidRequest, match="already reported"):
        await engine.jobs.report_usage(alice, succeeded.id, {"tokens": 9})

    after = [await engine.jobs.get(alice, job_id) for job_id in (succeeded.id, failed.value.job.id)]
    assert after == before
    assert after[0].usage == reported.usage and after[1].usage is None
    assert [job.state for job in after] == ["succeeded", "failed"]


OTHER = "example/other"
RUNNERS = (
    runner(APP, url="https://a1", orchestrator_url="https://orch-a.example"),
    runner(APP, url="https://b1", orchestrator_url="https://orch-b.example"),
    runner(OTHER, url="https://o1", orchestrator_url="https://orch-a.example"),
)


def charge(row_id: str, manifest: str, eth: str, allocation: str) -> UsageRow:
    return UsageRow(
        id=row_id, event_id=f"ev-{row_id}", status="applied", manifest_id=manifest, allocation_id=allocation,
        payment_session_id=f"sess-{manifest}", request_id=None, pipeline="live", computed_fee_eth=Decimal(eth),
        computed_fee_usd=None, currency="eth", created_at=FixedClock().now(),
    )  # fmt: skip


async def every_class(
    engine: BuilderEngine, admin: ActorContext, actor: ActorContext, transport: ScriptedTransport,
    usage: FakeUsageSource,
) -> None:  # fmt: skip
    """Three jobs for ``actor``: on APP, one that failed over (0.2 failed after payment, then 0.1
    delivered) and one paid whose usage has not arrived (pending); on OTHER, one left uncertain (0.4)."""
    allocation = await provision(engine, admin, actor.actor_id)
    tag = actor.actor_id
    transport.steps = [
        RunnerCallError("refused", "busy", payment_sent=True, manifest_ids=(f"{tag}-a",), status_code=503),
        ScriptedTransport.ok(manifest_ids=(f"{tag}-b",)),
        RunnerCallError("timeout", "no answer", payment_sent=True, manifest_ids=(f"{tag}-u",)),
        ScriptedTransport.ok(manifest_ids=(f"{tag}-p",)),
    ]
    failed_over = (await engine.jobs.run(actor, JobRequest(app=APP, max_attempts=2))).job
    assert [attempt.runner_url for attempt in failed_over.attempts] == ["https://a1", "https://b1"]
    with pytest.raises(JobFailed) as uncertain:
        await engine.jobs.run(actor, JobRequest(app=OTHER))
    assert uncertain.value.job.state == "uncertain"
    await engine.jobs.run(actor, JobRequest(app=APP))
    usage.rows += [
        charge(f"{tag}-1", f"{tag}-a", "0.2", allocation),
        charge(f"{tag}-2", f"{tag}-b", "0.1", allocation),
        charge(f"{tag}-3", f"{tag}-u", "0.4", allocation),
    ]
    await engine.costs.sync_once()


WINDOW = (FixedClock().now() - timedelta(hours=1), FixedClock().now() + timedelta(hours=1))


def eth(fees: Fees) -> tuple[Decimal, int]:
    return fees.fee_eth, fees.row_count


def per_app(summary: CostSummary) -> dict[str, dict[str, tuple[Decimal, int]]]:
    return {app: {cls: eth(fees) for cls, fees in classes.items()} for app, classes in summary.by_app.items()}


async def test_the_summary_totals_every_class_with_a_failover_job(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, transport: ScriptedTransport,
    usage: FakeUsageSource,
) -> None:  # fmt: skip
    engine = await make_engine(RUNNERS, default_currency="ETH")
    await every_class(engine, admin, alice, transport, usage)
    summary = await engine.costs.summary(alice, *WINDOW)
    assert {cls: eth(fees) for cls, fees in summary.by_class.items()} == {
        "delivered": (Decimal("0.1"), 1),
        "failed_after_payment": (Decimal("0.2"), 1),  # the attempt the job failed over from (D52)
        "uncertain": (Decimal("0.4"), 1),
        "pending": (Decimal(0), 0),  # paid, usage not here yet: counted beside the totals
    }
    assert (summary.jobs_with_cost, summary.jobs_pending) == (2, 1)


async def test_the_summary_scope_is_the_actor_or_for_an_admin_the_application_or_one_actor(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, bob: ActorContext,
    transport: ScriptedTransport, usage: FakeUsageSource,
) -> None:  # fmt: skip
    engine = await make_engine(RUNNERS, default_currency="ETH")
    await every_class(engine, admin, alice, transport, usage)
    await every_class(engine, admin, bob, transport, usage)
    mine = await engine.costs.summary(alice, *WINDOW)
    everyone = await engine.costs.summary(admin, *WINDOW)
    just_bob = await engine.costs.summary(admin, *WINDOW, actor_id="bob")
    assert eth(mine.by_class["delivered"]) == eth(just_bob.by_class["delivered"]) == (Decimal("0.1"), 1)
    assert eth(everyone.by_class["delivered"]) == (Decimal("0.2"), 2)
    counts = [(summary.jobs_with_cost, summary.jobs_pending) for summary in (mine, everyone, just_bob)]
    assert counts == [(2, 1), (4, 2), (2, 1)]
    with pytest.raises(AccessDenied):  # an actor cannot widen its own scope
        await engine.costs.summary(alice, *WINDOW, actor_id="bob")


async def test_by_app_sums_to_the_class_total_in_the_same_window_and_scope(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, bob: ActorContext,
    transport: ScriptedTransport, usage: FakeUsageSource, clock: FixedClock,
) -> None:  # fmt: skip
    engine = await make_engine(RUNNERS, default_currency="ETH")
    await every_class(engine, admin, alice, transport, usage)
    await every_class(engine, admin, bob, transport, usage)
    clock.advance(2 * 3600)  # a later job on OTHER, outside the window
    transport.steps = [ScriptedTransport.ok(manifest_ids=("alice-late",))]
    await engine.jobs.run(alice, JobRequest(app=OTHER))
    allocation = (await engine.payments.allowance(alice)).allocation_id
    usage.rows.append(charge("late", "alice-late", "5", allocation))
    await engine.costs.sync_once()

    mine = await engine.costs.summary(alice, *WINDOW)
    everyone = await engine.costs.summary(admin, *WINDOW)
    just_bob = await engine.costs.summary(admin, *WINDOW, actor_id="bob")
    later = await engine.costs.summary(alice, WINDOW[1], WINDOW[1] + timedelta(hours=2))
    assert per_app(mine) == {
        APP: {  # final and still-settling fees stay apart, as in by_class (D60)
            "delivered": (Decimal("0.1"), 1),
            "failed_after_payment": (Decimal("0.2"), 1),
            "pending": (Decimal(0), 0),  # paid, usage not here yet
        },
        OTHER: {"uncertain": (Decimal("0.4"), 1)},  # the late job is outside the window
    }
    assert per_app(everyone) == {
        APP: {
            "delivered": (Decimal("0.2"), 2),
            "failed_after_payment": (Decimal("0.4"), 2),
            "pending": (Decimal(0), 0),
        },
        OTHER: {"uncertain": (Decimal("0.8"), 2)},
    }
    assert per_app(later) == {OTHER: {"delivered": (Decimal(5), 1)}}
    for summary in (mine, everyone, just_bob, later):
        for cls, total in summary.by_class.items():  # for every class, the apps add up to its total
            assert add_fees(classes[cls] for classes in summary.by_app.values() if cls in classes) == total


async def test_rest_returns_by_app_and_the_schema_declares_it(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, transport: ScriptedTransport,
    usage: FakeUsageSource,
) -> None:  # fmt: skip
    engine = await make_engine(RUNNERS, default_currency="ETH")
    await every_class(engine, admin, alice, transport, usage)
    key = await engine.keys.issue(admin, kind="actor", actor_id="alice", operation_ref="issue-cost-reports")
    app = create_app(engine, manage_lifecycle=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://engine") as client:
        start, end = (moment.isoformat() for moment in WINDOW)
        answer = await client.get(
            "/v1/costs/summary",
            params={"start": start, "end": end},
            headers={"Authorization": f"Bearer {key.token}"},
        )
        schema = (await client.get("/openapi.json")).json()["components"]["schemas"]["CostSummary"]
    assert answer.status_code == 200
    assert per_app(CostSummary.model_validate(answer.json())) == {
        APP: {
            "delivered": (Decimal("0.1"), 1),
            "failed_after_payment": (Decimal("0.2"), 1),
            "pending": (Decimal(0), 0),
        },
        OTHER: {"uncertain": (Decimal("0.4"), 1)},
    }
    per_class = schema["properties"]["by_app"]["additionalProperties"]
    assert per_class["additionalProperties"] == {"$ref": "#/components/schemas/Fees"}


async def test_the_events_feed_answers_501_naming_the_hold(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext
) -> None:  # the feed stays held in M3 (decision of 21 October, item 4; D60)
    engine = await make_engine()
    with pytest.raises(NotImplementedInMilestone) as held:
        await engine.events.after(alice, 0)
    assert held.value.milestone == "held" and held.value.ticket == "ISS-18"
    key = await engine.keys.issue(admin, kind="actor", actor_id="alice", operation_ref="issue-events")
    app = create_app(engine, manage_lifecycle=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://engine") as client:
        answer = await client.get("/v1/events", headers={"Authorization": f"Bearer {key.token}"})
    assert answer.status_code == 501
    assert answer.json() == {
        "error": "the engine events feed is defined but held by the M3 scope decision of 21 October 2026 "
        "(item 4, D60); see ISS-18",
        "milestone": "held",
        "ticket": "ISS-18",
    }
