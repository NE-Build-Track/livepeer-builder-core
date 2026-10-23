"""FakeEngine and the real engine honor the same actor-API contract (livepeer_builder.api.ActorApi).

Each test runs once against ``FakeEngine`` and once against a real engine from ``make_engine``, scripted
to the same outcome. When a case passes on both, application unit tests written against the fake mean
the same thing against the engine.
"""

import asyncio
import hashlib
import json
from collections.abc import AsyncIterable, AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest

from livepeer_builder import (
    ActorApi,
    ActorContext,
    EngineSupply,
    Job,
    JobFailed,
    JobRequest,
    JobStreamApi,
    Money,
    OperationConflict,
    OperationExists,
)
from livepeer_builder.contracts import AttemptOutcome, FailureKind, ProvisionRequest
from livepeer_builder.errors import NotFound, RunnerCallError
from livepeer_builder.testing import (
    Fails,
    FakeEngine,
    Reply,
    ScriptedStreamTransport,
    ScriptedTransport,
    StreamScript,
    make_engine,
    runner,
)

APP, OTHER, NONE = "example/echo", "example/other", "example/nobody"  # no runner serves NONE
ALICE = ActorContext(
    actor_id="alice", permissions=frozenset({"discover", "jobs.run", "jobs.read", "costs.read"})
)
ADMIN = ActorContext(actor_id="operator", credential_kind="admin")
RUNNERS = (runner(APP, url="https://r1"), runner(OTHER, url="https://r2"))

Side = Callable[..., AbstractAsyncContextManager[ActorApi]]
# A runner error as the engine's transport raises it (attempt outcome, paid), and as FakeEngine scripts it.
RunnerError = tuple[AttemptOutcome, bool]
FAILURE_KINDS: dict[str, FailureKind] = {
    "timeout": "timeout",
    "unreachable": "unreachable",
    "refused": "refused",
}


@asynccontextmanager
async def fake(
    *,
    fails: bool = False,
    chunks: tuple[bytes, ...] | None = None,
    data: dict[str, Any] | None = None,
    fails_mid_stream: bool = False,
    paid: bool = False,
    error: RunnerError | None = None,
) -> AsyncIterator[ActorApi]:
    cut = Fails(message="cut", status_code=None)  # a runner error after the first byte, no status of its own
    script: tuple[bytes | Fails, ...] = (*(chunks or ()), *((cut,) if fails_mid_stream else ()))
    failed = Fails(payment_sent=True, body="trace") if paid else Fails()
    if error is not None:
        failed = Fails(kind=FAILURE_KINDS[error[0]], status_code=None, payment_sent=error[1])
    reply: Reply | Fails = failed if fails or error else Reply(chunks=script, data=data)
    replies = {(APP, ""): reply, (NONE, ""): Fails(kind="no_offering", status_code=None)}
    yield FakeEngine(replies=replies, runners=RUNNERS, streaming=chunks is not None)


@asynccontextmanager
async def real(
    *,
    fails: bool = False,
    chunks: tuple[bytes, ...] | None = None,
    data: dict[str, Any] | None = None,
    fails_mid_stream: bool = False,
    paid: bool = False,
    error: RunnerError | None = None,
) -> AsyncIterator[ActorApi]:
    transport: ScriptedTransport
    if error is not None:
        transport = ScriptedTransport([RunnerCallError(error[0], "boom", payment_sent=error[1])])
    elif chunks is not None:
        broken = [RunnerCallError("http", "cut", payment_sent=True)] if fails_mid_stream else []
        transport = ScriptedStreamTransport(opens=[StreamScript(events=[*chunks, *broken])])
    elif fails:
        error = RunnerCallError(
            "http", "boom", payment_sent=paid, status_code=500, body="trace" if paid else None
        )
        transport = ScriptedTransport([error])
    elif data is not None:  # the SDK parsed the body: data is set and content is its JSON
        body = json.dumps(data).encode()
        transport = ScriptedTransport([ScriptedTransport.ok(body).model_copy(update={"data": data})])
    else:
        transport = ScriptedTransport([ScriptedTransport.ok(), ScriptedTransport.ok()])
    async with make_engine(runners=RUNNERS, supply=EngineSupply(transport=transport)) as engine:
        amount = Money(amount=Decimal("5"), currency="USD")
        request = ProvisionRequest(
            application_id="default", actor_id="alice", amount=amount, grant_id="grant_1"
        )
        await engine.payments.provision(ADMIN, request)
        yield engine


async def collect(stream: AsyncIterable[bytes], into: list[bytes]) -> None:
    async for chunk in stream:
        into.append(chunk)


@pytest.fixture(params=[fake, real], ids=["fake", "real"])
def side(request: pytest.FixtureRequest) -> Side:
    return request.param  # type: ignore[no-any-return]


async def test_run_returns_a_succeeded_result(side: Side) -> None:
    async with side() as engine:
        result = await engine.jobs.run(ALICE, JobRequest(app=APP))
    assert result.job.state == "succeeded" and result.status_code == 200


