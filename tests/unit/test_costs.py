"""ISS-02 / ISS-04: cost sync, classification, projections (corrected), summary and reconciliation."""

import asyncio
import contextlib
import json
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from livepeer_gateway import http as gateway_http
from livepeer_gateway import live_runner
from livepeer_gateway.errors import LivepeerHTTPError

from livepeer_builder.adapters.sdk import SdkRunnerTransport
from livepeer_builder.contracts import (
    ActorContext,
    Attempt,
    AttemptOutcome,
    Fees,
    Job,
    JobCost,
    JobRequest,
    Money,
    NetworkRate,
    ProvisionRequest,
    SignedSession,
    SyncCheckpoint,
    UsageRow,
)
from livepeer_builder.errors import JobFailed, RunnerCallError
from livepeer_builder.services.costs import classify
from livepeer_builder.store import SqlStore
from livepeer_builder.testing import (
    FakeAllocationManager,
    FakeUsageSource,
    FixedClock,
    ScriptedStreamTransport,
    ScriptedTransport,
    StreamScript,
    runner,
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


def test_a_running_job_is_pending_until_it_ends() -> None:
    attempts = (("unreachable", (), False), ("in_flight", (), False))  # the second has not paid yet
    running = classify(job_with(*attempts, state="running"), [])
    assert (running.status, running.attempts) == ("pending", ())  # it may still pay: not a known zero
    assert classify(job_with(*attempts[:1], state="failed"), []).status == "none"
    # Restart recovery leaves the in-flight attempt unpaid on an uncertain job. Payment may have gone out.
    interrupted = classify(job_with(("in_flight", (), False), state="uncertain"), [])
    assert (interrupted.status, interrupted.attempts) == ("pending", ())
    assert classify(job_with(state="queued"), []).status == "pending"  # reserved for async; not ended
    # Rows can match while a stream still runs (and a metered one keeps paying): no total is final yet.
    streaming = classify(job_with(("in_flight", ("s",), True), state="running"), [row("1", "s", "1")])
    assert (streaming.status, streaming.fees.fee_eth) == ("pending", Decimal(1))


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


async def test_a_signed_call_that_fails_on_the_orchestrator_is_failed_after_payment(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, usage: FakeUsageSource,
    manager: FakeAllocationManager, clock: FixedClock, monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    """The real SDK signs, then the runner fails: the charge is never none, and lands as failed_after_payment.

    Only the signer and runner HTTP calls are faked, so ``call_runner``, ``on_payment_session`` and the
    adapter's collector run as in production (livepeer-python-gateway#81, issue #33).
    """
    fixed = NetworkRate(amount=Decimal("0.10"), currency="usd", unit="fixed")  # a unit the SDK can pay
    engine = await make_engine(
        default_currency="ETH",
        runners=tuple(runner(url=f"https://r{n}").model_copy(update={"rate": fixed}) for n in (1, 2, 3)),
    )
    actor = await engine.payments.provision(
        admin,
        ProvisionRequest(
            application_id="default",
            actor_id="alice",
            amount=Money(amount=Decimal(1), currency="ETH"),
            grant_id="grant_1",
        ),
    )
    signer_calls: list[dict[str, object]] = []
    runner_calls: list[tuple[str, bool]] = []
    in_flight, release = asyncio.Event(), asyncio.Event()

    async def signer_info(url: str, headers: object) -> SimpleNamespace:
        return SimpleNamespace(address="0xpayer")

    async def sign(url: str, payload: dict[str, object], **kwargs: object) -> dict[str, object]:
        signer_calls.append(payload)
        return {"payment": "ticket", "segCreds": "creds", "state": {}}

    async def runner_http(url: str, *, headers: dict[str, str] | None = None, **kwargs: object) -> object:
        paid = "Livepeer-Payment" in (headers or {})
        runner_calls.append((url, paid))
        if url.startswith("https://r1"):
            raise LivepeerHTTPError(503, url, body="busy")  # refused before payment: fail over
        if not paid:
            challenge = {
                "payment_params": "params",
                "manifest_id": "m-500",
                "payment_url": "https://orch.example/apps/r2/session/m-500/payment",
            }
            raise LivepeerHTTPError(402, url, json.dumps(challenge))
        in_flight.set()  # the signer has charged; the paid request has not answered
        await release.wait()
        raise LivepeerHTTPError(500, url, body="runner crashed")

    monkeypatch.setattr(live_runner, "get_signer_info", signer_info)
    monkeypatch.setattr(gateway_http, "post_json", sign)
    monkeypatch.setattr(live_runner, "_request_body", runner_http)
    engine.jobs._transport = SdkRunnerTransport("https://signer.example")

    task = asyncio.create_task(engine.jobs.run(alice, JobRequest(app=APP)))
    paid = asyncio.create_task(in_flight.wait())
    try:
        await asyncio.wait({task, paid}, return_when=asyncio.FIRST_COMPLETED)
        assert paid.done(), task.exception()  # the job ended before the paid request started
        running = (await engine.jobs.list_jobs(alice))[0]
        assert running.state == "running"
        await engine.costs.sync_once()  # issue #33: this sync used to store none for the paid attempt
        assert (await engine.costs.for_job(alice, running.id)).status == "pending"

        release.set()
        with pytest.raises(JobFailed) as failed:
            await task
    finally:
        release.set()
        paid.cancel()
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, JobFailed):
                await task

    job = failed.value.job
    assert job.state == "failed"
    assert job.failure is not None and job.failure.kind == "runner_error"
    assert [signed["ManifestID"] for signed in signer_calls] == ["m-500"]
    assert [url.split("/")[2] for url, _ in runner_calls] == ["r1", "r2", "r2"]  # never r3 once paid
    assert [
        (a.number, a.outcome, a.status_code, a.payment_sent, tuple(s.manifest_id for s in a.sessions))
        for a in job.attempts
    ] == [(1, "refused", 503, False, ()), (2, "http", 500, True, ("m-500",))]

    # Paid and ended, but Batteries has not reported the charge: pending, never none.
    await engine.costs.sync_once()
    waiting = await engine.costs.for_job(alice, job.id)
    assert waiting.status == "pending"
    assert [(a.attempt_number, a.cost_class) for a in waiting.attempts] == [(2, "pending")]

    usage.rows = [row("u1", "sess-500", "0.3", allocation=actor.allocation_id, manifest="m-500")]
    await engine.costs.sync_once()
    cost = await engine.costs.for_job(alice, job.id)
    assert (cost.status, cost.fees.fee_eth) == ("observed", Decimal("0.3"))
    assert [(a.attempt_number, a.cost_class, a.fees.fee_eth) for a in cost.attempts] == [
        (2, "failed_after_payment", Decimal("0.3"))
    ]

    manager.allocations[actor.allocation_id].spent = Decimal("0.3")
    result = await engine.costs.reconcile(admin, "default", "alice")
    assert result.by_class["failed_after_payment"].fee_eth == Decimal("0.3")
    assert result.by_class["delivered"].fee_eth == Decimal(0)
    assert result.unattributed.fee_eth == Decimal(0)
    assert result.balanced is True

    summary = await engine.costs.summary(
        alice, clock.now() - timedelta(days=1), clock.now() + timedelta(days=1)
    )
    assert summary.by_class["failed_after_payment"].fee_eth == Decimal("0.3")
    assert (summary.jobs_with_cost, summary.jobs_pending) == (1, 0)


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


async def test_a_job_synced_while_running_never_reads_none(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, usage: FakeUsageSource
) -> None:
    engine = await make_engine(
        default_currency="ETH", runners=(runner(url="https://r1"), runner(url="https://r2"))
    )
    actor = await engine.payments.provision(
        admin,
        ProvisionRequest(
            application_id="default",
            actor_id="alice",
            amount=Money(amount=Decimal(1), currency="ETH"),
            grant_id="grant_1",
        ),
    )
    release, entered = asyncio.Event(), asyncio.Event()

    class PaysLate(ScriptedTransport):
        async def call(self, r, request, credential):  # type: ignore[no-untyped-def]
            if not self.calls:  # the first runner fails before payment and the job fails over
                self.calls.append((r.url, credential))
                raise RunnerCallError("unreachable", "self-signed certificate", payment_sent=False)
            entered.set()  # the second attempt is in flight and has not paid yet
            await release.wait()  # the test sets release in a finally, so a failed assertion unblocks this
            return ScriptedTransport.ok(manifest_ids=("m-late",))

    engine.jobs._transport = PaysLate()
    task = asyncio.create_task(engine.jobs.run(alice, JobRequest(app=APP)))
    try:
        await entered.wait()
        job = (await engine.jobs.list_jobs(alice))[0]
        assert job.state == "running"
        assert job.attempts[0].outcome == "unreachable"
        assert (await engine.costs.for_job(alice, job.id)).status == "pending"  # no projection yet
        assert (await engine.costs.sync_once()).jobs_updated == 0  # a running job is not projected
        assert await engine.store.get_projection(job.id) is None
        assert (await engine.costs.for_job(alice, job.id)).status == "pending"

        release.set()
        assert (await task).job.state == "succeeded"
        assert (await engine.costs.for_job(alice, job.id)).status == "pending"
        await engine.costs.sync_once()  # paid, no usage row yet
        waiting = await engine.costs.for_job(alice, job.id)
        assert waiting.status == "pending"
        assert [attempt.cost_class for attempt in waiting.attempts] == ["pending"]

        usage.rows = [row("u1", "sess-late", "0.3", allocation=actor.allocation_id, manifest="m-late")]
        await engine.costs.sync_once()
        observed = await engine.costs.for_job(alice, job.id)
        assert observed.status == "observed"
        assert observed.fees.fee_eth == Decimal("0.3")
        assert [(attempt.attempt_number, attempt.cost_class) for attempt in observed.attempts] == [
            (2, "delivered")
        ]
    finally:
        release.set()
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, JobFailed):
                await task


