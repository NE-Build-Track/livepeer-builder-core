"""D42: payment evidence is written when the signer signs, before the paid request, and never shrinks.

The transports here report the way ``SdkRunnerTransport`` does from ``on_payment_session``: they call
``report_signed_session`` and only then wait for the runner, so each test can look at the store while
the paid request is in flight, or cancel it there.
"""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from decimal import Decimal
from uuid import uuid4

import pytest

from livepeer_builder import BuilderEngine
from livepeer_builder.contracts import ActorContext, Attempt, JobRequest, Money, ProvisionRequest, UsageRow
from livepeer_builder.errors import JobFailed, RunnerCallError
from livepeer_builder.protocols import report_signed_session
from livepeer_builder.services.evidence import SignedSessionRecorder
from livepeer_builder.store import SqlStore
from livepeer_builder.testing import (
    FakeAllocationManager,
    FakeUsageSource,
    FixedClock,
    ScriptedStream,
    ScriptedStreamTransport,
    ScriptedTransport,
    StreamScript,
    runner,
)
from tests.conftest import EngineFactory

APP = "example/echo"
TWO_RUNNERS = (runner(url="https://r1"), runner(url="https://r2"))


async def provisioned(
    make_engine: EngineFactory, admin: ActorContext, **settings: object
) -> tuple[BuilderEngine, str]:
    engine = await make_engine(default_currency="ETH", **settings)
    request = ProvisionRequest(
        application_id="default",
        actor_id="alice",
        amount=Money(amount=Decimal(1), currency="ETH"),
        grant_id="grant_1",
    )
    actor = await engine.payments.provision(admin, request)
    return engine, actor.allocation_id


def usage_row(row_id: str, manifest: str, eth: str, allocation: str) -> UsageRow:
    return UsageRow(
        id=row_id, event_id=f"ev-{row_id}", status="applied", manifest_id=manifest, allocation_id=allocation,
        payment_session_id=f"sess-{manifest}", request_id=None, pipeline="live", computed_fee_eth=Decimal(eth),
        computed_fee_usd=None, currency="usd", created_at=FixedClock().now(),
    )  # fmt: skip


def evidence(attempt: Attempt) -> tuple[bool, list[str]]:
    return attempt.payment_sent, [session.manifest_id for session in attempt.sessions]


class SignsThenWaits(ScriptedStreamTransport):
    """Fails the first ``unpaid`` runners before payment, then signs ``manifest`` and waits for ``release``.

    ``then`` is what the runner finally does: answer (``None``) or raise.
    """

    def __init__(self, manifest: str = "m-1", unpaid: int = 0, then: RunnerCallError | None = None) -> None:
        super().__init__()
        self.manifest, self.unpaid, self.then = manifest, unpaid, then
        self.entered, self.release = asyncio.Event(), asyncio.Event()

    async def _sign_and_wait(self, url: str, credential: str | None) -> None:
        self.calls.append((url, credential))
        if len(self.calls) <= self.unpaid:
            raise RunnerCallError("unreachable", "self-signed certificate", payment_sent=False)
        await report_signed_session(self.manifest)  # on_payment_session: signed, paid request not sent yet
        self.entered.set()
        await self.release.wait()
        if self.then is not None:
            raise self.then

    async def call(self, r, request, credential):  # type: ignore[no-untyped-def]
        await self._sign_and_wait(r.url, credential)
        return ScriptedTransport.ok(manifest_ids=(self.manifest,))

    async def open(self, r, request, credential):  # type: ignore[no-untyped-def]
        await self._sign_and_wait(r.url, credential)
        return ScriptedStream(StreamScript(events=[b"ok"], manifest_ids=(self.manifest,)))


@contextlib.asynccontextmanager
async def in_flight(
    engine: BuilderEngine, alice: ActorContext, transport: SignsThenWaits
) -> AsyncIterator[asyncio.Task]:  # type: ignore[type-arg]
    """Runs a job on ``transport`` and yields its task once the session is signed. Always stops it."""
    engine.jobs._transport = transport
    task = asyncio.create_task(engine.jobs.run(alice, JobRequest(app=APP)))
    try:
        await transport.entered.wait()
        yield task
    finally:
        transport.release.set()
        if not task.done():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError, JobFailed):
            await task


async def test_evidence_is_stored_before_the_runner_answers(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext
) -> None:
    engine, _ = await provisioned(make_engine, admin)
    transport = SignsThenWaits()
    async with in_flight(engine, alice, transport) as task:
        (job,) = await engine.jobs.list_jobs(alice)
        assert job.attempts[0].outcome == "in_flight"
        assert evidence(job.attempts[0]) == (True, ["m-1"])
        transport.release.set()
        finished = (await task).job
    stored = await engine.jobs.get(alice, finished.id)
    assert (stored.state, stored.attempts[0].outcome) == ("succeeded", "succeeded")
    assert evidence(stored.attempts[0]) == (True, ["m-1"])  # written twice, stored once


async def test_a_cancelled_paid_call_keeps_its_evidence_and_its_cost(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, usage: FakeUsageSource,
    manager: FakeAllocationManager,
) -> None:  # fmt: skip
    engine, allocation = await provisioned(make_engine, admin)
    async with in_flight(engine, alice, SignsThenWaits()) as task:
        task.cancel()  # the caller went away while the runner had the paid request
        with pytest.raises(asyncio.CancelledError):
            await task
    (job,) = await engine.jobs.list_jobs(alice)
    assert job.failure is not None
    assert (job.state, job.failure.payment_sent) == ("uncertain", True)
    assert evidence(job.attempts[0]) == (True, ["m-1"])

    usage.rows = [usage_row("u1", "m-1", "0.3", allocation)]
    await engine.costs.sync_once()
    cost = await engine.costs.for_job(alice, job.id)
    assert [(a.cost_class, a.fees.fee_eth) for a in cost.attempts] == [("uncertain", Decimal("0.3"))]
    manager.allocations[allocation].spent = Decimal("0.3")
    reconciled = await engine.costs.reconcile(admin, "default", "alice")
    assert (reconciled.unattributed.fee_eth, reconciled.balanced) == (0, True)  # attributed to the job