async def test_a_failed_run_raises_job_failed(side: Side) -> None:
    async with side(fails=True) as engine:
        with pytest.raises(JobFailed) as failed:
            await engine.jobs.run(ALICE, JobRequest(app=APP))
    assert failed.value.job.state == "failed" and failed.value.job.failure is not None


async def test_get_and_list_return_the_job(side: Side) -> None:
    async with side() as engine:
        job = (await engine.jobs.run(ALICE, JobRequest(app=APP))).job
        assert (await engine.jobs.get(ALICE, job.id)).id == job.id
        assert [listed.id for listed in await engine.jobs.list_jobs(ALICE)] == [job.id]
        with pytest.raises(NotFound):
            await engine.jobs.get(ALICE, uuid4())


async def test_cost_of_an_unknown_job_is_not_found(side: Side) -> None:
    async with side() as engine:
        with pytest.raises(NotFound):
            await engine.costs.for_job(ALICE, uuid4())


async def test_candidates_are_filtered_by_app(side: Side) -> None:
    async with side() as engine:
        assert [r.url for r in engine.discovery.candidates(APP)] == ["https://r1"]


async def test_streaming_is_absent_without_a_streaming_transport(side: Side) -> None:
    async with side() as engine:
        assert engine.jobs.streaming is None


async def test_streaming_yields_the_chunks(side: Side) -> None:
    async with side(chunks=(b"a", b"b")) as engine:
        streaming = engine.jobs.streaming
        assert streaming is not None
        stream = await streaming.stream(ALICE, JobRequest(app=APP))
        received = [chunk async for chunk in stream]
        await stream.aclose()
    assert received == [b"a", b"b"] and stream.status_code == 200


async def test_health_is_ready_after_a_discovery_snapshot(side: Side) -> None:
    async with side() as engine:
        assert engine.health().ready


async def test_run_returns_the_parsed_body_as_data(side: Side) -> None:
    async with side(data={"answer": 42}) as engine:
        result = await engine.jobs.run(ALICE, JobRequest(app=APP))
    assert result.data == {"answer": 42}


async def test_a_run_records_the_attempt_on_the_runner_it_used(side: Side) -> None:
    async with side() as engine:
        attempt = (await engine.jobs.run(ALICE, JobRequest(app=APP))).job.attempts[-1]
    assert (attempt.runner_url, attempt.orchestrator_url) == ("https://r1", "https://orch.example")
    assert attempt.outcome == "succeeded" and attempt.payment_sent and attempt.status_code == 200


async def test_a_succeeded_run_records_a_result_of_the_body(side: Side) -> None:
    async with side() as engine:
        result = await engine.jobs.run(ALICE, JobRequest(app=APP))
        stored = await engine.jobs.get(ALICE, result.job.id)
    for job in (result.job, stored):
        assert job.result is not None
        assert (job.result.size_bytes, job.result.sha256) == (
            len(result.content),
            hashlib.sha256(result.content).hexdigest(),
        )


async def test_a_failure_mid_stream_raises_job_failed_after_the_earlier_chunks(side: Side) -> None:
    async with side(chunks=(b"a",), fails_mid_stream=True) as engine:
        streaming = engine.jobs.streaming
        assert streaming is not None
        stream = await streaming.stream(ALICE, JobRequest(app=APP))
        received: list[bytes] = []
        with pytest.raises(JobFailed) as failed:
            await collect(stream, received)
        await stream.aclose()
    assert received == [b"a"]
    job = failed.value.job  # paid before it broke, so the cost is uncertain until usage settles it
    assert job.state == "uncertain" and job.failure is not None and job.failure.payment_sent
    assert (job.failure.kind, job.failure.status_code) == ("runner_error", None)
    assert (job.attempts[-1].outcome, job.attempts[-1].status_code) == ("http", 200)  # the stream's status


async def test_a_repeated_operation_ref_raises_operation_exists_or_conflict(side: Side) -> None:
    async with side() as engine:
        first = (await engine.jobs.run(ALICE, JobRequest(app=APP, operation_ref="op-1"))).job
        with pytest.raises(OperationExists) as exists:
            await engine.jobs.run(ALICE, JobRequest(app=APP, operation_ref="op-1"))
        with pytest.raises(OperationConflict) as conflict:
            await engine.jobs.run(ALICE, JobRequest(app=APP, path="/other", operation_ref="op-1"))
    assert exists.value.job.id == first.id and conflict.value.job.id == first.id


async def test_a_paid_runner_error_fails_the_job_with_the_runner_s_words(side: Side) -> None:
    async with side(fails=True, paid=True) as engine:
        with pytest.raises(JobFailed) as failed:
            await engine.jobs.run(ALICE, JobRequest(app=APP))
    job = failed.value.job  # paid, so no failover: the job fails on this runner
    assert job.state == "failed" and job.failure is not None
    assert (job.failure.payment_sent, job.failure.body, job.failure.status_code) == (True, "trace", 500)
    assert job.attempts[-1].payment_sent and job.attempts[-1].outcome == "http"