async def test_a_job_that_fails_unpaid_after_a_running_sync_reads_none(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext
) -> None:
    engine = await make_engine(
        default_currency="ETH", runners=(runner(url="https://r1"), runner(url="https://r2"))
    )
    await engine.payments.provision(
        admin,
        ProvisionRequest(
            application_id="default",
            actor_id="alice",
            amount=Money(amount=Decimal(1), currency="ETH"),
            grant_id="grant_1",
        ),
    )
    release, entered = asyncio.Event(), asyncio.Event()

    class FailsUnpaid(ScriptedTransport):
        async def call(self, r, request, credential):  # type: ignore[no-untyped-def]
            self.calls.append((r.url, credential))
            if len(self.calls) == 1:  # fail over before payment; the next attempt is the one still in flight
                raise RunnerCallError("unreachable", "self-signed certificate", payment_sent=False)
            entered.set()
            await release.wait()
            raise RunnerCallError("unreachable", "connection dropped", payment_sent=False)

    engine.jobs._transport = FailsUnpaid()
    task = asyncio.create_task(engine.jobs.run(alice, JobRequest(app=APP)))
    try:
        await entered.wait()
        job = (await engine.jobs.list_jobs(alice))[0]
        assert job.state == "running"
        await engine.costs.sync_once()  # would previously store pending and keep serving it after the failure
        assert await engine.store.get_projection(job.id) is None
        assert (await engine.costs.for_job(alice, job.id)).status == "pending"

        release.set()
        with pytest.raises(JobFailed):
            await task
        assert (await engine.costs.for_job(alice, job.id)).status == "none"  # no second sync required
    finally:
        release.set()
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, JobFailed):
                await task


