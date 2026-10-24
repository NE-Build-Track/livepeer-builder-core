"""How a stream ends (docs/contracts/engine.md#streaming): finishing that survives cancellation (D34),
ownership and shutdown (D36), and a caller that stops because it has everything (D38)."""

import asyncio
import hashlib
import logging

import pytest

from livepeer_builder.contracts import ActorContext, JobRequest
from livepeer_builder.errors import JobFailed
from livepeer_builder.runtime import BuilderEngine
from livepeer_builder.store import SqlStore
from livepeer_builder.testing import ScriptedStreamTransport, StreamScript
from tests.conftest import EngineFactory
from tests.unit.test_streaming import APP, provisioned, read_all


async def ended(engine: BuilderEngine) -> None:
    """Wait for every open stream to finish (finishing runs in its own task)."""
    for _ in range(300):
        if not engine.jobs._open_streams:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("a stream never finished")


# -- D34: finishing always completes ----------------------------------------------------------------


async def test_finishing_survives_repeated_cancellation(
    make_engine: EngineFactory,
    admin: ActorContext,
    alice: ActorContext,
    transport: ScriptedStreamTransport,
    store: SqlStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = await provisioned(make_engine, admin, stream_settle_s=0)
    transport.opens = [StreamScript(events=[b"a", 5.0, b"b"])]
    stream = await engine.jobs.streaming.stream(alice, JobRequest(app=APP))
    finish_job = store.finish_job

    async def slow_finish_job(*args, **kwargs):  # type: ignore[no-untyped-def]
        await asyncio.sleep(0.1)
        await finish_job(*args, **kwargs)

    monkeypatch.setattr(store, "finish_job", slow_finish_job)
    task = asyncio.create_task(read_all(stream))
    await asyncio.sleep(0.02)
    for _ in range(5):  # what an anyio cancel scope does: cancel every await again
        task.cancel()
        await asyncio.sleep(0.01)
    with pytest.raises(asyncio.CancelledError):
        await task
    await ended(engine)
    job = await engine.jobs.get(alice, stream.job.id)
    assert job.state == "uncertain" and job.attempts[0].outcome == "abandoned"
    assert engine.admission.running == 0 and transport.streams[0].closed


async def test_a_store_failure_still_releases_the_slot(
    make_engine: EngineFactory,
    admin: ActorContext,
    alice: ActorContext,
    transport: ScriptedStreamTransport,
    store: SqlStore,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    engine = await provisioned(make_engine, admin, max_concurrent_jobs=1)
    transport.opens = [StreamScript(events=[b"a", b"b"])]
    stream = await engine.jobs.streaming.stream(alice, JobRequest(app=APP))

    async def broken(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("database is gone")

    monkeypatch.setattr(store, "finish_job", broken)
    with caplog.at_level(logging.ERROR):
        assert await read_all(stream) == b"ab"
    assert any(getattr(r, "event", None) == "jobs.stream_finish_failed" for r in caplog.records)
    assert engine.admission.running == 0  # the next job is admitted
    monkeypatch.undo()
    assert (await engine.jobs.get(alice, stream.job.id)).state == "running"
    assert await engine.jobs.recover_interrupted() == 1  # the next start resolves it
    assert (await engine.jobs.get(alice, stream.job.id)).state == "uncertain"


async def test_a_hung_store_is_given_up_on_after_the_finalize_timeout(
    make_engine: EngineFactory,
    admin: ActorContext,
    alice: ActorContext,
    transport: ScriptedStreamTransport,
    store: SqlStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = await provisioned(make_engine, admin, stream_finalize_timeout_s=0.05)
    transport.opens = [StreamScript(events=[b"a"])]
    stream = await engine.jobs.streaming.stream(alice, JobRequest(app=APP))

    async def hangs(*args, **kwargs):  # type: ignore[no-untyped-def]
        await asyncio.sleep(10)

    monkeypatch.setattr(store, "finish_job", hangs)
    await asyncio.wait_for(read_all(stream), timeout=2)
    assert engine.admission.running == 0 and transport.streams[0].closed


# -- D36: who owns an open stream -------------------------------------------------------------------


async def test_async_with_closes_a_stream_the_app_never_read(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, transport: ScriptedStreamTransport
) -> None:
    engine = await provisioned(make_engine, admin)
    transport.opens = [StreamScript(events=[b"a", b"b"])]
    stream = await engine.jobs.streaming.stream(alice, JobRequest(app=APP))
    with pytest.raises(ValueError, match="app bug"):
        async with stream:
            raise ValueError("app bug")
    assert stream.job.state == "uncertain" and stream.job.attempts[0].outcome == "abandoned"
    assert transport.streams[0].closed and engine.admission.running == 0

    transport.opens = [StreamScript(events=[b"a", b"b"])]
    stream = await engine.jobs.streaming.stream(alice, JobRequest(app=APP))
    async with stream:
        assert [chunk async for chunk in stream] == [b"a", b"b"]
    assert stream.job.state == "succeeded"  # leaving the block after the end changes nothing


async def test_a_stream_nobody_reads_still_ends_at_timeout_s(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, transport: ScriptedStreamTransport
) -> None:
    engine = await provisioned(make_engine, admin)
    transport.opens = [StreamScript(events=[b"a", b"b"])]
    stream = await engine.jobs.streaming.stream(alice, JobRequest(app=APP, timeout_s=0.05))
    await ended(engine)
    assert stream.job.state == "uncertain" and stream.job.failure is not None
    assert stream.job.failure.kind == "timeout" and "timeout_s" in stream.job.failure.message
    assert transport.streams[0].closed and engine.admission.running == 0
    with pytest.raises(JobFailed):  # a late reader learns why it ended
        await read_all(stream)


async def test_engine_stop_drains_open_streams_then_abandons_the_rest(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, transport: ScriptedStreamTransport
) -> None:
    engine = await provisioned(make_engine, admin, stream_drain_timeout_s=0.3)
    transport.opens = [
        StreamScript(events=[b"a", 0.05, b"b"]),  # finishes inside the drain window
        StreamScript(events=[b"a", 5.0, b"b"]),  # does not
    ]
    quick = await engine.jobs.streaming.stream(alice, JobRequest(app=APP))
    slow = await engine.jobs.streaming.stream(alice, JobRequest(app=APP))
    reading = asyncio.create_task(read_all(quick))
    await engine.jobs.close_streams()
    assert await reading == b"ab" and quick.job.state == "succeeded"
    assert slow.job.state == "uncertain" and slow.job.failure is not None
    assert slow.job.failure.message == "the engine stopped while the stream was open"
    assert engine.admission.running == 0
    with pytest.raises(JobFailed):
        await read_all(slow)


async def test_engine_stop_waits_for_a_stream_that_is_still_recording_its_end(
    make_engine: EngineFactory,
    admin: ActorContext,
    alice: ActorContext,
    transport: ScriptedStreamTransport,
    store: SqlStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = await provisioned(make_engine, admin, stream_drain_timeout_s=0.05)
    transport.opens = [StreamScript(events=[b"a"])]
    stream = await engine.jobs.streaming.stream(alice, JobRequest(app=APP))
    finish_job = store.finish_job
    writing = asyncio.Event()

    async def slow_finish_job(*args, **kwargs):  # type: ignore[no-untyped-def]
        writing.set()
        await asyncio.sleep(0.2)  # longer than the drain window, within the finalize timeout
        return await finish_job(*args, **kwargs)

    monkeypatch.setattr(store, "finish_job", slow_finish_job)
    reading = asyncio.create_task(read_all(stream))
    await writing.wait()
    await engine.jobs.close_streams()
    stored = await store.get_job(stream.job.id)
    assert stored is not None and stored.state == "succeeded"  # written before the store can close
    assert await reading == b"a"


async def test_a_failed_write_after_open_closes_the_stream(
    make_engine: EngineFactory,
    admin: ActorContext,
    alice: ActorContext,
    transport: ScriptedStreamTransport,
    store: SqlStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = await provisioned(make_engine, admin)
    transport.opens = [StreamScript(events=[b"a"], manifest_ids=("m-5",))]
    finish_attempt = store.finish_attempt
    calls = 0

    async def fails_once(attempt):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("write failed")
        await finish_attempt(attempt)

    monkeypatch.setattr(store, "finish_attempt", fails_once)
    with pytest.raises(RuntimeError, match="write failed"):
        await engine.jobs.streaming.stream(alice, JobRequest(app=APP))
    assert transport.streams[0].closed and engine.admission.running == 0
    (job,) = await engine.jobs.list_jobs(alice)
    assert job.state == "uncertain"
    assert [s.manifest_id for s in job.attempts[0].sessions] == ["m-5"]  # cost sync can still match it


# -- D38: the caller has everything it needs --------------------------------------------------------


async def test_complete_records_success_with_the_bytes_received(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, transport: ScriptedStreamTransport
) -> None:
    engine = await provisioned(make_engine, admin)
    transport.opens = [StreamScript(events=[b"data: x\n\n", b"data: [DONE]\n\n", 5.0, b"trailing"])]
    stream = await engine.jobs.streaming.stream(alice, JobRequest(app=APP))
    async with stream:
        async for chunk in stream:
            if chunk == b"data: [DONE]\n\n":
                await stream.complete()
                break
    assert stream.job.state == "succeeded" and stream.job.result is not None
    assert stream.job.result.size_bytes == len(b"data: x\n\ndata: [DONE]\n\n")
    assert transport.streams[0].closed and engine.admission.running == 0
    await stream.complete()  # no-op once ended


async def test_complete_leaves_out_a_chunk_the_caller_never_read(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, transport: ScriptedStreamTransport
) -> None:
    engine = await provisioned(make_engine, admin)
    # no pause before "trailing": the engine has already read it ahead when the caller says done
    transport.opens = [StreamScript(events=[b"data: x\n\n", b"data: [DONE]\n\n", b"trailing"])]
    stream = await engine.jobs.streaming.stream(alice, JobRequest(app=APP))
    async with stream:
        async for chunk in stream:
            if chunk == b"data: [DONE]\n\n":
                await asyncio.sleep(0.02)  # the read-ahead lands in the queue
                await stream.complete()
                break
    delivered = b"data: x\n\ndata: [DONE]\n\n"
    assert stream.job.result is not None
    assert stream.job.result.size_bytes == len(delivered)
    assert stream.job.result.sha256 == hashlib.sha256(delivered).hexdigest()


async def test_a_cancelled_reader_succeeds_when_the_runner_ends_within_the_settle_window(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, transport: ScriptedStreamTransport
) -> None:
    engine = await provisioned(make_engine, admin, stream_settle_s=1.0)
    transport.opens = [StreamScript(events=[b"a", b"[DONE]", 0.05])]  # the runner closes 50 ms later
    stream = await engine.jobs.streaming.stream(alice, JobRequest(app=APP))
    seen = asyncio.Event()

    async def reader() -> None:
        async for chunk in stream:
            if chunk == b"[DONE]":
                seen.set()

    task = asyncio.create_task(reader())
    await seen.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await ended(engine)
    assert stream.job.state == "succeeded" and stream.job.attempts[0].outcome == "succeeded"
    assert stream.job.result is not None and stream.job.result.size_bytes == len(b"a[DONE]")


async def test_a_cancelled_reader_is_abandoned_when_the_runner_keeps_going(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, transport: ScriptedStreamTransport
) -> None:
    engine = await provisioned(make_engine, admin, stream_settle_s=0.05)
    transport.opens = [StreamScript(events=[b"a", 5.0, b"b"])]
    stream = await engine.jobs.streaming.stream(alice, JobRequest(app=APP))
    task = asyncio.create_task(read_all(stream))
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await ended(engine)
    assert stream.job.state == "uncertain" and stream.job.attempts[0].outcome == "abandoned"


async def test_dropping_the_iterator_abandons_at_once(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, transport: ScriptedStreamTransport
) -> None:
    engine = await provisioned(make_engine, admin, stream_settle_s=1.0)
    # the runner would close 50 ms later, inside the settle window
    transport.opens = [StreamScript(events=[b"data: x\n\n", b"data: [DONE]\n\n", b"trailing", 0.05])]
    stream = await engine.jobs.streaming.stream(alice, JobRequest(app=APP))
    async for chunk in stream:  # no async with, no complete(): the app just stops
        if chunk == b"data: [DONE]\n\n":
            await asyncio.sleep(0.01)  # the read-ahead lands in the queue
            break
    await ended(engine)
    assert stream.job.state == "uncertain" and stream.job.attempts[0].outcome == "abandoned"
    assert stream.job.result is None


async def test_disconnected_gives_the_runner_the_settle_window(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, transport: ScriptedStreamTransport
) -> None:
    engine = await provisioned(make_engine, admin, stream_settle_s=1.0)
    transport.opens = [StreamScript(events=[b"a", b"b", 0.05])]  # the runner closes 50 ms later
    stream = await engine.jobs.streaming.stream(alice, JobRequest(app=APP))
    async with stream:
        async for _chunk in stream:
            await stream.disconnected()  # an app's own HTTP layer: its client went away
            break
    assert stream.job.state == "succeeded" and stream.job.attempts[0].outcome == "succeeded"
    assert stream.job.result is not None and stream.job.result.size_bytes == len(b"ab")


@pytest.mark.parametrize("settle", [1.0, 0.0])
async def test_aclose_never_waits_for_the_runner(
    make_engine: EngineFactory,
    admin: ActorContext,
    alice: ActorContext,
    transport: ScriptedStreamTransport,
    settle: float,
) -> None:
    engine = await provisioned(make_engine, admin, stream_settle_s=settle)
    transport.opens = [StreamScript(events=[b"a", 0.05])]  # would end by itself 50 ms later
    stream = await engine.jobs.streaming.stream(alice, JobRequest(app=APP))
    loop = asyncio.get_running_loop()
    started = loop.time()
    await stream.aclose()
    assert loop.time() - started < 0.04  # Stop stops now, so spending stops now
    assert stream.job.state == "uncertain" and stream.job.attempts[0].outcome == "abandoned"
