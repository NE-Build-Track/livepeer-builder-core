"""ISS-26: a claimed job has one owner, and the gate it passed is one gate.

Before a job is claimed the worker takes, in one call that never awaits, a worker, an admission slot
and the job's own id (``AdmissionLimiter.async_lane``). So two steps that overlap, as when the queue is
also driven by hand with ``run_once()``, never run more jobs than there are workers and never both go
for one job; a claim that is still being written is its step's and nobody else's to resolve; and once
the engine has begun to stop, nothing more is claimed. After the claim, every write for the job checks
the claim's number: a worker that no longer holds the job writes nothing, pays nobody and does not end
it.

The store is the real ``SqlStore`` on SQLite, with one write held or ordered where a test needs it, and
the transport is the scripted one.
"""

import asyncio
from typing import Any

import pytest

from livepeer_builder import EngineSupply
from livepeer_builder.contracts import ActorContext, Failure, JobRequest
from livepeer_builder.testing import FixedClock, ScriptedStreamTransport, ScriptedTransport, StreamScript
from tests.conftest import EngineFactory, allot
from tests.store.test_async_restart import WaitsForItsAllocation, claims
from tests.unit.test_async_cut_short import until
from tests.unit.test_async_jobs import APP, calls, echo, on_a_file, queued  # noqa: F401 - on_a_file: a fixture


