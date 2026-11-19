"""ISS-26: asynchronous jobs. A job outlives the request that submitted it: it is stored ``queued``, a
worker claims it and runs the same paid call an immediate job runs, and its end is read back by id
(docs/design-docs/m4-guides/iss-26-async-jobs.md, D69).

These tests drive the worker step by step with ``run_once()``: no loop runs and nothing sleeps. The store
is the real ``SqlStore`` on SQLite and the transport is the scripted one. The results a job leaves, its
repeats and its end at stop are in ``test_async_results.py`` and ``test_async_repeats.py``.
"""

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import pytest

from livepeer_builder import EngineSupply
from livepeer_builder.contracts import ActorContext, JobRequest, Runner, RunnerReply
from livepeer_builder.errors import JobFailed, NoCapacity
from livepeer_builder.store import SqlStore
from livepeer_builder.testing import FixedClock, ScriptedTransport, runner
from tests.conftest import EngineFactory, allot

APP, OTHER = "example/echo", "example/other"


def queued(**fields: object) -> JobRequest:
    return JobRequest.model_validate({"app": APP, "mode": "async", "payload": {"x": 1}, **fields})


def echo(_: Runner, request: JobRequest, __: str | None) -> RunnerReply:
    """A runner that answers with the payload it was sent, paid."""
    return ScriptedTransport.ok(json.dumps(request.payload).encode())


@pytest.fixture
async def on_a_file(tmp_path: Path) -> AsyncIterator[EngineSupply]:
    """A store on a database file, for a test where two jobs run at once: the in-memory store is one
    connection, so the writes of two jobs would share a transaction."""
    store = await SqlStore.connect(f"sqlite+aiosqlite:///{tmp_path / 'engine.db'}")
    yield EngineSupply(store=store)
    await store.close()


async def calls(transport: ScriptedTransport, count: int) -> None:
    """Wait until the transport holds ``count`` calls: a condition, never a length of time. It gives up
    after far more turns of the loop than any step here takes, so a job that never starts fails the test
    instead of hanging it."""
    for _ in range(20_000):
        if len(transport.calls) >= count:
            return
        await asyncio.sleep(0)
    raise AssertionError(f"the transport was called {len(transport.calls)} times, not {count}")


async def test_submit_returns_a_queued_job_and_calls_no_runner(
    make_engine: EngineFactory, alice: ActorContext, transport: ScriptedTransport
) -> None:
    """The starter test: nothing is dispatched, paid or admitted at submit."""
    engine = await make_engine()
    await allot(engine, alice)

    job = await engine.jobs.submit(alice, queued())

    assert (job.state, job.mode) == ("queued", "async")
    assert (job.attempts, job.started_at, job.completed_at) == ((), None, None)
    assert transport.calls == []
    assert engine.admission.running == 0  # no admission slot is held while a job waits
    stored = await engine.jobs.get(alice, job.id)
    assert stored == job


async def test_run_once_dispatches_the_oldest_queued_job_and_stores_its_result(
    make_engine: EngineFactory, alice: ActorContext, transport: ScriptedTransport, clock: FixedClock
) -> None:
    engine = await make_engine()  # one worker, as make_engine sets it
    await allot(engine, alice)
    first = await engine.jobs.submit(alice, queued(payload={"n": 1}))
    clock.advance(5)
    second = await engine.jobs.submit(alice, queued(payload={"n": 2}))
    transport.steps = [echo, echo]

    assert await engine.jobs.worker.run_once() == 1  # one worker: the oldest job, and only that one
    ran = await engine.jobs.get(alice, first.id)
    assert (ran.state, ran.started_at, ran.completed_at) == ("succeeded", clock.now(), clock.now())
    assert [(a.outcome, a.payment_sent, a.allocation_id is not None) for a in ran.attempts] == [
        ("succeeded", True, True)
    ]
    assert ran.result is not None
    assert (ran.result.storage, ran.result.location) == ("stored", f"/v1/jobs/{first.id}/result")
    assert (ran.result.size_bytes, ran.result.sha256) == (8, hashlib.sha256(b'{"n": 1}').hexdigest())
    assert ran.result.expires_at == clock.now() + timedelta(days=7)
    assert await engine.store.read_result(first.id, clock.now()) == b'{"n": 1}'
    assert (await engine.jobs.get(alice, second.id)).state == "queued"
    assert engine.admission.running == 0  # the slot went back with the job's end

    assert await engine.jobs.worker.run_once() == 1
    assert (await engine.jobs.get(alice, second.id)).state == "succeeded"
    assert await engine.store.read_result(second.id, clock.now()) == b'{"n": 2}'
    assert await engine.jobs.worker.run_once() == 0  # an empty queue: nothing to do
    assert len(transport.calls) == 2


