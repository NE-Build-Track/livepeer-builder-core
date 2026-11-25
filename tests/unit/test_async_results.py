"""ISS-26: what a finished async job leaves, and how its owner reads it.

A succeeded async job's bytes are stored in the transaction that ends it and read back with
``jobs.result``. A job that has not ended is not a result yet; a failed one answers as ``run`` would
have; an immediate job's result was never kept, and a stored one is kept for a set time
(docs/contracts/engine.md#results).
"""

import asyncio
import hashlib
from datetime import timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest

from livepeer_builder import BuilderEngine, EngineSupply
from livepeer_builder.contracts import ActorContext, Failure, JobRequest, Operation, ResourceRef, ResultRef
from livepeer_builder.errors import (
    InvalidRequest,
    JobFailed,
    JobNotFinished,
    NotFound,
    ResultExpired,
    RunnerCallError,
)
from livepeer_builder.store import SqlStore
from livepeer_builder.testing import FakeUsageSource, FixedClock, ScriptedTransport
from tests.conftest import EngineFactory, allot
from tests.unit.test_async_jobs import APP, calls, queued
from tests.unit.test_costs import row

PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256))


def image(content: bytes = PNG) -> Any:
    return ScriptedTransport.ok(content).model_copy(update={"content_type": "image/png; q=1"})


async def finished(engine: BuilderEngine, actor: ActorContext, **fields: object) -> UUID:
    """Submit one async job and step the worker until it has ended."""
    job = await engine.jobs.submit(actor, queued(**fields))
    assert await engine.jobs.worker.run_once() == 1
    return job.id


async def test_a_stored_result_is_read_back_with_its_bytes_and_its_type(
    make_engine: EngineFactory, alice: ActorContext, transport: ScriptedTransport
) -> None:
    engine = await make_engine()
    await allot(engine, alice)
    transport.steps = [image(), ScriptedTransport.ok(b'{"answer": 42}')]

    result = await engine.jobs.result(alice, await finished(engine, alice))
    assert (result.content, result.content_type, result.status_code) == (PNG, "image/png; q=1", 200)
    assert result.data is None  # not JSON
    assert result.job.state == "succeeded" and result.job.result is not None
    assert result.job.result.sha256 == hashlib.sha256(result.content).hexdigest()
    assert result.job.result.size_bytes == len(PNG)
    assert (await engine.jobs.result(alice, result.job.id)).content == PNG  # as often as it is asked for

    parsed = await engine.jobs.result(alice, await finished(engine, alice))
    assert (parsed.content, parsed.data) == (b'{"answer": 42}', {"answer": 42})  # as run answers it


async def test_a_stored_result_keeps_only_a_content_type_a_header_can_hold(
    make_engine: EngineFactory, alice: ActorContext, transport: ScriptedTransport
) -> None:
    """The one rule for a runner's content type (#158) holds for a stored result as for an immediate
    one: a type no response header can hold is ``application/octet-stream``, on the job's record and
    when the result is read back, where it becomes the answer's own header."""
    engine = await make_engine()
    await allot(engine, alice)
    transport.steps = [ScriptedTransport.ok(PNG).model_copy(update={"content_type": "text/plain\r\nx: y"})]

    result = await engine.jobs.result(alice, await finished(engine, alice))
    assert result.job.result is not None
    assert (result.content_type, result.job.result.content_type) == ("application/octet-stream",) * 2
    assert result.content == PNG


