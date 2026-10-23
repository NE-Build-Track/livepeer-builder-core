"""ISS-02 / ISS-04: cost sync, classification, projections (corrected), summary and reconciliation."""

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from livepeer_builder.contracts import (
    ActorContext,
    Attempt,
    Job,
    JobRequest,
    Money,
    ProvisionRequest,
    SignedSession,
    SyncCheckpoint,
    UsageRow,
)
from livepeer_builder.errors import JobFailed, RunnerCallError
from livepeer_builder.runtime import BuilderEngine
from livepeer_builder.runtime.assembly import EngineSupply
from livepeer_builder.services.costs import classify
from livepeer_builder.store import SqlStore
from livepeer_builder.testing import (
    FakeAllocationManager,
    FakeUsageSource,
    FixedClock,
    ScriptedTransport,
    StreamScript,
)
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
            Attempt(
                job_id=job_id, number=n, runner_url="r", orchestrator_url=None, allocation_id="alloc_1",
                sessions=tuple(
                    SignedSession(manifest_id=f"m-{item}", payment_session_id=item, match="matched")
                    for item in auth
                ),
                payment_sent=paid, outcome=outcome, status_code=None, started_at=now, ended_at=now,  # type: ignore[arg-type]
            )
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
        application_id="default",
        actor_id="alice",
        amount=Money(amount=Decimal(1), currency="ETH"),
        grant_id="grant_1",
    )
    await engine.payments.provision(admin, request)
    return engine


async def test_sync_attributes_by_payment_session_and_marks_corrections(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, transport: ScriptedTransport,
    usage: FakeUsageSource, clock: FixedClock,
) -> None:  # fmt: skip
    engine = await make_engine(default_currency="ETH")
    actor = await engine.payments.provision(
        admin,
        ProvisionRequest(
            application_id="default",
            actor_id="alice",
            amount=Money(amount=Decimal(1), currency="ETH"),
            grant_id="grant_1",
        ),
    )
    transport.steps = [ScriptedTransport.ok(manifest_ids=("m-sess",))]
    job = (await engine.jobs.run(alice, JobRequest(app=APP))).job
    assert (await engine.costs.for_job(alice, job.id)).status == "pending"  # missing evidence is not zero
    assert (await engine.costs.sync_once()).jobs_updated == 1  # the worker persists the pending projection

    allocation = actor.allocation_id
    usage.rows = [
        row("u1", "sess-1", "0.5", allocation=allocation, manifest="m-sess"),
        row("u2", "other", "9", allocation=allocation, manifest="other"),
    ]
    report = await engine.costs.sync_once()
    assert (report.rows_new, report.jobs_updated) == (2, 1)
    observed = await engine.costs.for_job(alice, job.id)
    assert observed.status == "observed" and observed.fees.fee_eth == Decimal("0.5")
    assert observed.attempts[0].cost_class == "delivered"

    await engine.store.save_projection(observed.model_copy(update={"attempts": ()}))
    assert (await engine.costs.for_job(alice, job.id)).attempts == ()
    rebuilt_sync = await engine.costs.sync_once()  # no new usage; the empty attempt list is stale
    assert rebuilt_sync.rows_new == 0
    rebuilt = await engine.costs.for_job(alice, job.id)
    assert rebuilt.attempts[0].cost_class == "delivered" and rebuilt.fees.fee_eth == Decimal("0.5")

    again = await engine.costs.sync_once()  # re-reads the last page; nothing is applied twice
    assert again.rows_new == 0

    clock.advance(60)
    usage.rows.append(row("u3", "sess-1", "0.25", allocation=allocation, manifest="m-sess"))
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
            application_id="default",
            actor_id="alice",
            amount=Money(amount=Decimal(1), currency="ETH"),
            grant_id="grant_1",
        ),
    )
    allocation = actor.allocation_id
    transport.steps = [
        ScriptedTransport.ok(manifest_ids=("m-good",)),
        RunnerCallError("payment", "tickets rejected", payment_sent=True, manifest_ids=("m-rejected",)),
    ]
    await engine.jobs.run(alice, JobRequest(app=APP))
    with pytest.raises(JobFailed):
        await engine.jobs.run(alice, JobRequest(app=APP))
    usage.rows = [
        row("1", "good", "0.2", allocation=allocation, manifest="m-good"),
        row("2", "rejected", "0.3", allocation=allocation, manifest="m-rejected"),
        row("3", "forged-session", "0.1", allocation=allocation, manifest="forged-by-client"),
    ]
    await engine.costs.sync_once()
    manager.allocations[allocation].spent = Decimal("0.6")

    result = await engine.costs.reconcile(admin, "default", "alice")
    assert result.by_class["delivered"].fee_eth == Decimal("0.2")
    assert result.by_class["failed_after_payment"].fee_eth == Decimal("0.3")
    assert result.unattributed.fee_eth == Decimal("0.1")  # no session log entry for that manifest
    assert result.balanced is True

    summary = await engine.costs.summary(
        alice, FixedClock().now() - timedelta(days=1), FixedClock().now() + timedelta(days=1)
    )
    assert summary.by_class["failed_after_payment"].fee_eth == Decimal("0.3")
    assert summary.jobs_with_cost == 2


