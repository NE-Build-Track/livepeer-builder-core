"""ISS-26, #184 review: what the worker knows of its queue, when a count and a submit cross.

The worker keeps the queue's depth as it last counted it, and its loop reads the database only while
that count says a job waits. A count is a read that takes time, and a submit may store a job and tell the
worker while one is under way: the count then says less than the worker already knows. These tests hold a
count open across a submit, on SQLite and PostgreSQL. Where the loop runs it polls once an hour, so only
what the worker was told can make it look.
"""

import asyncio
from datetime import datetime

import pytest

from livepeer_builder import BuilderEngine
from livepeer_builder.contracts import Job
from livepeer_builder.testing import FixedClock, ScriptedStreamTransport
from tests.store.test_async_restart import ALICE, PaysThenWaits, allot, echo, engine_on, queued, states, until

HOUR = 3600.0


class HeldCount:
    """Holds the first count of the queue after its read, until the test lets its answer through."""

    def __init__(self, engine: BuilderEngine) -> None:
        self.read, self.release = asyncio.Event(), asyncio.Event()
        self._counted = engine.store.queued_stats
        engine.store.queued_stats = self._stats  # type: ignore[method-assign]

    async def _stats(self) -> tuple[int, datetime | None]:
        stats = await self._counted()
        if not self.read.is_set():
            self.read.set()
            await self.release.wait()
        return stats


async def test_a_count_read_before_a_job_was_queued_does_not_replace_what_the_submit_noted(
    database_url: str, clock: FixedClock
) -> None:
    """Health said the queue was empty with a job in it: the older count's answer came last, and won."""
    async with engine_on(database_url, clock, ScriptedStreamTransport()) as engine:  # no loop runs
        await allot(engine)
        worker = engine.jobs.worker
        held = HeldCount(engine)
        older = asyncio.create_task(worker.count_queue())  # as a cancel counts, or the end of a step
        await held.read.wait()  # it read an empty queue

        await engine.jobs.submit(ALICE, queued(1))
        assert engine.health().queued_jobs == 1
        held.release.set()

        assert await older == 0  # what it read, for its caller
        assert (engine.health().queued_jobs, engine.health().oldest_queued_s) == (1, 0.0)
        assert await worker.count_queue() == 1  # and a count that began after the submit is kept
        assert engine.health().queued_jobs == 1


async def test_the_loop_starts_a_job_queued_while_an_older_count_was_being_read(
    database_url: str, clock: FixedClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The older count's answer arrives in the turn of the event loop in which the worker is told, and
    before the worker's loop runs. It stored an empty queue over the job, the loop found nothing to do,
    and the job stayed ``queued`` with nobody looking."""
    transport = ScriptedStreamTransport(steps=[echo])
    async with engine_on(database_url, clock, transport, async_poll_s=HOUR) as engine:
        await allot(engine)
        await engine.start()
        worker = engine.jobs.worker
        held = HeldCount(engine)
        told = worker.queued

        def answered_as_the_worker_is_told(job: Job) -> None:
            held.release.set()
            told(job)

        monkeypatch.setattr(worker, "queued", answered_as_the_worker_is_told)
        older = asyncio.create_task(worker.count_queue())
        await held.read.wait()

        job = await engine.jobs.submit(ALICE, queued(1))

        assert await older == 0
        await until(lambda: len(transport.calls) == 1 and engine.admission.running == 0, "the job ran")
        assert await states(engine, job.id) == ["succeeded"]
        assert engine.health().queued_jobs == 0


async def test_a_woken_loop_looks_at_the_queue_even_when_its_last_count_was_zero(
    database_url: str, clock: FixedClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second guard. The count is a memory, and the queue is in the database: whatever leaves the
    count at zero with a job waiting, the next wake finds the job. Here the worker is never told of the
    second job, and the end of the first is what wakes the loop."""
    transport = PaysThenWaits()
    async with engine_on(database_url, clock, transport, async_poll_s=HOUR) as engine:
        await allot(engine)
        await engine.start()
        first = await engine.jobs.submit(ALICE, queued(1))
        await until(lambda: len(transport.calls) == 1, "the first job is at the runner")
        monkeypatch.setattr(engine.jobs.worker, "queued", lambda job: None)
        second = await engine.jobs.submit(ALICE, queued(2))
        assert engine.health().queued_jobs == 0  # the worker knows of no waiting job

        transport.release.set()  # the first job ends: a worker is free, and the loop is woken

        await until(lambda: len(transport.calls) == 2 and engine.admission.running == 0, "both ran")
        assert await states(engine, first.id, second.id) == ["succeeded", "succeeded"]