async def test_reading_a_stream_to_the_end_succeeds_with_a_result_of_the_bytes_read(side: Side) -> None:
    async with side(chunks=(b"a", b"bc")) as engine:
        streaming = engine.jobs.streaming
        assert streaming is not None
        async with await streaming.stream(ALICE, JobRequest(app=APP)) as stream:
            received = [chunk async for chunk in stream]
        job = await engine.jobs.get(ALICE, stream.job.id)
    assert received == [b"a", b"bc"] and stream.finished and stream.job.state == "succeeded"
    assert job.state == "succeeded" and job.result is not None
    assert (job.result.size_bytes, job.result.sha256) == (3, hashlib.sha256(b"abc").hexdigest())
    assert (job.attempts[-1].outcome, job.attempts[-1].status_code) == ("succeeded", 200)


async def test_complete_succeeds_with_the_bytes_read_so_far(side: Side) -> None:
    async with side(chunks=(b"a", b"bc")) as engine:
        streaming = engine.jobs.streaming
        assert streaming is not None
        async with await streaming.stream(ALICE, JobRequest(app=APP)) as stream:
            chunks = aiter(stream)
            assert await anext(chunks) == b"a"
            await stream.complete()  # the caller has what it needs (D38)
        job = await engine.jobs.get(ALICE, stream.job.id)
    assert job.state == "succeeded" and job.result is not None and job.result.size_bytes == 1


async def test_closing_a_paid_stream_early_abandons_it(side: Side) -> None:
    async with side(chunks=(b"a", b"bc")) as engine:
        streaming = engine.jobs.streaming
        assert streaming is not None
        stream = await streaming.stream(ALICE, JobRequest(app=APP))
        chunks = aiter(stream)
        assert await anext(chunks) == b"a"
        await stream.aclose()
        await stream.complete()  # too late: the stream already ended
        job = await engine.jobs.get(ALICE, stream.job.id)
    assert stream.finished and job.state == "uncertain" and job.result is None  # paid, so not "failed"
    assert job.failure is not None and (job.failure.kind, job.failure.payment_sent) == ("abandoned", True)
    assert job.attempts[-1].outcome == "abandoned"


async def test_usage_can_be_reported_for_a_finished_stream(side: Side) -> None:
    async with side(chunks=(b"a",)) as engine:
        streaming = engine.jobs.streaming
        assert streaming is not None
        async with await streaming.stream(ALICE, JobRequest(app=APP)) as stream:
            _ = [chunk async for chunk in stream]
        await engine.jobs.report_usage(ALICE, stream.job.id, {"prompt_tokens": 1})


@pytest.mark.parametrize(
    ("error", "state"),
    [
        (("timeout", True), "uncertain"),  # paid, and the runner may have run: the cost is not known
        (("unreachable", True), "uncertain"),
        (("unreachable", False), "failed"),  # nothing paid; with one runner there is nowhere to fail over
        (("refused", False), "failed"),
    ],
)
async def test_a_runner_error_records_its_kind_and_settles_the_job(
    side: Side, error: RunnerError, state: str
) -> None:
    async with side(error=error) as engine:
        with pytest.raises(JobFailed) as failed:
            await engine.jobs.run(ALICE, JobRequest(app=APP))
    job = failed.value.job
    assert job.state == state and job.failure is not None and job.failure.kind == error[0]
    assert (job.attempts[-1].outcome, job.attempts[-1].payment_sent) == error


async def test_no_offering_fails_without_an_attempt(side: Side) -> None:
    async with side() as engine:
        with pytest.raises(JobFailed) as failed:
            await engine.jobs.run(ALICE, JobRequest(app=NONE))
    job = failed.value.job
    assert job.state == "failed" and job.failure is not None and job.failure.kind == "no_offering"
    assert job.attempts == ()


async def test_a_cancelled_stream_lets_the_runner_finish_within_the_settle_window(side: Side) -> None:
    # A cancel (a task timeout, a server shutting the request down) is not the caller choosing to stop:
    # the engine waits LPB_STREAM_SETTLE_S for the runner, and a runner that finishes in time succeeds
    # (D38, D40). The scripted runner here has nothing left to wait for, so it finishes.
    async with side(chunks=(b"a", b"bc")) as engine:
        streaming = engine.jobs.streaming
        assert streaming is not None
        read_one = asyncio.Event()
        opened: list[JobStreamApi] = []

        async def reader() -> None:
            async with await streaming.stream(ALICE, JobRequest(app=APP)) as stream:
                opened.append(stream)
                chunks = aiter(stream)  # held: a dropped iterator is the caller stopping (D40)
                assert await anext(chunks) == b"a"
                read_one.set()
                await asyncio.Event().wait()  # until cancelled

        task = asyncio.create_task(reader())
        await read_one.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        job = await ended(engine, opened[0].job.id)
    assert job.state == "succeeded" and job.result is not None and job.result.size_bytes == 3


async def ended(engine: ActorApi, job_id: UUID, within_s: float = 5.0) -> Job:
    """The job once its stream's ending is written (the engine writes it in a shielded task)."""
    async with asyncio.timeout(within_s):
        # The facade exposes no event for "ending written", so this polls under a deadline.
        while (job := await engine.jobs.get(ALICE, job_id)).state == "running":  # noqa: ASYNC110
            await asyncio.sleep(0.02)
    return job