async def test_a_stored_none_is_ignored_while_the_job_runs(
    make_engine: EngineFactory, alice: ActorContext, clock: FixedClock
) -> None:
    engine = await make_engine()
    job = job_with(state="running").model_copy(update={"completed_at": None})
    await engine.store.create_job(job, None)
    # Before the #33 fix, a sync could store none for a running job whose paid attempt had not reported back.
    stale = JobCost(
        job_id=job.id, status="none", fees=Fees(), attempts=(), first_observed_at=None, updated_at=clock.now()
    )
    await engine.store.save_projection(stale)
    assert (await engine.costs.for_job(alice, job.id)).status == "pending"

    attempt = Attempt(
        job_id=job.id, number=1, runner_url="https://r1", orchestrator_url=None, allocation_id="alloc_1", sessions=(),
        payment_sent=False, outcome="in_flight", status_code=None, started_at=clock.now(), ended_at=None,
    )  # fmt: skip
    await engine.store.start_attempt(attempt)
    paid = attempt.model_copy(
        update={
            "sessions": (SignedSession(manifest_id="m-x"),),
            "payment_sent": True,
            "outcome": AttemptOutcome.SUCCEEDED,
        }
    )
    await engine.store.finish_attempt(paid)
    clock.advance(1)
    await engine.store.finish_job(job.id, "succeeded", None, None, clock.now())
    assert (await engine.costs.for_job(alice, job.id)).status == "pending"  # the stale none is not served
    assert (await engine.costs.sync_once()).jobs_updated == 1  # still pending: the worker keeps it
    assert (await engine.costs.for_job(alice, job.id)).status == "pending"  # paid, no usage row yet