@pytest.mark.parametrize("kind", ["auditor", "tenant_admin", "clearinghouse_admin"])
async def test_a_results_bytes_go_to_the_jobs_owner_and_to_nobody_who_may_read_its_record(
    kind: str, make_engine: EngineFactory, alice: ActorContext, transport: ScriptedTransport
) -> None:
    """An auditor, an admin of the job's tenant and a clearinghouse admin each read another actor's
    job as a record. None of them is its owner: its result is the 404 of a job that is not theirs,
    whatever state it is in, and never its bytes (decided 25 November 2026)."""
    engine = await make_engine()
    await allot(engine, alice)
    reader = ActorContext.model_validate({"actor_id": "desk", "credential_kind": kind})  # alice's tenant
    transport.steps = [image(), RunnerCallError("http", "boom", payment_sent=True, status_code=500)]
    stored, failed = await finished(engine, alice), await finished(engine, alice)
    waiting = (await engine.jobs.submit(alice, queued())).id

    for job_id in (stored, failed, waiting):
        assert (await engine.jobs.get(reader, job_id)).id == job_id  # the record, as before
        with pytest.raises(NotFound, match=f"job {job_id} not found"):
            await engine.jobs.result(reader, job_id)
    assert (await engine.jobs.result(alice, stored)).content == PNG


class AllowsEverything:
    """A supplied ``AccessPolicy`` that refuses nothing."""

    def check(self, actor: ActorContext, operation: Operation, resource: ResourceRef | None = None) -> None:
        return None


async def test_no_supplied_policy_hands_a_results_bytes_to_another_actor(
    make_engine: EngineFactory, alice: ActorContext, bob: ActorContext, transport: ScriptedTransport
) -> None:
    """The contract promises the bytes to the job's owner without condition, so the rule is the
    engine's own and a policy can only narrow it. The owner is the actor of the job's tenant: the same
    actor id in another tenant is somebody else."""
    engine = await make_engine(supply=EngineSupply(policy=AllowsEverything()))
    await allot(engine, alice)
    transport.steps = [image()]
    job_id = await finished(engine, alice)
    namesake = alice.model_copy(update={"tenant_id": "another"})

    assert (await engine.jobs.get(bob, job_id)).id == job_id  # this policy lets anybody read the record
    for other in (bob, namesake):
        with pytest.raises(NotFound):
            await engine.jobs.result(other, job_id)
    assert (await engine.jobs.result(alice, job_id)).content == PNG


async def test_a_job_that_has_not_ended_has_no_result_yet(
    make_engine: EngineFactory, alice: ActorContext, transport: ScriptedTransport
) -> None:
    engine = await make_engine()
    await allot(engine, alice)
    job = await engine.jobs.submit(alice, queued())
    with pytest.raises(JobNotFinished) as waiting:
        await engine.jobs.result(alice, job.id)
    assert (waiting.value.job.id, waiting.value.job.state, waiting.value.retry_after_s) == (
        job.id,
        "queued",
        1,
    )

    transport.steps = [image()]
    transport.gate = asyncio.Event()
    step = asyncio.create_task(engine.jobs.worker.run_once())
    await calls(transport, 1)
    with pytest.raises(JobNotFinished) as running:
        await engine.jobs.result(alice, job.id)
    assert running.value.job.state == "running"
    transport.gate.set()
    await step
    assert (await engine.jobs.result(alice, job.id)).content == PNG


async def test_a_failed_job_answers_as_run_would_have(
    make_engine: EngineFactory, alice: ActorContext, transport: ScriptedTransport
) -> None:
    engine = await make_engine()
    await allot(engine, alice)
    transport.steps = [RunnerCallError("http", "boom", payment_sent=True, status_code=500, body="trace")]
    job_id = await finished(engine, alice)
    with pytest.raises(JobFailed) as failed:
        await engine.jobs.result(alice, job_id)
    job = failed.value.job
    assert job.failure is not None
    assert (job.state, job.failure.kind, job.failure.body, job.failure.payment_sent) == (
        "failed",
        "runner_error",
        "trace",
        True,
    )
    assert await engine.store.read_result(job_id, engine.clock.now()) is None

    transport.steps = [RunnerCallError("timeout", "slow", payment_sent=True, manifest_ids=("m-1",))]
    with pytest.raises(JobFailed) as unknown:
        await engine.jobs.result(alice, await finished(engine, alice))
    assert unknown.value.job.state == "uncertain"