async def test_rows_that_arrive_before_the_attempt_match_on_the_next_sync(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, transport: ScriptedTransport,
    usage: FakeUsageSource,
) -> None:  # fmt: skip
    engine = await make_engine(default_currency="ETH")
    actor = await engine.payments.provision(
        admin,
        ProvisionRequest(
            application_id="default",
            actor_id="alice",
            amount=Money(amount=Decimal(1), currency="ETH"),
            grant_id="grant_1",
        ),
    )
    usage.rows = [row("u1", "sess-1", "0.4", allocation=actor.allocation_id, manifest="m-early")]
    await engine.costs.sync_once()
    transport.steps = [ScriptedTransport.ok(manifest_ids=("m-early",))]
    job = (await engine.jobs.run(alice, JobRequest(app=APP))).job
    assert (await engine.costs.for_job(alice, job.id)).status == "pending"

    report = await engine.costs.sync_once()  # no new rows; the unmatched log entry resolves
    assert report.rows_new == 0
    observed = await engine.costs.for_job(alice, job.id)
    assert observed.status == "observed" and observed.fees.fee_eth == Decimal("0.4")


async def test_a_manifest_shared_by_two_sessions_is_uncertain_and_unattributed(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, transport: ScriptedTransport,
    usage: FakeUsageSource, manager: FakeAllocationManager,
) -> None:  # fmt: skip
    engine = await make_engine(default_currency="ETH")
    actor = await engine.payments.provision(
        admin,
        ProvisionRequest(
            application_id="default",
            actor_id="alice",
            amount=Money(amount=Decimal(1), currency="ETH"),
            grant_id="grant_1",
        ),
    )
    allocation = actor.allocation_id
    transport.steps = [ScriptedTransport.ok(manifest_ids=("m-shared",))]
    job = (await engine.jobs.run(alice, JobRequest(app=APP))).job
    usage.rows = [row("1", "sess-a", "0.2", allocation=allocation, manifest="m-shared")]
    await engine.costs.sync_once()
    assert (await engine.costs.for_job(alice, job.id)).attempts[0].cost_class == "delivered"

    usage.rows.append(row("2", "sess-b", "0.3", allocation=allocation, manifest="m-shared"))
    await engine.costs.sync_once()
    cost = await engine.costs.for_job(alice, job.id)
    assert cost.attempts[0].cost_class == "uncertain"
    assert cost.attempts[0].fees.row_count == 0
    stored = await engine.jobs.get(alice, job.id)
    assert stored.attempts[0].sessions[0].match == "ambiguous"
    assert stored.attempts[0].sessions[0].payment_session_id is None

    manager.allocations[allocation].spent = Decimal("0.5")
    result = await engine.costs.reconcile(admin, "default", "alice")
    assert result.unattributed.fee_eth == Decimal("0.5")
    assert result.by_class["uncertain"].fee_eth == Decimal(0)
    assert result.balanced is True


async def test_a_forged_manifest_on_another_allocation_stays_unattributed(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, transport: ScriptedTransport,
    usage: FakeUsageSource,
) -> None:  # fmt: skip
    engine = await make_engine(default_currency="ETH")
    actor = await engine.payments.provision(
        admin,
        ProvisionRequest(
            application_id="default",
            actor_id="alice",
            amount=Money(amount=Decimal(1), currency="ETH"),
            grant_id="grant_1",
        ),
    )
    transport.steps = [ScriptedTransport.ok(manifest_ids=("m-real",))]
    job = (await engine.jobs.run(alice, JobRequest(app=APP))).job
    usage.rows = [
        row("1", "sess-real", "0.2", allocation=actor.allocation_id, manifest="m-real"),
        row("2", "sess-forged", "0.9", allocation="someone-else", manifest="m-real"),
    ]
    await engine.costs.sync_once()
    observed = await engine.costs.for_job(alice, job.id)
    assert observed.status == "observed" and observed.fees.fee_eth == Decimal("0.2")
    assert [item.id for item in await engine.store.unattributed_usage("someone-else")] == ["2"]


async def test_cursor_mismatch_restarts_from_the_beginning(
    make_engine: EngineFactory, usage: FakeUsageSource
) -> None:
    engine = await make_engine()
    usage.rows = [row("1", "s", "1")]
    await engine.costs.sync_once()
    await engine.store.apply_usage((), SyncCheckpoint(resume_cursor="not-a-cursor"))  # Batteries answers 400
    report = await engine.costs.sync_once()
    assert (report.rows_seen, report.rows_new) == (1, 0)  # resynced from the start; nothing applied twice