async def test_a_restart_during_the_paid_request_finds_the_evidence(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, store: SqlStore
) -> None:
    engine, _ = await provisioned(make_engine, admin)
    async with in_flight(engine, alice, SignsThenWaits()):
        assert await engine.jobs.recover_interrupted() == 1  # as a fresh process would on start
        (job,) = await store.jobs_in_state("uncertain")
        assert job.failure is not None
        assert job.failure.payment_sent
        assert evidence(job.attempts[0]) == (True, ["m-1"])


async def test_a_failed_evidence_write_does_not_stop_the_paid_call(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, store: SqlStore,
    transport: ScriptedStreamTransport, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:  # fmt: skip
    engine, _ = await provisioned(make_engine, admin)
    transport.steps = [ScriptedTransport.ok(manifest_ids=("m-1",))]  # reports m-1 before it answers
    finish_attempt = store.finish_attempt
    calls = 0

    async def fails_first(attempt):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("database unavailable")
        await finish_attempt(attempt)

    monkeypatch.setattr(store, "finish_attempt", fails_first)
    with caplog.at_level(logging.WARNING):
        result = await engine.jobs.run(alice, JobRequest(app=APP))
    assert any(getattr(r, "event", "") == "jobs.evidence_write_failed" for r in caplog.records)
    stored = await engine.jobs.get(alice, result.job.id)
    assert stored.state == "succeeded"
    assert evidence(stored.attempts[0]) == (True, ["m-1"])  # the write after the call kept it


async def test_reported_payment_is_never_undone_by_the_error(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext
) -> None:
    engine, _ = await provisioned(make_engine, admin, runners=TWO_RUNNERS)
    # A transport that reports a signature, then raises as if nothing was paid, must not fail over.
    transport = SignsThenWaits(then=RunnerCallError("unreachable", "connection reset", payment_sent=False))
    async with in_flight(engine, alice, transport) as task:
        transport.release.set()
        with pytest.raises(JobFailed) as failed:
            await task
    job = failed.value.job
    assert len(transport.calls) == 1  # the second runner is never tried
    assert job.failure is not None
    assert (job.state, job.failure.payment_sent) == ("uncertain", True)
    assert evidence(job.attempts[0]) == (True, ["m-1"])


async def test_a_stream_stores_its_evidence_before_the_runner_answers(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext
) -> None:
    engine, _ = await provisioned(make_engine, admin)
    transport = SignsThenWaits(manifest="m-s")
    engine.jobs._transport = transport
    task = asyncio.create_task(engine.jobs.stream(alice, JobRequest(app=APP)))
    try:
        await transport.entered.wait()  # signed; the runner has not sent its headers
        (job,) = await engine.jobs.list_jobs(alice)
        assert evidence(job.attempts[0]) == (True, ["m-s"])
        transport.release.set()
        stream = await task
        assert b"".join([chunk async for chunk in stream]) == b"ok"
    finally:
        transport.release.set()
        if not task.done():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert evidence(stream.job.attempts[0]) == (True, ["m-s"])


async def test_cost_matches_while_the_paid_request_runs_and_reads_pending_until_it_ends(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, usage: FakeUsageSource
) -> None:
    """#33: the first runner fails before payment, the second is paid and slow."""
    engine, allocation = await provisioned(make_engine, admin, runners=TWO_RUNNERS)
    transport = SignsThenWaits(manifest="m-late", unpaid=1)
    async with in_flight(engine, alice, transport) as task:
        (job,) = await engine.jobs.list_jobs(alice)
        usage.rows = [usage_row("u1", "m-late", "0.3", allocation)]
        assert (await engine.costs.sync_once()).jobs_updated == 0  # matched, but the job has not ended
        live = await engine.costs.for_job(alice, job.id)
        assert (live.status, live.fees.fee_eth) == ("pending", Decimal("0.3"))
        transport.release.set()
        assert (await task).job.state == "succeeded"
    # Ended, and no sync since: the read computes it from the rows already matched, and stores it.
    observed = await engine.costs.for_job(alice, job.id)
    assert await engine.store.get_projection(job.id) == observed
    assert (observed.status, observed.fees.fee_eth) == ("observed", Decimal("0.3"))
    assert [(a.attempt_number, a.cost_class) for a in observed.attempts] == [(2, "delivered")]


async def test_a_late_report_lands_on_its_own_attempt() -> None:
    written: list[Attempt] = []

    class Store:
        async def finish_attempt(self, attempt: Attempt) -> None:
            written.append(attempt)

    def attempt(number: int) -> Attempt:
        return Attempt(
            job_id=uuid4(), number=number, runner_url=None, orchestrator_url=None, allocation_id=None, sessions=(),
            payment_sent=False, outcome="in_flight", status_code=None, started_at=FixedClock().now(), ended_at=None,
        )  # fmt: skip

    attempts = [attempt(1)]
    recorder = SignedSessionRecorder(Store(), attempts)  # type: ignore[arg-type]
    second = attempt(2)
    attempts.append(second)  # the engine moved on to the next runner before the report arrived
    await recorder("m-late")
    assert evidence(attempts[0]) == (True, ["m-late"])
    assert attempts[1] == second  # the next attempt is untouched
    assert [a.number for a in written] == [1]