async def test_an_immediate_jobs_result_was_never_kept(
    make_engine: EngineFactory, alice: ActorContext, transport: ScriptedTransport
) -> None:
    engine = await make_engine()
    await allot(engine, alice)
    transport.steps = [ScriptedTransport.ok()]
    ran = await engine.jobs.run(alice, JobRequest(app=APP))
    with pytest.raises(ResultExpired, match="immediate") as gone:
        await engine.jobs.result(alice, ran.job.id)
    assert gone.value.job.id == ran.job.id


async def test_a_stored_result_expires_and_its_job_stays(
    make_engine: EngineFactory, alice: ActorContext, transport: ScriptedTransport, clock: FixedClock
) -> None:
    engine = await make_engine(retention_results_days=2)
    await allot(engine, alice)
    transport.steps = [image()]
    job_id = await finished(engine, alice)
    kept = await engine.jobs.get(alice, job_id)
    assert kept.result is not None and kept.result.expires_at == clock.now() + timedelta(days=2)

    clock.advance(2 * 86400 - 1)
    assert (await engine.jobs.result(alice, job_id)).content == PNG
    clock.advance(1)
    with pytest.raises(ResultExpired, match="expired") as gone:
        await engine.jobs.result(alice, job_id)
    assert gone.value.job == kept  # the record and its ResultRef are still there
    assert await engine.jobs.get(alice, job_id) == kept


async def test_results_are_kept_as_long_as_their_job_when_no_time_is_set(
    make_engine: EngineFactory, alice: ActorContext, transport: ScriptedTransport, clock: FixedClock
) -> None:
    engine = await make_engine(retention_results_days=None)
    await allot(engine, alice)
    transport.steps = [image()]
    job_id = await finished(engine, alice)
    kept = await engine.jobs.get(alice, job_id)
    assert kept.result is not None and kept.result.expires_at is None
    clock.advance(400 * 86400)
    assert (await engine.jobs.result(alice, job_id)).content == PNG


# -- a reply the engine could not keep ---------------------------------------------------------------------


async def test_a_reply_too_large_to_store_fails_the_job_and_is_never_billed_as_delivered(
    make_engine: EngineFactory,
    alice: ActorContext,
    transport: ScriptedTransport,
    usage: FakeUsageSource,
    admin: ActorContext,
) -> None:
    """D69 (4): the runner answered and was paid, and the engine kept nothing. The attempt keeps its
    outcome, and the cost follows the job: ``failed_after_payment``, never ``delivered``."""
    engine = await make_engine(async_result_max_bytes=64)
    await allot(engine, alice)
    big = bytes(65)
    transport.steps = [image(big), image(bytes(64))]
    job_id = await finished(engine, alice)

    with pytest.raises(JobFailed) as failed:
        await engine.jobs.result(alice, job_id)
    job = failed.value.job
    assert job.failure is not None
    assert (job.state, job.failure.kind, job.failure.payment_sent) == ("failed", "result_not_stored", True)
    assert "65 bytes" in job.failure.message and "64" in job.failure.message
    assert [(a.outcome, a.payment_sent, a.status_code) for a in job.attempts] == [("succeeded", True, 200)]
    assert job.result == ResultRef(  # what is left of it: its shape, and nowhere to read it
        storage="stored",
        content_type="image/png; q=1",
        size_bytes=65,
        sha256=hashlib.sha256(big).hexdigest(),
    )
    assert await engine.store.read_result(job_id, engine.clock.now()) is None

    pending = await engine.costs.for_job(alice, job_id)
    assert (pending.status, [a.cost_class for a in pending.attempts]) == ("pending", ["pending"])
    allocation = job.attempts[0].allocation_id or ""
    usage.rows = [row("u1", "sess-1", "0.25", allocation=allocation, manifest="manifest-1")]
    await engine.costs.sync_now(admin)
    cost = await engine.costs.for_job(alice, job_id)
    assert [(a.cost_class, a.fees.fee_eth) for a in cost.attempts] == [
        ("failed_after_payment", Decimal("0.25"))
    ]
    assert (cost.billable.fee_eth, cost.billable.row_count) == (Decimal(0), 0)  # nothing to bill

    assert (await engine.jobs.result(alice, await finished(engine, alice))).content == bytes(
        64
    )  # at the limit