async def test_an_uncertain_job_without_evidence_is_stored_pending(
    make_engine: EngineFactory, alice: ActorContext, clock: FixedClock
) -> None:
    engine = await make_engine()
    job = job_with(state="running").model_copy(update={"completed_at": None})
    await engine.store.create_job(job, None)
    await engine.store.start_attempt(
        Attempt(
            job_id=job.id, number=1, runner_url="https://r1", orchestrator_url=None, allocation_id="alloc_1",
            sessions=(), payment_sent=False, outcome="in_flight", status_code=None, started_at=clock.now(),
            ended_at=None,
        )
    )  # fmt: skip
    # The engine stopped mid-call, so payment may have gone out without the evidence being written.
    assert await engine.jobs.recover_interrupted() == 1
    assert (await engine.costs.sync_once()).jobs_updated == 1
    stored = await engine.store.get_projection(job.id)
    assert stored is not None
    assert (stored.status, stored.attempts) == ("pending", ())
    assert [cost.job_id for cost in await engine.costs.recent(alice)] == [job.id]
    window = (clock.now() - timedelta(hours=1), clock.now() + timedelta(hours=1))
    summary = await engine.costs.summary(alice, *window)
    assert (summary.jobs_pending, summary.jobs_with_cost) == (1, 0)


async def test_a_stream_with_matched_rows_stays_pending_until_it_ends(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, transport: ScriptedStreamTransport,
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
    transport.opens = [StreamScript(events=[b"a", b"b"], manifest_ids=("m-live",))]
    stream = await engine.jobs.stream(alice, JobRequest(app=APP))
    job_id = stream.job.id
    usage.rows = [row("u1", "sess-live", "0.2", allocation=actor.allocation_id, manifest="m-live")]
    assert (await engine.costs.sync_once()).jobs_updated == 0  # the session matches; the job is still running
    live = await engine.costs.for_job(alice, job_id)
    assert (live.status, live.fees.fee_eth) == ("pending", Decimal("0.2"))  # so far, not final
    assert await engine.store.get_projection(job_id) is None

    assert b"".join([chunk async for chunk in stream]) == b"ab"
    await engine.costs.sync_once()
    ended = await engine.costs.for_job(alice, job_id)
    assert (ended.status, ended.fees.fee_eth) == ("observed", Decimal("0.2"))
    assert [attempt.cost_class for attempt in ended.attempts] == ["delivered"]


async def test_a_running_stream_counts_in_reconciliation_and_summary(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, transport: ScriptedStreamTransport,
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
    transport.opens = [StreamScript(events=[b"a"], manifest_ids=("m-live",))]
    stream = await engine.jobs.stream(alice, JobRequest(app=APP))
    usage.rows = [row("u1", "sess-live", "0.2", allocation=actor.allocation_id, manifest="m-live")]
    await engine.costs.sync_once()  # matched, so not unattributed; no projection while it runs
    manager.allocations[actor.allocation_id].spent = Decimal("0.2")

    # An earlier release stored projections for running jobs: that row must not count a second time.
    live = await engine.costs.for_job(alice, stream.job.id)
    await engine.store.save_projection(live.model_copy(update={"updated_at": FixedClock().now()}))
    result = await engine.costs.reconcile(admin, "default", "alice")
    assert result.by_class["pending"].fee_eth == Decimal("0.2")
    assert result.unattributed.fee_eth == 0 and result.balanced is True
    window = (FixedClock().now() - timedelta(days=1), FixedClock().now() + timedelta(days=1))
    summary = await engine.costs.summary(alice, *window)
    assert (summary.jobs_pending, summary.by_class["pending"].fee_eth) == (1, Decimal("0.2"))
    await stream.aclose()


async def test_a_paid_attempt_with_no_row_stays_pending(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, transport: ScriptedTransport,
    clock: FixedClock,
) -> None:  # fmt: skip
    engine = await make_engine(default_currency="ETH")
    await engine.payments.provision(
        admin,
        ProvisionRequest(
            application_id="default",
            actor_id="alice",
            amount=Money(amount=Decimal(1), currency="ETH"),
            grant_id="grant_1",
        ),
    )
    transport.steps = [
        RunnerCallError("payment", "tickets rejected", payment_sent=True, manifest_ids=("m-x",))
    ]
    with pytest.raises(JobFailed) as failed:
        await engine.jobs.run(alice, JobRequest(app=APP))
    job_id = failed.value.job.id
    clock.advance(86400)
    await engine.costs.sync_once()
    stored = await engine.store.get_projection(job_id)  # what sync wrote, before any read recomputes it
    assert stored is not None and stored.status == "pending"  # missing evidence is never zero
    assert [a.cost_class for a in stored.attempts] == ["pending"]


async def test_a_stream_that_just_ended_stays_in_reconciliation_before_the_next_sync(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, transport: ScriptedStreamTransport,
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
    transport.opens = [StreamScript(events=[b"a"], manifest_ids=("m-live",))]
    stream = await engine.jobs.stream(alice, JobRequest(app=APP))
    usage.rows = [row("u1", "sess-live", "0.2", allocation=actor.allocation_id, manifest="m-live")]
    await engine.costs.sync_once()  # matched while it runs
    manager.allocations[actor.allocation_id].spent = Decimal("0.2")
    assert b"".join([chunk async for chunk in stream]) == b"a"  # ended; no sync since

    result = await engine.costs.reconcile(admin, "default", "alice")
    assert result.by_class["delivered"].fee_eth == Decimal("0.2") and result.balanced is True
    window = (FixedClock().now() - timedelta(days=1), FixedClock().now() + timedelta(days=1))
    summary = await engine.costs.summary(alice, *window)
    assert (summary.jobs_with_cost, summary.by_class["delivered"].fee_eth) == (1, Decimal("0.2"))


async def test_a_job_stored_while_a_rollup_reads_wins_over_its_running_snapshot(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, transport: ScriptedStreamTransport,
    usage: FakeUsageSource, manager: FakeAllocationManager, store: SqlStore, monkeypatch: pytest.MonkeyPatch,
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
    transport.opens = [StreamScript(events=[b"a"], manifest_ids=("m-live",))]
    stream = await engine.jobs.stream(alice, JobRequest(app=APP))
    usage.rows = [row("u1", "sess-live", "0.2", allocation=allocation, manifest="m-live")]
    await engine.costs.sync_once()
    manager.allocations[allocation].spent = Decimal("0.3")
    due_jobs = store.jobs_for_projection
    overlapped = False

    async def end_and_sync_in_between(job_ids, application_id=None):  # type: ignore[no-untyped-def]
        # After reconcile read the running snapshot (0.2 pending): the stream ends, one more row
        # arrives, and cost sync stores the ended projection (0.3 delivered).
        nonlocal overlapped
        if not overlapped:
            overlapped = True
            assert b"".join([chunk async for chunk in stream]) == b"a"
            usage.rows.append(row("u2", "sess-live", "0.1", allocation=allocation, manifest="m-live"))
            await engine.costs.sync_once()
        return await due_jobs(job_ids, application_id)

    monkeypatch.setattr(store, "jobs_for_projection", end_and_sync_in_between)
    result = await engine.costs.reconcile(admin, "default", "alice")
    assert overlapped
    assert (result.by_class["delivered"].fee_eth, result.by_class["pending"].fee_eth) == (Decimal("0.3"), 0)
    assert result.balanced is True


async def test_a_projection_synced_during_a_rollup_is_not_replaced_by_an_older_compute(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, transport: ScriptedStreamTransport,
    usage: FakeUsageSource, manager: FakeAllocationManager, store: SqlStore, monkeypatch: pytest.MonkeyPatch,
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
    transport.opens = [StreamScript(events=[b"a"], manifest_ids=("m-live",))]
    stream = await engine.jobs.stream(alice, JobRequest(app=APP))
    usage.rows = [row("u1", "sess-live", "0.2", allocation=allocation, manifest="m-live")]
    await engine.costs.sync_once()
    assert b"".join([chunk async for chunk in stream]) == b"a"  # ended, not projected: due
    manager.allocations[allocation].spent = Decimal("0.3")
    stored_between = store.projections_between
    overlapped = False

    async def sync_then_read(*args):  # type: ignore[no-untyped-def]
        # After reconcile listed the job as due: one more row arrives and cost sync stores 0.3.
        nonlocal overlapped
        if not overlapped:
            overlapped = True
            usage.rows.append(row("u2", "sess-live", "0.1", allocation=allocation, manifest="m-live"))
            await engine.costs.sync_once()
        return await stored_between(*args)

    monkeypatch.setattr(store, "projections_between", sync_then_read)
    result = await engine.costs.reconcile(admin, "default", "alice")
    assert overlapped
    assert result.by_class["delivered"].fee_eth == Decimal("0.3") and result.balanced is True


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
