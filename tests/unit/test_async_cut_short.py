"""ISS-26, #184 review: a submit that is cut short leaves no job behind.

A job is in the queue once its row is committed, whatever happens to the request that wrote it. This
test cuts a submit short just after that commit, by cancelling its request. The store is the real
``SqlStore`` on SQLite and the transport is the scripted one. It runs the worker's real loop and waits
for a condition, never for a length of time.
"""

import asyncio
from collections.abc import Callable
from typing import Any

import pytest

from livepeer_builder.contracts import ActorContext
from livepeer_builder.testing import ScriptedTransport
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