# -- the end of a job is one transaction -----------------------------------------------------------------


class FailsOneFinish(SqlStore):
    """The real store, whose finishing transaction fails once: nothing of it is written."""

    failures = 1

    async def finish_job_with_result(self, *args: Any, **kwargs: Any) -> bool:
        if self.failures:
            self.failures -= 1
            raise OSError("the disk is full")
        return await super().finish_job_with_result(*args, **kwargs)


async def test_a_finish_that_fails_stores_no_bytes_and_the_paid_job_ends_uncertain(
    make_engine: EngineFactory,
    alice: ActorContext,
    transport: ScriptedTransport,
    store: SqlStore,
    usage: FakeUsageSource,
    admin: ActorContext,
) -> None:
    """The runner answered and was paid, and the write of the job's end failed. No bytes are stored
    and the job is not ``succeeded``; it is ``uncertain``, as an immediate job that lost its end is.
    Its owner cannot read the answer, so the payment is never billed as delivered: the cost follows
    the job, as for an answer too large to store (D69 (4))."""
    engine = await make_engine(supply=EngineSupply(store=FailsOneFinish(store.engine)))
    await allot(engine, alice)
    transport.steps = [image(), image()]
    job_id = await finished(engine, alice)

    job = await engine.jobs.get(alice, job_id)
    assert job.failure is not None
    assert (job.state, job.result, job.failure.kind, job.failure.payment_sent) == (
        "uncertain",
        None,
        "runner_error",
        True,
    )
    assert job.failure.message == "engine error: OSError"
    assert [a.outcome for a in job.attempts] == ["succeeded"]  # the runner did answer
    assert await engine.store.read_result(job_id, engine.clock.now()) is None
    with pytest.raises(JobFailed):
        await engine.jobs.result(alice, job_id)
    assert engine.admission.running == 0
    assert len(transport.calls) == 1  # and it is never run again

    allocation = job.attempts[0].allocation_id or ""
    usage.rows = [row("u1", "sess-1", "0.25", allocation=allocation, manifest="manifest-1")]
    await engine.costs.sync_now(admin)
    cost = await engine.costs.for_job(alice, job_id)
    assert [(a.cost_class, a.fees.fee_eth) for a in cost.attempts] == [
        ("failed_after_payment", Decimal("0.25"))
    ]
    assert (cost.billable.fee_eth, cost.billable.row_count) == (Decimal(0), 0)  # nothing to bill

    usage.rows = []
    assert (await engine.jobs.result(alice, await finished(engine, alice))).content == PNG  # the next one


# -- submit refuses what an async job cannot do --------------------------------------------------------------


async def test_submit_refuses_what_only_a_stream_answers(
    make_engine: EngineFactory, alice: ActorContext
) -> None:
    engine = await make_engine()
    await allot(engine, alice)
    with pytest.raises(InvalidRequest, match="return_http_errors"):
        await engine.jobs.submit(alice, queued(return_http_errors=True))
    with pytest.raises(InvalidRequest, match=r"jobs\.submit"):
        await engine.jobs.submit(alice, JobRequest(app=APP))
    with pytest.raises(InvalidRequest, match=r"jobs\.submit"):
        await engine.jobs.run(alice, queued())
    with pytest.raises(InvalidRequest, match="must start with"):
        await engine.jobs.submit(alice, queued(path="run"))
    assert await engine.jobs.list_jobs(alice) == []


async def test_a_cancelled_job_answers_with_its_failure(
    make_engine: EngineFactory, alice: ActorContext
) -> None:
    engine = await make_engine()
    await allot(engine, alice)
    job = await engine.jobs.submit(alice, queued())
    await engine.jobs.cancel(alice, job.id)
    with pytest.raises(JobFailed) as failed:
        await engine.jobs.result(alice, job.id)
    assert failed.value.job.failure == Failure(
        kind="abandoned", message="cancelled before it ran", reason="cancelled"
    )