def events(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [getattr(record, "event", "") for record in caplog.records]


async def test_two_steps_at_once_never_run_more_jobs_than_there_are_workers(
    make_engine: EngineFactory,
    alice: ActorContext,
    transport: ScriptedTransport,
    clock: FixedClock,
    on_a_file: EngineSupply,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One worker, two jobs, and a second step that begins while the first has claimed its job and not
    yet started it. The second step saw no job running and claimed and paid for a second one."""
    engine = await make_engine(supply=on_a_file, async_workers=1)
    await allot(engine, alice)
    first_job = await engine.jobs.submit(alice, queued(payload={"n": 1}))
    clock.advance(1)
    second_job = await engine.jobs.submit(alice, queued(payload={"n": 2}))
    transport.steps, transport.gate = [echo, echo], asyncio.Event()
    counted, counting, go_on = engine.store.queued_stats, asyncio.Event(), asyncio.Event()

    async def first_count_waits() -> Any:
        if not counting.is_set():
            counting.set()
            await go_on.wait()
        return await counted()

    monkeypatch.setattr(engine.store, "queued_stats", first_count_waits)
    first = asyncio.create_task(engine.jobs.worker.run_once())
    await counting.wait()  # the first step holds its claim, and its job has not started
    second = asyncio.create_task(engine.jobs.worker.run_once())
    try:
        await until(lambda: second.done() or bool(transport.calls), "the second step claimed or gave up")

        assert transport.calls == []  # the only worker is taken: nothing was claimed, nothing paid
        assert await second == 0
        go_on.set()
        await calls(transport, 1)
        assert [(await engine.jobs.get(alice, job.id)).state for job in (first_job, second_job)] == [
            "running",
            "queued",
        ]
    finally:
        go_on.set()
        transport.gate.set()
        await asyncio.wait([first, second], timeout=10)
    assert await first == 1
    assert (len(transport.calls), engine.admission.running) == (1, 0)


async def test_a_claim_that_is_being_written_is_not_another_steps_to_resolve(
    make_engine: EngineFactory,
    alice: ActorContext,
    transport: ScriptedTransport,
    clock: FixedClock,
    on_a_file: EngineSupply,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two steps read the same page of the queue. The first's claim is committed and its answer is on
    its way; the second's claim of the same job fails. The second took the job for one nobody runs
    and put it back: the first then paid for a job that was queued again, its result was dropped, and
    the job was claimed a second time."""
    engine = await make_engine(supply=on_a_file, async_workers=2)
    await allot(engine, alice)
    job = await engine.jobs.submit(alice, queued())
    transport.steps = [ScriptedTransport.ok(b"paid once")]
    read, claim = engine.store.queued_jobs, engine.store.claim_job
    pages, claimers = 0, 0
    both_read, committed, answer = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def both_steps_read_first(*args: Any, **kwargs: Any) -> Any:
        nonlocal pages
        page = await read(*args, **kwargs)
        pages += 1
        if pages >= 2:
            both_read.set()
        await both_read.wait()
        return page

    async def first_answers_late_and_second_fails(*args: Any, **kwargs: Any) -> Any:
        nonlocal claimers
        claimers += 1
        if claimers > 1:
            await committed.wait()  # the other step's claim is written first
            raise OSError("the database went away")
        claimed = await claim(*args, **kwargs)
        committed.set()
        await answer.wait()
        return claimed

    monkeypatch.setattr(engine.store, "queued_jobs", both_steps_read_first)
    monkeypatch.setattr(engine.store, "claim_job", first_answers_late_and_second_fails)
    steps = [asyncio.create_task(engine.jobs.worker.run_once()) for _ in range(2)]
    try:
        await committed.wait()  # one step's claim is written, and it has not heard so
        (other,), (claiming,) = await asyncio.wait(steps, return_when=asyncio.FIRST_COMPLETED, timeout=10)
        (outcome,) = await asyncio.gather(other, return_exceptions=True)

        assert (await engine.jobs.get(alice, job.id)).state == "running"  # the claiming step's, left alone
        assert outcome == 0  # the other step never went for a job that was being claimed
    finally:
        answer.set()
        await asyncio.wait(steps, timeout=10)
    assert await claiming == 1
    ended = await engine.jobs.get(alice, job.id)
    assert (ended.state, [a.outcome for a in ended.attempts]) == ("succeeded", ["succeeded"])
    assert await engine.store.read_result(job.id, clock.now()) == b"paid once"
    assert (len(transport.calls), await claims(engine, job.id), engine.admission.running) == (1, 1, 0)


async def test_a_worker_that_lost_its_claim_pays_nobody_and_does_not_end_the_job(
    make_engine: EngineFactory,
    alice: ActorContext,
    transport: ScriptedTransport,
    clock: FixedClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The job is taken from its worker between the claim and the first attempt: put back and claimed
    again, as a second engine on the database would (T44). The first attempt is written only under the
    claim that holds the job, so the worker that lost it stops there: no runner is called, nothing is
    written, and the job is whoever claimed it last's to end."""
    credentials = WaitsForItsAllocation()
    credentials.hold = False
    engine = await make_engine(supply=EngineSupply(credentials=credentials))  # type: ignore[arg-type]
    job = await engine.jobs.submit(alice, queued())
    credentials.hold = True
    transport.steps = [echo]
    step = asyncio.create_task(engine.jobs.worker.run_once())
    await credentials.asked.wait()  # claimed, and held before its first attempt

    assert await engine.store.requeue_unstarted(job.id, 3)
    assert await engine.store.claim_job(job.id, clock.now()) is not None
    assert await claims(engine, job.id) == 2
    credentials.release.set()
    assert await step == 1

    left = await engine.jobs.get(alice, job.id)
    assert (left.state, left.attempts, left.failure) == ("running", (), None)
    assert transport.calls == []
    assert engine.admission.running == 0  # the slot is given back: the job is no longer this worker's
    assert events(caplog).count("jobs.async_claim_lost") == 1
    assert "jobs.finish_missed" not in events(caplog) and "jobs.async_failed" not in events(caplog)


async def test_nothing_is_claimed_while_the_engine_waits_for_its_streams_to_close(
    make_engine: EngineFactory,
    alice: ActorContext,
    transport: ScriptedStreamTransport,
    on_a_file: EngineSupply,  # noqa: F811
) -> None:
    """``stop()`` gives open streams their time first. A job queued in that time was claimed and paid
    by an engine that was already stopping."""
    engine = await make_engine(supply=on_a_file, stream_drain_timeout_s=30, async_poll_s=0.01)
    await allot(engine, alice)
    await engine.start()
    transport.opens = [StreamScript(events=[b"a", 30.0, b"b"])]
    stream = await engine.jobs.streaming.stream(alice, JobRequest(app=APP))
    transport.steps = [echo]

    stopping = asyncio.create_task(engine.stop())
    await asyncio.sleep(0)  # stop runs up to its first wait: the open stream
    try:
        job = await engine.jobs.submit(alice, queued())  # still accepted: it waits for the next start
        assert await engine.jobs.worker.run_once() == 0

        assert len(transport.calls) == 1  # the stream's own call, and no other
        assert (await engine.jobs.get(alice, job.id)).state == "queued"
    finally:
        await stream.aclose()
        await stopping
    assert len(transport.calls) == 1
    assert (await engine.store.get_job(job.id)).state == "queued"  # type: ignore[union-attr]


async def test_a_stop_that_arrives_between_two_claims_of_one_page_ends_the_claiming(
    make_engine: EngineFactory,
    alice: ActorContext,
    transport: ScriptedTransport,
    clock: FixedClock,
    on_a_file: EngineSupply,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The step had read a page of two jobs when the engine began to stop. "May I still claim" was
    asked once a page, so the second job was claimed all the same.

    The first job's claim was being written when the stop came. It was started when the claim answered,
    and paid for while the engine waited for its streams to close (review of ``19986ef``): a job that
    is claimed and not yet started when the gate closes goes back to the queue, as a job the stop
    cancels before its first attempt does. Nothing new is paid for once the engine stops."""
    engine = await make_engine(supply=on_a_file, async_workers=2, async_drain_timeout_s=5)
    await allot(engine, alice)
    first = await engine.jobs.submit(alice, queued(payload={"n": 1}))
    clock.advance(1)
    second = await engine.jobs.submit(alice, queued(payload={"n": 2}))
    transport.steps = [echo, echo]
    claim, stops = engine.store.claim_job, []

    async def the_engine_stops_during_the_first_claim(*args: Any, **kwargs: Any) -> Any:
        claimed = await claim(*args, **kwargs)
        if not stops:
            stops.append(asyncio.create_task(engine.stop()))
            await asyncio.sleep(0)  # stop runs up to its first wait
        return claimed

    monkeypatch.setattr(engine.store, "claim_job", the_engine_stops_during_the_first_claim)
    step = asyncio.create_task(engine.jobs.worker.run_once())
    await asyncio.wait([step], timeout=10)
    await asyncio.wait(stops, timeout=10)

    assert step.done() and step.result() == 0  # nothing was started once the gate had closed
    assert transport.calls == []
    assert [(await engine.store.get_job(job.id)).state for job in (first, second)] == ["queued"] * 2  # type: ignore[union-attr]
    assert await claims(engine, first.id) == 1 and engine.admission.running == 0  # put back, and counted


async def test_a_job_another_writer_ended_is_not_ended_again_by_the_worker_that_held_it(
    make_engine: EngineFactory,
    alice: ActorContext,
    transport: ScriptedTransport,
    clock: FixedClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The job is ended under its worker, as a second engine's start would end it (T44), and then the
    worker is stopped. It neither puts the job back nor writes an end of its own over the other."""
    credentials = WaitsForItsAllocation()
    credentials.hold = False
    supply = EngineSupply(credentials=credentials)  # type: ignore[arg-type]
    engine = await make_engine(supply=supply, async_drain_timeout_s=0.05)
    job = await engine.jobs.submit(alice, queued())
    credentials.hold = True
    step = asyncio.create_task(engine.jobs.worker.run_once())
    await credentials.asked.wait()  # claimed, and held before its first attempt
    elsewhere = Failure(kind="interrupted", message="the engine stopped while this job was running")
    assert await engine.store.finish_job(job.id, "failed", elsewhere, None, clock.now())

    await engine.jobs.worker.drain()
    await asyncio.wait([step])

    left = await engine.jobs.get(alice, job.id)
    assert (left.state, left.failure) == ("failed", elsewhere)
    assert (transport.calls, engine.admission.running) == ([], 0)
    assert events(caplog).count("jobs.async_claim_lost") == 1


async def test_a_claim_that_finds_its_job_gone_gives_back_what_it_took(
    make_engine: EngineFactory,
    alice: ActorContext,
    transport: ScriptedTransport,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The job was cancelled between the worker's look at the queue and its claim. The claim misses,
    and the worker, the slot and the job's id it took for it are free again."""
    engine = await make_engine()
    await allot(engine, alice)
    job = await engine.jobs.submit(alice, queued())
    await engine.jobs.cancel(alice, job.id)

    async def read_before_the_cancel(*_: Any, **__: Any) -> list[tuple[Any, str]]:
        return [(job.id, job.app)]

    monkeypatch.setattr(engine.store, "queued_jobs", read_before_the_cancel)
    assert await engine.jobs.worker.run_once() == 0

    lane = engine.admission.async_lane
    assert (engine.admission.running, lane.room, lane.holds(job.id)) == (0, 1, False)
    assert transport.calls == []
    assert (await engine.jobs.get(alice, job.id)).state == "failed"  # as the cancel left it
