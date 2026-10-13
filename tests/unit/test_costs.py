"""ISS-02 / ISS-04: cost sync, classification, projections (corrected), summary and reconciliation."""

from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from livepeer_builder.contracts import (
    ActorContext,
    Attempt,
    Job,
    JobRequest,
    Money,
    ProvisionRequest,
    SyncCheckpoint,
    UsageRow,
)
from livepeer_builder.errors import JobFailed, RunnerCallError
from livepeer_builder.services.costs import classify
from livepeer_builder.testing import FakeAllocationManager, FakeUsageSource, FixedClock, ScriptedTransport
from tests.conftest import EngineFactory

APP = "example/echo"


def row(row_id: str, session: str | None, eth: str, *, usd: str | None = None, status: str = "applied",
        allocation: str = "alloc_1", manifest: str | None = None) -> UsageRow:  # fmt: skip
    return UsageRow(
        id=row_id, event_id=f"ev-{row_id}", status=status, manifest_id=manifest, allocation_id=allocation,  # type: ignore[arg-type]
        payment_session_id=session, request_id=None, pipeline="live", computed_fee_eth=Decimal(eth),
        computed_fee_usd=None if usd is None else Decimal(usd), currency="usd", created_at=FixedClock().now(),
    )  # fmt: skip


def job_with(*attempts: tuple[str, tuple[str, ...], bool], state: str = "succeeded") -> Job:
    job_id = uuid4()
    now = FixedClock().now()
    return Job(
        id=job_id, actor_id="alice", application_id="default", operation_ref=None, mode="immediate", app=APP,
        capability=None, model=None, state=state, failure=None, result=None, usage=None,  # type: ignore[arg-type]
        created_at=now, completed_at=now,
        attempts=tuple(
            Attempt(job_id=job_id, number=n, runner_url="r", orchestrator_url=None, auth_ids=auth, manifest_id=None,
                    payment_sent=paid, outcome=outcome, status_code=None, started_at=now, ended_at=now)  # type: ignore[arg-type]
            for n, (outcome, auth, paid) in enumerate(attempts, start=1)
        ),
    )  # fmt: skip


def test_classification_covers_every_class() -> None:
    rows = [row("1", "a-ok", "1"), row("2", "a-fail", "2"), row("3", "ignored", "9", status="quarantined")]
    cost = classify(job_with(("refused", ("a-fail",), True), ("succeeded", ("a-ok",), True)), rows)
    assert [(a.attempt_number, a.cost_class, a.fees.fee_eth) for a in cost.attempts] == [
        (1, "failed_after_payment", Decimal(2)),
        (2, "delivered", Decimal(1)),
    ]
    assert cost.status == "observed" and cost.fees.fee_eth == Decimal(3)

    uncertain = classify(job_with(("timeout", ("a-x",), True), state="uncertain"), [row("4", "a-x", "1")])
    assert uncertain.attempts[0].cost_class == "uncertain"

    waiting = classify(job_with(("succeeded", ("a-late",), True)), [])
    assert waiting.status == "pending" and waiting.attempts[0].cost_class == "pending"

    unpaid = classify(job_with(("refused", (), False)), [])
    assert unpaid.status == "none" and unpaid.attempts == ()


def test_usd_total_only_when_every_row_is_priced() -> None:
    job = job_with(("succeeded", ("s",), True))
    assert classify(job, [row("1", "s", "1", usd="2"), row("2", "s", "1", usd="3")]).fees.fee_usd == Decimal(
        5
    )
    assert classify(job, [row("1", "s", "1", usd="2"), row("2", "s", "1")]).fees.fee_usd is None


async def provisioned(make_engine: EngineFactory, admin: ActorContext):  # type: ignore[no-untyped-def]
    engine = await make_engine()
    request = ProvisionRequest(
        application_id="default", actor_id="alice", amount=Money(amount=Decimal(1), currency="ETH")
    )
    await engine.payments.provision(admin, request)
    return engine


