"""ISS-26, #184 review: a submit and a worker's step that are cut short leave no job behind.

A job is in the queue once its row is committed, whatever happens to the request or the step that wrote
it. These tests cut each one short just after a commit: a submit whose request is cancelled, a step
whose second claim fails, and a step cancelled while the answer to its claim is on its way. The store is
the real ``SqlStore`` on SQLite and the transport is the scripted one. The first test runs the worker's
real loop and waits for a condition, never for a length of time.
"""

import asyncio
from collections.abc import Callable
from typing import Any
from uuid import UUID

import pytest

from livepeer_builder.contracts import ActorContext
from livepeer_builder.services.failover import MAX_CLAIMS
from livepeer_builder.testing import FixedClock, ScriptedTransport
from tests.conftest import EngineFactory, allot
from tests.unit.test_async_jobs import calls, echo, queued


async def until(condition: Callable[[], bool], what: str) -> None:
    """Wait for the engine to get somewhere: at most ten seconds, then the test fails."""
    deadline = asyncio.get_running_loop().time() + 10
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"timed out waiting until {what}")
        await asyncio.sleep(0.001)


async def test_a_submit_cancelled_after_its_job_was_stored_still_wakes_the_worker(
    make_engine: EngineFactory,
    alice: ActorContext,
    transport: ScriptedTransport,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The worker's loop reads the queue only while it knows of a waiting job, and a submit is what
    tells it. A request cancelled between the job's commit and that word left the job ``queued`` with
    nobody looking, until another submit or a restart."""
    engine = await make_engine(async_poll_s=0.01)
    await allot(engine, alice)
    stop = asyncio.Event()
    loop = asyncio.create_task(engine.jobs.worker.run(stop))
    stored, answered = asyncio.Event(), asyncio.Event()
    enqueue = engine.store.enqueue_job

    async def answers_late(*args: Any, **kwargs: Any) -> None:
        await enqueue(*args, **kwargs)  # the job is committed
        stored.set()
        await answered.wait()  # and the request is cancelled while the write's answer is on its way

    monkeypatch.setattr(engine.store, "enqueue_job", answers_late)
    transport.steps = [echo]
    try:
        submit = asyncio.create_task(engine.jobs.submit(alice, queued()))
        await stored.wait()
        submit.cancel()
        with pytest.raises(asyncio.CancelledError):
            await submit
        answered.set()

        await calls(transport, 1)  # the worker started the job, with no second submit to wake it
        await until(lambda: engine.admission.running == 0, "the job ended")
        (job,) = await engine.jobs.list_jobs(alice)
        assert job.state == "succeeded"
        assert engine.health().queued_jobs == 0
    finally:
        stop.set()
        await loop


async def test_a_claim_that_fails_does_not_lose_the_job_claimed_before_it(
    make_engine: EngineFactory,
    alice: ActorContext,
    transport: ScriptedTransport,
    clock: FixedClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A claim is committed when it returns, and it holds an admission slot. A step that claimed one job
    and then failed on the next left the first ``running`` with nobody running it, and its slot taken
    until the engine stopped."""
    engine = await make_engine(async_workers=2)
    await allot(engine, alice)
    first = await engine.jobs.submit(alice, queued(payload={"n": 1}))
    clock.advance(1)  # oldest first
    second = await engine.jobs.submit(alice, queued(payload={"n": 2}))
    claim = engine.store.claim_job

    async def second_claim_fails(job_id: UUID, *args: Any, **kwargs: Any) -> Any:
        if job_id == second.id:
            raise OSError("the database went away")
        return await claim(job_id, *args, **kwargs)

    monkeypatch.setattr(engine.store, "claim_job", second_claim_fails)
    transport.steps = [echo]

    with pytest.raises(OSError, match="the database went away"):
        await engine.jobs.worker.run_once()

    await calls(transport, 1)  # the job claimed before the failure runs all the same
    await until(lambda: engine.admission.running == 0, "every admission slot was given back")
    assert (await engine.jobs.get(alice, first.id)).state == "succeeded"
    assert await engine.store.read_result(first.id, clock.now()) == b'{"n": 1}'
    assert (await engine.jobs.get(alice, second.id)).state == "queued"  # its claim never happened
    assert len(transport.calls) == 1


async def claim_cut_short(engine: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run one step of the worker and cancel it once its first claim is committed, before the claim's
    answer is back: the row says ``running``, and the step never learns it."""
    committed = asyncio.Event()
    claim = engine.store.claim_job

    async def answers_late(*args: Any, **kwargs: Any) -> None:
        await claim(*args, **kwargs)
        committed.set()
        await asyncio.Event().wait()

    with monkeypatch.context() as patched:
        patched.setattr(engine.store, "claim_job", answers_late)
        step = asyncio.create_task(engine.jobs.worker.run_once())
        await committed.wait()
        step.cancel()
        with pytest.raises(asyncio.CancelledError):
            await step


async def test_a_step_cancelled_while_its_claim_was_being_written_puts_the_job_back(
    make_engine: EngineFactory,
    alice: ActorContext,
    transport: ScriptedTransport,
    clock: FixedClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A claim is committed before its answer reaches the worker. A step cancelled in between gave the
    slot back and forgot the job: its row stayed ``running`` with nobody running it, until a restart.
    Nothing went out for it, so it goes back to the queue, as a job stopped before its first attempt
    does."""
    engine = await make_engine()
    await allot(engine, alice)
    job = await engine.jobs.submit(alice, queued())

    await claim_cut_short(engine, monkeypatch)

    assert engine.admission.running == 0  # the slot was given back
    assert (await engine.jobs.get(alice, job.id)).state == "queued"  # and the job waits again
    assert transport.calls == []
    transport.steps = [echo]
    assert await engine.jobs.worker.run_once() == 1  # the next step runs it
    assert (await engine.jobs.get(alice, job.id)).state == "succeeded"
    assert await engine.store.read_result(job.id, clock.now()) == b'{"x": 1}'


async def test_a_claim_cut_short_for_the_last_time_ends_the_job_unpaid(
    make_engine: EngineFactory,
    alice: ActorContext,
    transport: ScriptedTransport,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A job is claimed ``MAX_CLAIMS`` times at most (D69 (2)). One whose last claim was cut short is not
    left ``running``: it ends as a job stopped before its first attempt ends once it cannot go back."""
    engine = await make_engine()
    await allot(engine, alice)
    job = await engine.jobs.submit(alice, queued())

    for _ in range(MAX_CLAIMS):
        assert (await engine.jobs.get(alice, job.id)).state == "queued"
        await claim_cut_short(engine, monkeypatch)

    ended = await engine.jobs.get(alice, job.id)
    assert ended.failure is not None
    assert (ended.state, ended.failure.kind, ended.failure.payment_sent) == ("failed", "runner_error", False)
    assert (ended.attempts, transport.calls, engine.admission.running) == ((), [], 0)
    assert await engine.jobs.worker.run_once() == 0  # nothing is left to claim


async def test_a_claim_that_fails_on_a_job_another_step_holds_leaves_that_job_running(
    make_engine: EngineFactory,
    alice: ActorContext,
    transport: ScriptedTransport,
    clock: FixedClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two steps at once, as when the queue is also driven by hand: the second read the queue before
    the first claimed, and its own claim of the same job then failed. The job is ``running`` because the
    first step holds it, so the failed claim resolves nothing: it is neither put back nor ended."""
    engine = await make_engine(async_workers=2)
    await allot(engine, alice)
    job = await engine.jobs.submit(alice, queued())
    transport.steps, transport.gate = [ScriptedTransport.ok(b"late")], asyncio.Event()
    first = asyncio.create_task(engine.jobs.worker.run_once())
    await calls(transport, 1)  # the job is at the runner, which has not answered

    async def read_before_the_claim(*_: Any, **__: Any) -> list[tuple[UUID, str]]:
        return [(job.id, job.app)]

    async def fails(*_: Any, **__: Any) -> None:
        raise OSError("the database went away")

    with monkeypatch.context() as patched:
        patched.setattr(engine.store, "queued_jobs", read_before_the_claim)
        patched.setattr(engine.store, "claim_job", fails)
        with pytest.raises(OSError, match="the database went away"):
            await engine.jobs.worker.run_once()

    assert (await engine.jobs.get(alice, job.id)).state == "running"
    transport.gate.set()
    assert await first == 1
    assert (await engine.jobs.get(alice, job.id)).state == "succeeded"
    assert await engine.store.read_result(job.id, clock.now()) == b"late"
    assert (len(transport.calls), engine.admission.running) == (1, 0)


async def test_a_cut_claim_that_cannot_be_resolved_is_logged_and_the_step_reports_what_stopped_it(
    make_engine: EngineFactory,
    alice: ActorContext,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The look that resolves a cut claim uses the same database. If it fails too, the step still
    raises what stopped the claim, the slot goes back, and a restart resolves the job."""
    engine = await make_engine()
    await allot(engine, alice)
    await engine.jobs.submit(alice, queued())

    async def claim_fails(*_: Any, **__: Any) -> None:
        raise OSError("the database went away")

    async def read_fails(*_: Any, **__: Any) -> None:
        raise OSError("and it is still away")

    monkeypatch.setattr(engine.store, "claim_job", claim_fails)
    monkeypatch.setattr(engine.store, "get_job", read_fails)
    with pytest.raises(OSError, match="the database went away"):
        await engine.jobs.worker.run_once()

    assert engine.admission.running == 0
    assert "jobs.async_unclaim_failed" in [getattr(record, "event", None) for record in caplog.records]