async def running_job(
    store: SqlStore, clock: FixedClock, *, paid: bool = False, allocation: str = "alloc_1"
) -> tuple[Job, Attempt]:
    """A job with one attempt still in flight, as the engine stores it before the runner answers."""
    now = clock.now()
    job = Job(
        id=uuid4(), actor_id="alice", application_id="default", operation_ref=None, mode="immediate", app=APP,
        capability=None, model=None, state="running", attempts=(), failure=None, result=None, usage=None,
        created_at=now, completed_at=None,
    )  # fmt: skip
    await store.create_job(job, None)
    attempt = Attempt(
        job_id=job.id, number=1, runner_url="r", orchestrator_url=None, allocation_id=allocation,
        sessions=(SignedSession(manifest_id="m-run"),) if paid else (), payment_sent=paid,
        outcome="in_flight", status_code=None, started_at=now, ended_at=None,
    )  # fmt: skip
    await store.start_attempt(attempt)
    return job, attempt


async def test_a_sync_during_a_job_never_leaves_a_paid_job_reading_as_none(
    make_engine: EngineFactory, alice: ActorContext, store: SqlStore, clock: FixedClock
) -> None:  # #33: the sync stored "none" for a job still signing, and the read served it after payment
    engine = await make_engine()
    job, attempt = await running_job(store, clock)  # still signing: nothing paid yet
    await engine.costs.sync_once()
    clock.advance(5)
    paid = attempt.model_copy(
        update={"sessions": (SignedSession(manifest_id="m-1"),), "payment_sent": True,
                "outcome": "succeeded", "status_code": 200, "ended_at": clock.now()}
    )  # fmt: skip
    await store.finish_attempt(paid)
    await store.finish_job(job.id, "succeeded", None, None, clock.now())
    assert (await engine.costs.for_job(alice, job.id)).status == "pending"  # paid, usage not here yet


async def test_a_running_job_reads_pending_never_zero(
    make_engine: EngineFactory, alice: ActorContext, store: SqlStore, clock: FixedClock
) -> None:
    engine = await make_engine()
    job, _ = await running_job(store, clock)  # it may still pay, so its cost is not known to be zero
    cost = await engine.costs.for_job(alice, job.id)
    assert cost.status == "pending"
    await engine.costs.sync_once()
    assert await store.get_projection(job.id) is None  # only an ended job is projected


async def test_a_job_that_ended_since_the_last_sync_counts_in_summary_and_recent(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, transport: ScriptedTransport,
    clock: FixedClock,
) -> None:  # fmt: skip
    engine = await make_engine()
    await engine.payments.provision(
        admin,
        ProvisionRequest(application_id="default", actor_id="alice", amount=Money(amount=Decimal(1), currency="USD"),
                         grant_id="grant_1"),
    )  # fmt: skip
    transport.steps = [ScriptedTransport.ok(manifest_ids=("m-new",))]
    job = (await engine.jobs.run(alice, JobRequest(app=APP))).job  # no sync since it ended
    summary = await engine.costs.summary(
        alice, clock.now() - timedelta(hours=1), clock.now() + timedelta(hours=1)
    )
    assert summary.jobs_pending == 1
    assert [cost.job_id for cost in await engine.costs.recent(alice)] == [job.id]


async def test_a_none_projection_an_older_release_stored_for_a_running_job_is_not_served(
    make_engine: EngineFactory, alice: ActorContext, store: SqlStore, clock: FixedClock
) -> None:  # an upgraded database can hold #33's rows; a read must not trust them
    engine = await make_engine()
    job, _ = await running_job(store, clock, paid=True)
    old = classify(job.model_copy(update={"state": "succeeded"}), ())  # what the old sync stored
    await store.save_projection(
        old.model_copy(update={"status": "none", "attempts": (), "updated_at": clock.now()})
    )
    assert (await engine.costs.for_job(alice, job.id)).status == "pending"


def test_a_running_job_keeps_matched_fees_pending_until_it_ends() -> None:
    running = job_with(("in_flight", ("s1",), True), state="running")
    cost = classify(running, [row("1", "s1", "0.5")])
    assert cost.status == "pending" and cost.fees.fee_eth == Decimal("0.5")
    assert [(a.cost_class, a.fees.fee_eth) for a in cost.attempts] == [("pending", Decimal("0.5"))]