async def test_sync_attributes_by_payment_session_and_marks_corrections(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, transport: ScriptedTransport,
    usage: FakeUsageSource, clock: FixedClock,
) -> None:  # fmt: skip
    engine = await make_engine(default_currency="ETH")
    await engine.payments.provision(
        admin,
        ProvisionRequest(
            application_id="default", actor_id="alice", amount=Money(amount=Decimal(1), currency="ETH")
        ),
    )
    transport.steps = [ScriptedTransport.ok(auth_ids=("sess-1",))]
    job = (await engine.jobs.run(alice, JobRequest(app=APP))).job
    assert (await engine.costs.for_job(alice, job.id)).status == "pending"  # missing evidence is not zero

    usage.rows = [row("u1", "sess-1", "0.5"), row("u2", "other", "9")]
    report = await engine.costs.sync_once()
    assert (report.rows_new, report.jobs_updated) == (2, 1)
    observed = await engine.costs.for_job(alice, job.id)
    assert observed.status == "observed" and observed.fees.fee_eth == Decimal("0.5")
    assert observed.attempts[0].cost_class == "delivered"

    again = await engine.costs.sync_once()  # re-reads the last page; nothing is applied twice
    assert again.rows_new == 0

    clock.advance(60)
    usage.rows.append(row("u3", "sess-1", "0.25"))  # a metered session signed again later
    await engine.costs.sync_once()
    corrected = await engine.costs.for_job(alice, job.id)
    assert corrected.status == "corrected" and corrected.fees.fee_eth == Decimal("0.75")


async def test_failed_after_payment_and_forged_manifest_reconcile(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, transport: ScriptedTransport,
    usage: FakeUsageSource, manager: FakeAllocationManager,
) -> None:  # fmt: skip
    engine = await make_engine(default_currency="ETH")
    actor = await engine.payments.provision(
        admin,
        ProvisionRequest(
            application_id="default", actor_id="alice", amount=Money(amount=Decimal(1), currency="ETH")
        ),
    )
    allocation = actor.allocation_id
    transport.steps = [
        ScriptedTransport.ok(auth_ids=("good",)),
        RunnerCallError("payment", "tickets rejected", payment_sent=True, auth_ids=("rejected",)),
    ]
    await engine.jobs.run(alice, JobRequest(app=APP))
    with pytest.raises(JobFailed):
        await engine.jobs.run(alice, JobRequest(app=APP))
    usage.rows = [
        row("1", "good", "0.2", allocation=allocation),
        row("2", "rejected", "0.3", allocation=allocation),  # charged at signing even though rejected
        row("3", "forged-session", "0.1", allocation=allocation, manifest="forged-by-client"),
    ]
    await engine.costs.sync_once()
    manager.allocations[allocation].spent = Decimal("0.6")

    result = await engine.costs.reconcile(admin, "default", "alice")
    assert result.by_class["delivered"].fee_eth == Decimal("0.2")
    assert result.by_class["failed_after_payment"].fee_eth == Decimal("0.3")
    assert result.unattributed.fee_eth == Decimal("0.1")  # a manifest label never attributes a charge
    assert result.balanced is True

    summary = await engine.costs.summary(
        alice, FixedClock().now() - timedelta(days=1), FixedClock().now() + timedelta(days=1)
    )
    assert summary.by_class["failed_after_payment"].fee_eth == Decimal("0.3")
    assert summary.jobs_with_cost == 2


async def test_cursor_mismatch_restarts_from_the_beginning(
    make_engine: EngineFactory, usage: FakeUsageSource
) -> None:
    engine = await make_engine()
    usage.rows = [row("1", "s", "1")]
    await engine.costs.sync_once()
    await engine.store.apply_usage((), SyncCheckpoint(resume_cursor="not-a-cursor"))  # Batteries answers 400
    report = await engine.costs.sync_once()
    assert (report.rows_seen, report.rows_new) == (1, 0)  # resynced from the start; nothing applied twice