async def test_the_worker_runs_as_many_jobs_at_once_as_it_has_workers(
    make_engine: EngineFactory,
    alice: ActorContext,
    transport: ScriptedTransport,
    clock: FixedClock,
    on_a_file: EngineSupply,
) -> None:
    engine = await make_engine(supply=on_a_file, async_workers=2)
    await allot(engine, alice)
    jobs = []
    for n in range(3):
        jobs.append(await engine.jobs.submit(alice, queued(payload={"n": n})))
        clock.advance(1)  # oldest first: each is older than the next
    transport.steps = [echo, echo, echo]
    transport.gate = asyncio.Event()

    step = asyncio.create_task(engine.jobs.worker.run_once())
    await calls(transport, 2)
    assert [(await engine.jobs.get(alice, job.id)).state for job in jobs] == ["running", "running", "queued"]
    assert engine.admission.running == 2
    transport.gate.set()
    assert await step == 2
    assert await engine.jobs.worker.run_once() == 1
    assert [(await engine.jobs.get(alice, job.id)).state for job in jobs] == ["succeeded"] * 3


async def test_the_worker_never_runs_a_job_without_an_admission_slot(
    make_engine: EngineFactory, alice: ActorContext, transport: ScriptedTransport
) -> None:
    """Immediate and async jobs share ``LPB_MAX_CONCURRENT_JOBS``, in both directions."""
    engine = await make_engine(max_concurrent_jobs=1)
    await allot(engine, alice)
    transport.steps = [ScriptedTransport.ok(), echo, ScriptedTransport.ok()]
    transport.gate = asyncio.Event()

    immediate = asyncio.create_task(engine.jobs.run(alice, JobRequest(app=APP)))
    await calls(transport, 1)  # the immediate job holds the only slot
    waiting = await engine.jobs.submit(alice, queued())  # queued all the same: a submit takes no slot
    assert await engine.jobs.worker.run_once() == 0
    assert (await engine.jobs.get(alice, waiting.id)).state == "queued"
    assert len(transport.calls) == 1
    transport.gate.set()
    await immediate

    transport.gate = asyncio.Event()
    step = asyncio.create_task(engine.jobs.worker.run_once())
    await calls(transport, 2)  # now the async job holds it
    with pytest.raises(NoCapacity):
        await engine.jobs.run(alice, JobRequest(app=APP))
    transport.gate.set()
    assert await step == 1
    assert (await engine.jobs.get(alice, waiting.id)).state == "succeeded"
    assert engine.admission.running == 0


