"""ISS-26: the end of a claimed job is the worker's to write, until it is written.

A job's task can end without having written the job's end: the store was away when the job finished, or
the claim's own answer never came and the look that would resolve it failed too. The job was then
``running`` with nobody running it until the next restart: its owner polled 409 for ever. The worker
keeps such a job, with its slot, and writes its end on a later step, so once the store answers again
no claimed job stays ``running``. It keeps the job and its attempts, never a runner's reply.

Also here: a step that fails backs off for the poll interval, a job whose claim always fails holds up
nobody, a job stopped with its claims used up ends as a restart ends it, and ``drain()`` returns only
when every write of an end has ended.
"""

import asyncio
from typing import Any

import pytest
from sqlalchemy import delete
from sqlalchemy.exc import NoResultFound

from livepeer_builder import EngineSupply
from livepeer_builder.contracts import ActorContext, JobRequest, Runner, RunnerReply
from livepeer_builder.store import SqlStore
from livepeer_builder.store import schema as t
from livepeer_builder.testing import FixedClock, ScriptedTransport
from tests.conftest import EngineFactory, allot
from tests.store.test_async_restart import WaitsForItsAllocation, claims
from tests.unit.test_async_cut_short import until
from tests.unit.test_async_jobs import calls, echo, queued


class StoreAway:
    """Makes the named writes of the real store fail while ``away`` is set, as when the database is
    unreachable: nothing is written."""

    def __init__(self, store: Any, monkeypatch: pytest.MonkeyPatch, *names: str) -> None:
        self.away = True
        for name in names:
            monkeypatch.setattr(store, name, self._or_fail(getattr(store, name)))

    def _or_fail(self, write: Any) -> Any:
        async def maybe(*args: Any, **kwargs: Any) -> Any:
            if self.away:
                raise OSError("the database went away")
            return await write(*args, **kwargs)

        return maybe


