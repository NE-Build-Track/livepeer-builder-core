"""Payment evidence is stored when the signer signs, before the paid request goes out (ISS-16, D42).

A transport calls ``on_signed(manifest_id)`` for each session the signer signs. The engine writes it on
the attempt there and then, so a cancel, a disconnect or a crash during the paid request still leaves
the charge attributed to its job instead of ``unattributed``.
"""

import asyncio
from decimal import Decimal

import pytest

from livepeer_builder.contracts import (
    ActorContext,
    Attempt,
    JobRequest,
    Money,
    ProvisionRequest,
    Runner,
    RunnerReply,
)
from livepeer_builder.errors import JobFailed, RunnerCallError
from livepeer_builder.protocols import OnSigned
from livepeer_builder.store import SqlStore
from tests.conftest import EngineFactory

APP = "example/echo"


class SignsThenWaits:
    """Signs one session, then holds the paid request open until released (or cancelled)."""

    def __init__(self, store: SqlStore, then: RunnerReply | RunnerCallError | None = None) -> None:
        self.store = store
        self.then = then
        self.signed = asyncio.Event()
        self.release = asyncio.Event()

    async def call(
        self, runner: Runner, request: JobRequest, credential: str | None, on_signed: OnSigned | None = None
    ) -> RunnerReply:
        assert on_signed is not None, "the engine passes a recorder for every paid call"
        await on_signed("m-1")
        self.signed.set()
        await self.release.wait()  # the paid request is in flight
        if isinstance(self.then, RunnerCallError):
            raise self.then
        assert self.then is not None
        return self.then


async def signed(transport: "SignsThenWaits", task: "asyncio.Task[object]", within_s: float = 5.0) -> None:
    """Wait until the transport has signed, failing (not hanging) if the call ended without signing."""
    async with asyncio.timeout(within_s):
        while not transport.signed.is_set():
            if task.done():
                task.result()  # raises the call's error
                pytest.fail("the call finished without signing")
            await asyncio.sleep(0.01)  # waits on two things: the event and the task


async def provisioned(make_engine: EngineFactory, admin: ActorContext, transport: object):  # type: ignore[no-untyped-def]
    from livepeer_builder.runtime.assembly import EngineSupply

    engine = await make_engine(supply=EngineSupply(transport=transport))  # type: ignore[arg-type]
    amount = Money(amount=Decimal(1), currency="USD")
    request = ProvisionRequest(application_id="default", actor_id="alice", amount=amount, grant_id="grant_1")
    await engine.payments.provision(admin, request)
    return engine


async def test_evidence_is_stored_before_the_paid_request_answers(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, store: SqlStore
) -> None:
    transport = SignsThenWaits(store, then=RunnerReply(
        status_code=200, content=b"{}", content_type="application/json", data=None, manifest_ids=("m-1",),
        payment_sent=True,
    ))  # fmt: skip
    engine = await provisioned(make_engine, admin, transport)
    task = asyncio.create_task(engine.jobs.run(alice, JobRequest(app=APP)))
    await signed(transport, task)
    [job] = await store.jobs_in_state("running")  # what a crash right now would leave
    attempt = job.attempts[-1]
    assert attempt.outcome == "in_flight" and attempt.payment_sent
    assert [session.manifest_id for session in attempt.sessions] == ["m-1"]
    transport.release.set()
    result = await task
    assert [session.manifest_id for session in result.job.attempts[-1].sessions] == ["m-1"]  # recorded once


async def test_a_cancelled_paid_call_keeps_its_evidence(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, store: SqlStore
) -> None:
    transport = SignsThenWaits(store)
    engine = await provisioned(make_engine, admin, transport)
    task = asyncio.create_task(engine.jobs.run(alice, JobRequest(app=APP)))
    await signed(transport, task)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    [job] = await store.jobs_in_state("uncertain")  # paid, outcome unknown
    assert job.attempts[-1].payment_sent
    assert [session.manifest_id for session in job.attempts[-1].sessions] == ["m-1"]