class SyncsAfterFirstRead:
    """The engine's store, except that right after a cost read's first query, ``between`` runs (a
    concurrent cost sync writing). Reads inside ``cost_snapshot`` count as queries too."""

    def __init__(self, store: SqlStore, between: Callable[[], Awaitable[None]]) -> None:
        self._store = store
        self._between: Callable[[], Awaitable[None]] | None = between

    async def _after(self, result: Any) -> Any:
        between, self._between = self._between, None
        if between is not None:
            await between()
        return result

    def _wrap(self, target: Any) -> Any:
        owner = self

        class Wrapped:
            def __getattr__(self, name: str) -> Any:
                attribute = getattr(target, name)
                if name == "cost_snapshot":
                    return owner._snapshot(attribute)
                if not callable(attribute) or not asyncio.iscoroutinefunction(attribute):
                    return attribute

                async def read(*args: Any, **kwargs: Any) -> Any:
                    return await owner._after(await attribute(*args, **kwargs))

                return read

        return Wrapped()

    def _snapshot(self, open_snapshot: Callable[[], Any]) -> Callable[[], Any]:
        owner = self

        @contextlib.asynccontextmanager
        async def snapshot() -> AsyncIterator[Any]:
            async with open_snapshot() as view:
                yield owner._wrap(view)

        return snapshot

    def store(self) -> Any:
        return self._wrap(self._store)


async def test_a_reconciliation_during_a_sync_counts_every_row_once(
    make_engine: EngineFactory, admin: ActorContext, clock: FixedClock, usage: FakeUsageSource, tmp_path: Path
) -> None:  # review of #43: a row matched between two of reconcile's queries was counted nowhere
    file_store = await SqlStore.connect(f"sqlite+aiosqlite:///{tmp_path}/costs.db")  # a snapshot needs a file
    try:
        engine = await make_engine(supply=EngineSupply(store=file_store))
        actor = await engine.payments.provision(
            admin,
            ProvisionRequest(application_id="default", actor_id="alice", amount=Money(amount=Decimal(1), currency="USD"),
                             grant_id="grant_1"),
        )  # fmt: skip
        job, attempt = await running_job(file_store, clock, allocation=actor.allocation_id)
        usage.rows = [row("u1", "sess-1", "0.5", allocation=actor.allocation_id, manifest="m-1")]
        await engine.costs.sync_once()  # the row arrives before the session is recorded: unattributed

        async def session_recorded_and_synced() -> None:
            await ends_and_projects(engine, file_store, job, attempt, clock)

        engine.costs._store = SyncsAfterFirstRead(file_store, session_recorded_and_synced).store()  # type: ignore[assignment]
        report = await engine.costs.reconcile(admin, "default", "alice")
        counted = (
            sum((fees.fee_eth for fees in report.by_class.values()), Decimal(0)) + report.unattributed.fee_eth
        )
        assert counted == Decimal("0.5")  # in a class or unattributed, never in neither
    finally:
        await file_store.close()


async def ends_and_projects(
    engine: BuilderEngine, store: SqlStore, job: Job, attempt: Attempt, clock: FixedClock
) -> None:
    clock.advance(5)
    paid = attempt.model_copy(
        update={"sessions": (SignedSession(manifest_id="m-1"),), "payment_sent": True,
                "outcome": "succeeded", "status_code": 200, "ended_at": clock.now()}
    )  # fmt: skip
    await store.finish_attempt(paid)
    await store.finish_job(job.id, "succeeded", None, None, clock.now())
    await engine.costs.sync_once()  # matches the waiting row to the session and projects the job


async def test_a_running_jobs_matched_fee_is_pending_and_reconciliation_balances(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, transport: ScriptedTransport,
    usage: FakeUsageSource, manager: FakeAllocationManager,
) -> None:  # fmt: skip
    engine = await make_engine()
    actor = await engine.payments.provision(
        admin,
        ProvisionRequest(application_id="default", actor_id="alice", amount=Money(amount=Decimal(1), currency="USD"),
                         grant_id="grant_1"),
    )  # fmt: skip
    transport.opens = [StreamScript(events=[b"a", 30.0], manifest_ids=("m-s",))]  # paid at open, then runs on
    streaming = engine.jobs.streaming
    assert streaming is not None
    stream = await streaming.stream(alice, JobRequest(app=APP))
    usage.rows = [row("u1", "sess-s", "0.25", usd="0.25", allocation=actor.allocation_id, manifest="m-s")]
    await engine.costs.sync_once()  # the fee arrives and matches while the job runs
    manager.allocations[actor.allocation_id].spent = Decimal("0.25")  # what Batteries reports spent
    window = (stream.job.created_at - timedelta(hours=1), stream.job.created_at + timedelta(hours=1))
    summary = await engine.costs.summary(alice, *window)
    assert summary.jobs_pending == 1 and summary.by_class["pending"].fee_eth == Decimal("0.25")
    report = await engine.costs.reconcile(admin, "default", "alice")
    assert report.by_class["pending"].fee_eth == Decimal("0.25") and report.unattributed.fee_eth == Decimal(
        "0"
    )
    assert report.balanced
    await stream.aclose()
