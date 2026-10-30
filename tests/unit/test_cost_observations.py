"""ISS-23 gap tests for late or duplicate cost observations (D61).

Batteries is the authority and the engine keeps a cached copy of each usage row, keyed by its id. These
pin what the copy does with rows Batteries marks as not charged, with a row id delivered again with
another fee (Q2: ignored and logged, never a correction), and with a row that arrives after retention
purged its job.
"""

import logging
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest

from livepeer_builder import BuilderEngine
from livepeer_builder.contracts import ActorContext, Job, JobRequest, Money, ProvisionRequest
from livepeer_builder.testing import FakeAllocationManager, FakeUsageSource, FixedClock, ScriptedTransport
from tests.conftest import EngineFactory
from tests.unit.test_costs import row

APP = "example/echo"


async def paid_job(
    make_engine: EngineFactory,
    admin: ActorContext,
    alice: ActorContext,
    transport: ScriptedTransport,
    **settings: Any,
) -> tuple[BuilderEngine, Job, str]:
    """An engine, a succeeded job of alice's that signed manifest ``m-1``, and her allocation."""
    engine = await make_engine(default_currency="ETH", **settings)
    actor = await engine.payments.provision(
        admin,
        ProvisionRequest(
            operation_ref="provision:default:alice", application_id="default", actor_id="alice",
            amount=Money(amount=Decimal(1), currency="ETH"), grant_id="grant_1",
        ),
    )  # fmt: skip
    transport.steps = [ScriptedTransport.ok(manifest_ids=("m-1",))]
    return engine, (await engine.jobs.run(alice, JobRequest(app=APP))).job, actor.allocation_id


async def test_a_redelivered_row_with_another_fee_is_ignored_and_logged(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, transport: ScriptedTransport,
    usage: FakeUsageSource, caplog: pytest.LogCaptureFixture,
) -> None:  # fmt: skip
    engine, job, allocation = await paid_job(make_engine, admin, alice, transport)
    usage.rows = [row("u1", "s-1", "0.5", allocation=allocation, manifest="m-1")]
    await engine.costs.sync_once()
    caplog.set_level(logging.INFO, logger="livepeer_builder")

    await engine.costs.sync_once()  # the last page is read again on every sync: the same row, no noise
    assert not [r for r in caplog.records if getattr(r, "event", "") == "costs.usage_redelivered"]

    usage.rows = [row("u1", "s-1", "0.9", allocation=allocation, manifest="m-1")]
    report = await engine.costs.sync_once()
    assert report.rows_new == 0
    cost = await engine.costs.for_job(alice, job.id)
    assert (cost.status, cost.fees.fee_eth) == ("observed", Decimal("0.5"))  # not a correction (Q2)
    [logged] = [r for r in caplog.records if getattr(r, "event", "") == "costs.usage_redelivered"]
    assert logged.levelno == logging.WARNING
    assert (logged.row_id, logged.stored_fee_eth, logged.delivered_fee_eth) == ("u1", "0.5", "0.9")  # type: ignore[attr-defined]


@pytest.mark.parametrize("status", ["duplicate", "ignored"])
async def test_rows_batteries_marks_duplicate_or_ignored_are_never_counted(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, transport: ScriptedTransport,
    usage: FakeUsageSource, manager: FakeAllocationManager, clock: FixedClock, status: str,
) -> None:  # fmt: skip
    engine, job, allocation = await paid_job(make_engine, admin, alice, transport)
    usage.rows = [
        row("u1", "s-1", "0.5", allocation=allocation, manifest="m-1"),
        row("u2", "s-1", "0.5", allocation=allocation, manifest="m-1", status=status),  # on the job
        row("u3", "s-x", "0.7", allocation=allocation, manifest="m-x", status=status),  # on no job
    ]
    assert (await engine.costs.sync_once()).rows_new == 3  # cached, as Batteries reports them
    cost = await engine.costs.for_job(alice, job.id)
    assert (cost.fees.fee_eth, cost.fees.row_count) == (Decimal("0.5"), 1)
    window = (clock.now() - timedelta(days=1), clock.now() + timedelta(days=1))
    assert (await engine.costs.summary(alice, *window)).by_class["delivered"].fee_eth == Decimal("0.5")
    manager.allocations[allocation].spent = Decimal("0.5")
    reconciled = await engine.costs.reconcile(admin, "default", "alice")
    assert reconciled.unattributed.row_count == 0 and reconciled.balanced is True


async def test_a_late_row_for_a_job_retention_purged_stays_unattributed(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, transport: ScriptedTransport,
    usage: FakeUsageSource, manager: FakeAllocationManager, clock: FixedClock,
) -> None:  # fmt: skip
    engine, job, allocation = await paid_job(make_engine, admin, alice, transport, retention_jobs_days=30)
    clock.advance(31 * 86400)
    assert (await engine.purge()).jobs_deleted == 1  # the job, its attempts and its signed sessions

    usage.rows = [row("late", "s-1", "0.5", allocation=allocation, manifest="m-1")]
    assert (await engine.costs.sync_once()).jobs_updated == 0  # no job to attribute it to
    manager.allocations[allocation].spent = Decimal("0.5")
    reconciled = await engine.costs.reconcile(admin, "default", "alice")
    assert reconciled.unattributed.fee_eth == Decimal("0.5") and reconciled.balanced is True
    assert all(fees.row_count == 0 for fees in reconciled.by_class.values())
    assert [cost.job_id for cost in await engine.costs.recent(alice)] == []
    assert job.id not in {cost.job_id for cost in await engine.costs.recent(admin)}