async def test_an_error_after_signing_never_turns_the_attempt_unpaid(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, store: SqlStore
) -> None:
    # A transport that reports payment_sent=False after the signer signed must not drop the
    # signature. Paid unreachable stays uncertain and does not fail over (D52).
    error = RunnerCallError("unreachable", "connection reset", payment_sent=False)
    transport = SignsThenWaits(store, then=error)
    transport.release.set()
    engine = await provisioned(make_engine, admin, transport)
    with pytest.raises(JobFailed) as failed:
        await engine.jobs.run(alice, JobRequest(app=APP))
    job = failed.value.job
    assert len(job.attempts) == 1 and job.attempts[0].payment_sent  # paid unreachable does not fail over (D52)
    assert [session.manifest_id for session in job.attempts[0].sessions] == ["m-1"]
    assert job.state == "uncertain"  # paid and unreachable: the runner may have run


class OpensAfterSigning(SignsThenWaits):
    """A streaming transport whose open signs, then waits for the runner's headers."""

    async def open(
        self, runner: Runner, request: JobRequest, credential: str | None, on_signed: OnSigned | None = None
    ) -> RunnerReply:
        return await self.call(runner, request, credential, on_signed)


async def test_a_stream_cancelled_before_its_first_byte_keeps_its_evidence(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, store: SqlStore
) -> None:
    transport = OpensAfterSigning(store)
    engine = await provisioned(make_engine, admin, transport)
    streaming = engine.jobs.streaming
    assert streaming is not None
    task = asyncio.create_task(streaming.stream(alice, JobRequest(app=APP)))
    await signed(transport, task)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    [job] = await store.jobs_in_state("uncertain")
    assert job.attempts[-1].payment_sent
    assert [session.manifest_id for session in job.attempts[-1].sessions] == ["m-1"]


def fails_first_evidence_write(store: SqlStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """The write at signing fails (the database blipped); later writes work."""
    real = store.record_payment_evidence
    calls = 0

    async def flaky(attempt: Attempt) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ConnectionError("database unavailable")
        await real(attempt)

    monkeypatch.setattr(store, "record_payment_evidence", flaky)


async def test_a_cancel_after_a_failed_evidence_write_still_stores_the_evidence(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, store: SqlStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip  # #44 review: the close path wrote only the job row, so the signature was lost
    fails_first_evidence_write(store, monkeypatch)
    transport = SignsThenWaits(store)
    engine = await provisioned(make_engine, admin, transport)
    task = asyncio.create_task(engine.jobs.run(alice, JobRequest(app=APP)))
    await signed(transport, task)
    task.cancel()  # the caller disconnects during the paid request
    with pytest.raises(asyncio.CancelledError):
        await task
    [job] = await store.jobs_in_state("uncertain")
    assert job.attempts[-1].payment_sent
    assert [session.manifest_id for session in job.attempts[-1].sessions] == ["m-1"]


async def test_a_stream_cancelled_after_a_failed_evidence_write_still_stores_the_evidence(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, store: SqlStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    fails_first_evidence_write(store, monkeypatch)
    transport = OpensAfterSigning(store)
    engine = await provisioned(make_engine, admin, transport)
    streaming = engine.jobs.streaming
    assert streaming is not None
    task = asyncio.create_task(streaming.stream(alice, JobRequest(app=APP)))
    await signed(transport, task)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    [job] = await store.jobs_in_state("uncertain")
    assert [session.manifest_id for session in job.attempts[-1].sessions] == ["m-1"]


async def test_a_store_that_cannot_take_evidence_still_closes_the_job(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, store: SqlStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    async def down(attempt: Attempt) -> None:
        raise ConnectionError("database unavailable")

    monkeypatch.setattr(store, "record_payment_evidence", down)
    transport = SignsThenWaits(store)
    engine = await provisioned(make_engine, admin, transport)
    task = asyncio.create_task(engine.jobs.run(alice, JobRequest(app=APP)))
    await signed(transport, task)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    [job] = await store.jobs_in_state("uncertain")  # closed all the same: maybe paid, outcome unknown
    assert job.failure is not None and job.failure.payment_sent