def events(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [getattr(record, "event", "") for record in caplog.records]


def an_end_was_refused(caplog: pytest.LogCaptureFixture) -> bool:
    """The worker logged that it could not write a job's end (before ISS-26's rework: ``close_failed``)."""
    return bool({"jobs.async_end_failed", "jobs.close_failed"} & set(events(caplog)))


async def test_an_end_the_store_refused_is_written_by_a_later_step(
    make_engine: EngineFactory,
    alice: ActorContext,
    transport: ScriptedTransport,
    clock: FixedClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The runner answered and was paid, and then neither the job's result nor its end as a failure
    could be written. The task is over; the job is still the worker's. The step after the store is
    back ends it, as the first write would have: ``uncertain``, since it was paid, and never run again."""
    engine = await make_engine()
    await allot(engine, alice)
    job = await engine.jobs.submit(alice, queued())
    transport.steps = [ScriptedTransport.ok(b"lost")]
    store = StoreAway(engine.store, monkeypatch, "finish_job_with_result", "finish_job")

    assert await engine.jobs.worker.run_once() == 1  # its task ended, its end did not get written
    assert await engine.jobs.worker.run_once() == 0  # the store is still away: nothing changes
    assert (await engine.jobs.get(alice, job.id)).state == "running"
    store.away = False
    assert await engine.jobs.worker.run_once() == 0

    ended = await engine.jobs.get(alice, job.id)
    assert ended.failure is not None
    assert (ended.state, ended.failure.kind, ended.failure.payment_sent, ended.result) == (
        "uncertain",
        "runner_error",
        True,
        None,
    )
    assert ended.failure.message == "engine error: OSError"
    assert [a.outcome for a in ended.attempts] == ["succeeded"]
    assert await engine.store.read_result(job.id, clock.now()) is None  # the reply was not kept for a retry
    assert (len(transport.calls), engine.admission.running) == (1, 0)


async def test_the_loop_writes_an_owed_end_with_nothing_queued_and_nobody_submitting(
    make_engine: EngineFactory,
    alice: ActorContext,
    transport: ScriptedTransport,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No restart, no submit and an empty queue: the loop still steps while it owes an end."""
    engine = await make_engine(async_poll_s=0.01)
    await allot(engine, alice)
    store = StoreAway(engine.store, monkeypatch, "finish_job_with_result", "finish_job")
    transport.steps = [ScriptedTransport.ok(b"lost")]
    loop = asyncio.create_task(engine.jobs.worker.run(asyncio.Event()))
    try:
        job = await engine.jobs.submit(alice, queued())
        await calls(transport, 1)
        await until(lambda: an_end_was_refused(caplog), "the job's end was refused")
        await until(lambda: engine.jobs.worker.queued_jobs == 0, "the queue was counted empty")
        assert (await engine.jobs.get(alice, job.id)).state == "running"
        store.away = False

        await until(
            lambda: engine.admission.running == 0, "the job's end was written and its slot given back"
        )
        assert (await engine.jobs.get(alice, job.id)).state == "uncertain"
    finally:
        await engine.jobs.worker.drain()
        await loop


async def test_a_job_whose_end_is_owed_keeps_its_worker_until_the_end_is_written(
    make_engine: EngineFactory,
    alice: ActorContext,
    transport: ScriptedTransport,
    clock: FixedClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What the worker keeps for a later step is bounded by its workers: an owed end holds its slot, so
    with one worker nothing else is claimed until that end is written."""
    engine = await make_engine()  # one worker
    await allot(engine, alice)
    owed = await engine.jobs.submit(alice, queued(payload={"n": 1}))
    clock.advance(1)
    behind = await engine.jobs.submit(alice, queued(payload={"n": 2}))
    transport.steps = [ScriptedTransport.ok(b"lost"), echo]
    store = StoreAway(engine.store, monkeypatch, "finish_job_with_result", "finish_job")

    assert await engine.jobs.worker.run_once() == 1
    assert await engine.jobs.worker.run_once() == 0  # the one worker is still the first job's
    assert (engine.admission.running, len(transport.calls)) == (1, 1)
    assert (await engine.jobs.get(alice, behind.id)).state == "queued"
    store.away = False
    assert await engine.jobs.worker.run_once() == 1  # the owed end first, then the job behind it

    assert [(await engine.jobs.get(alice, job.id)).state for job in (owed, behind)] == [
        "uncertain",
        "succeeded",
    ]
    assert engine.admission.running == 0


async def test_a_cut_claim_the_store_could_not_resolve_is_resolved_by_a_later_step(
    make_engine: EngineFactory,
    alice: ActorContext,
    transport: ScriptedTransport,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The claim was written and its answer lost, and the look that resolves such a claim failed too.
    The step raises what stopped the claim and the job is ``running`` with nobody running it. The step
    after the store is back puts it in the queue again, and it runs."""
    engine = await make_engine(async_poll_s=0.001)
    await allot(engine, alice)
    job = await engine.jobs.submit(alice, queued())
    claim = engine.store.claim_job

    async def written_and_its_answer_lost(*args: Any, **kwargs: Any) -> Any:
        await claim(*args, **kwargs)
        raise OSError("the database went away")

    with monkeypatch.context() as cut:
        cut.setattr(engine.store, "claim_job", written_and_its_answer_lost)
        store = StoreAway(engine.store, cut, "get_job")
        with pytest.raises(OSError, match="the database went away"):
            await engine.jobs.worker.run_once()
    assert (await engine.jobs.get(alice, job.id)).state == "running"
    del store  # the patches are gone: the store answers again

    transport.steps = [echo]
    ran = 0
    for _ in range(1000):  # the job waits a poll interval after its failed claim; the first step resolves it
        ran += await engine.jobs.worker.run_once()
        if ran:
            break
        await asyncio.sleep(0.001)

    ended = await engine.jobs.get(alice, job.id)
    assert (ended.state, len(transport.calls), engine.admission.running) == ("succeeded", 1, 0)
    assert await claims(engine, job.id) == 2  # the cut claim counted, as a claim cut by a stop does
    assert "jobs.async_end_failed" in events(caplog)  # and the write that failed was logged


async def test_a_step_that_fails_is_tried_again_after_the_poll_interval_and_not_before(
    make_engine: EngineFactory, alice: ActorContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A claim that fails every time used to wake the loop each time it failed: hundreds of tries a
    second. Three tries now take two poll intervals at least, however slow the machine."""
    poll_s = 0.05
    engine = await make_engine(async_poll_s=poll_s)
    await allot(engine, alice)
    tries: list[float] = []

    async def fails(*_: Any, **__: Any) -> None:
        tries.append(asyncio.get_running_loop().time())
        raise OSError("the database went away")

    monkeypatch.setattr(engine.store, "claim_job", fails)
    await engine.jobs.submit(alice, queued())
    stop = asyncio.Event()
    loop = asyncio.create_task(engine.jobs.worker.run(stop))
    try:
        await until(lambda: len(tries) >= 3, "the claim was tried three times")
    finally:
        stop.set()
        await engine.jobs.worker.drain()
        await loop
    assert tries[2] - tries[0] >= 2 * poll_s * 0.9


async def test_a_job_that_cannot_be_claimed_does_not_hold_up_the_jobs_behind_it(
    make_engine: EngineFactory,
    alice: ActorContext,
    transport: ScriptedTransport,
    clock: FixedClock,
    store: SqlStore,
) -> None:
    """The oldest job's stored request is gone, so its claim raises every time. With one worker the step
    asked for one job, got that one, failed, and never reached the job behind it."""
    engine = await make_engine(async_queue_ttl_s=600)  # one worker
    await allot(engine, alice)
    broken = await engine.jobs.submit(alice, queued(payload={"n": 1}))
    clock.advance(1)
    behind = await engine.jobs.submit(alice, queued(payload={"n": 2}))
    async with store.engine.begin() as conn:
        await conn.execute(delete(t.job_requests).where(t.job_requests.c.job_id == str(broken.id)))
    transport.steps = [echo]

    with pytest.raises(NoResultFound):  # the step still says what went wrong
        await engine.jobs.worker.run_once()

    await calls(transport, 1)  # and the job behind the broken one was claimed and started all the same
    await until(lambda: engine.admission.running == 0, "the job behind it ended")
    assert [(await engine.jobs.get(alice, job.id)).state for job in (broken, behind)] == [
        "queued",
        "succeeded",
    ]
    assert await claims(engine, broken.id) == 0
    assert await engine.jobs.worker.run_once() == 0  # set aside for now: not tried, and nothing raised

    clock.advance(600)  # it ends as any job that could not be dispatched in time
    assert await engine.jobs.worker.run_once() == 0
    expired = await engine.jobs.get(alice, broken.id)
    assert expired.failure is not None
    assert (expired.state, expired.failure.kind, expired.attempts) == ("failed", "no_capacity", ())


async def test_a_job_stopped_with_its_claims_used_up_ends_as_a_restart_would_end_it(
    make_engine: EngineFactory, alice: ActorContext, transport: ScriptedTransport, clock: FixedClock
) -> None:
    """Stopped before its first attempt, a job goes back to the queue, three claims at most. On its
    third claim it cannot go back: it ends ``failed``, ``interrupted``, whether the engine's stop finds
    it or the next start does."""
    credentials = WaitsForItsAllocation()
    credentials.hold = False
    supply = EngineSupply(credentials=credentials)  # type: ignore[arg-type]
    engine = await make_engine(supply=supply, async_drain_timeout_s=0.05)
    job = await engine.jobs.submit(alice, queued())
    for _ in range(2):  # claimed twice already, and put back each time
        assert await engine.store.claim_job(job.id, clock.now()) is not None
        assert await engine.store.requeue_unstarted(job.id, 3)
    credentials.hold = True
    step = asyncio.create_task(engine.jobs.worker.run_once())
    await credentials.asked.wait()  # the third claim, held before its first attempt

    await engine.jobs.worker.drain()
    await asyncio.wait([step])

    ended = await engine.jobs.get(alice, job.id)
    assert ended.failure is not None
    assert (ended.state, ended.failure.kind, ended.failure.payment_sent, ended.attempts) == (
        "failed",
        "interrupted",
        False,
        (),
    )
    assert ended.failure.message == "the engine stopped while this job was running"
    assert (transport.calls, engine.admission.running) == ([], 0)


def breaks(_: Runner, __: JobRequest, ___: str | None) -> RunnerReply:
    raise RuntimeError("a transport of the deployment's own, with a bug")


async def test_drain_returns_only_when_every_end_is_written(
    make_engine: EngineFactory,
    alice: ActorContext,
    transport: ScriptedTransport,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The job's task is writing the job's end, after an error, when the drain time passes and the task
    is cancelled. The task ended there and then, the drain returned, and ``stop()`` went on to close
    the store under a write that was still running."""
    engine = await make_engine(async_drain_timeout_s=0.05)
    await allot(engine, alice)
    job = await engine.jobs.submit(alice, queued())
    transport.steps = [breaks]
    finish_job, writing, let_it = engine.store.finish_job, asyncio.Event(), asyncio.Event()

    async def slow(*args: Any, **kwargs: Any) -> Any:
        writing.set()
        await let_it.wait()
        return await finish_job(*args, **kwargs)

    monkeypatch.setattr(engine.store, "finish_job", slow)
    step = asyncio.create_task(engine.jobs.worker.run_once())
    await writing.wait()
    drain = asyncio.create_task(engine.jobs.worker.drain())
    try:
        finished, _ = await asyncio.wait([drain], timeout=0.5)  # ten times the drain time

        assert not finished  # the end is still being written: the drain waits for it
    finally:
        let_it.set()
        await asyncio.wait([drain, step], timeout=10)
    ended = await engine.jobs.get(alice, job.id)
    assert ended.failure is not None
    assert (ended.state, ended.failure.message) == ("uncertain", "engine error: RuntimeError")
    assert engine.admission.running == 0
