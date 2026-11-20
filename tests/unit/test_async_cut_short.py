"""ISS-26, #184 review: a submit and a worker's step that are cut short leave no job behind.

A job is in the queue once its row is committed, whatever happens to the request or the step that wrote
it. These tests cut each one short just after a commit: a submit whose request is cancelled, and a step
whose second claim fails. The store is the real ``SqlStore`` on SQLite and the transport is the scripted
one. The first test runs the worker's real loop and waits for a condition, never for a length of time.
"""

import asyncio
from collections.abc import Callable
from typing import Any
from uuid import UUID

import pytest

from livepeer_builder.contracts import ActorContext
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