async def test_an_app_at_its_cap_does_not_block_another_apps_job(
    make_engine: EngineFactory,
    alice: ActorContext,
    transport: ScriptedTransport,
    clock: FixedClock,
    on_a_file: EngineSupply,
) -> None:
    """Two workers, and three jobs of one app ahead of another app's job. The second worker does not
    wait behind the app that is at its limit: it takes the other app's job, though that one is newer."""
    engine = await make_engine(
        (runner(APP), runner(OTHER)), supply=on_a_file, async_workers=2, max_concurrent_jobs_per_app=1
    )
    await allot(engine, alice)
    order = []
    for app in (APP, APP, APP, OTHER):
        order.append(await engine.jobs.submit(alice, queued(app=app)))
        clock.advance(1)
    transport.steps = [echo, echo, echo, echo]
    transport.gate = asyncio.Event()

    step = asyncio.create_task(engine.jobs.worker.run_once())
    await calls(transport, 2)
    assert [(await engine.jobs.get(alice, job.id)).state for job in order] == [
        "running",
        "queued",
        "queued",
        "running",
    ]
    transport.gate.set()
    assert await step == 2
    assert await engine.jobs.worker.run_once() == 1  # APP is free again: its next job, one at a time
    assert [(await engine.jobs.get(alice, job.id)).state for job in order[1:3]] == ["succeeded", "queued"]


async def test_a_full_queue_refuses_the_submit_and_stores_nothing(
    make_engine: EngineFactory, alice: ActorContext, transport: ScriptedTransport
) -> None:
    engine = await make_engine(async_max_queued=2)
    await allot(engine, alice)
    held = [await engine.jobs.submit(alice, queued(payload={"n": n})) for n in range(2)]
    with pytest.raises(NoCapacity, match="queue is full"):
        await engine.jobs.submit(alice, queued(operation_ref="order-3"))
    assert {job.id for job in await engine.jobs.list_jobs(alice)} == {job.id for job in held}
    assert await engine.store.job_by_operation_ref("default", "alice", "order-3") is None

    transport.steps = [echo]
    assert await engine.jobs.worker.run_once() == 1
    await engine.jobs.submit(alice, queued(operation_ref="order-3"))  # there is room again


async def test_a_job_queued_past_the_time_limit_fails_unpaid(
    make_engine: EngineFactory, alice: ActorContext, transport: ScriptedTransport, clock: FixedClock
) -> None:
    engine = await make_engine(async_queue_ttl_s=600)
    await allot(engine, alice)
    old = await engine.jobs.submit(alice, queued(operation_ref="order-1"))
    clock.advance(599)
    fresh = await engine.jobs.submit(alice, queued())
    clock.advance(2)  # the first job has waited 601 s, the second 2 s
    transport.steps = [echo]

    assert await engine.jobs.worker.run_once() == 1  # only the fresh one was dispatched
    expired = await engine.jobs.get(alice, old.id)
    assert expired.failure is not None
    assert (expired.state, expired.failure.kind, expired.failure.payment_sent) == (
        "failed",
        "no_capacity",
        False,
    )
    assert (expired.attempts, expired.started_at, expired.completed_at) == ((), None, clock.now())
    assert (await engine.costs.for_job(alice, old.id)).status == "none"
    assert (await engine.jobs.get(alice, fresh.id)).state == "succeeded"
    assert len(transport.calls) == 1
    with pytest.raises(JobFailed) as failed:  # read as any failed job
        await engine.jobs.result(alice, old.id)
    assert failed.value.job == expired


async def test_health_counts_the_queue_and_the_age_of_its_oldest_job(
    make_engine: EngineFactory, alice: ActorContext, transport: ScriptedTransport, clock: FixedClock
) -> None:
    engine = await make_engine()
    await allot(engine, alice)
    assert (engine.health().queued_jobs, engine.health().oldest_queued_s) == (0, None)
    await engine.jobs.submit(alice, queued())
    clock.advance(30)
    await engine.jobs.submit(alice, queued())
    clock.advance(10)
    health = engine.health()
    assert (health.queued_jobs, health.oldest_queued_s, health.running_jobs) == (2, 40.0, 0)

    transport.steps = [echo, echo]
    await engine.jobs.worker.run_once()
    assert (engine.health().queued_jobs, engine.health().oldest_queued_s) == (1, 10.0)
    await engine.jobs.worker.run_once()
    assert (engine.health().queued_jobs, engine.health().oldest_queued_s) == (0, None)
