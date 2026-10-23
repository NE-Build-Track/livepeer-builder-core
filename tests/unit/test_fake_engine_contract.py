"""FakeEngine and the real engine honor the same actor-API contract (livepeer_builder.api.ActorApi).

Each test runs once against ``FakeEngine`` and once against a real engine from ``make_engine``, scripted
to the same outcome. When a case passes on both, application unit tests written against the fake mean
the same thing against the engine.
"""

import json
from collections.abc import AsyncIterable, AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

from livepeer_builder import (
    ActorApi,
    ActorContext,
    EngineSupply,
    JobFailed,
    JobRequest,
    Money,
    OperationConflict,
    OperationExists,
)
from livepeer_builder.contracts import ProvisionRequest
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

APP, OTHER = "example/echo", "example/other"
ALICE = ActorContext(
    actor_id="alice", permissions=frozenset({"discover", "jobs.run", "jobs.read", "costs.read"})
)
ADMIN = ActorContext(actor_id="operator", credential_kind="admin")
RUNNERS = (runner(APP, url="https://r1"), runner(OTHER, url="https://r2"))

Side = Callable[..., AbstractAsyncContextManager[ActorApi]]


@asynccontextmanager
async def fake(
    *,
    fails: bool = False,
    chunks: tuple[bytes, ...] | None = None,
    data: dict[str, Any] | None = None,
    fails_mid_stream: bool = False,
    paid: bool = False,
) -> AsyncIterator[ActorApi]:
    script: tuple[bytes | Fails, ...] = (*(chunks or ()), *((Fails(),) if fails_mid_stream else ()))
    failed = Fails(payment_sent=True, body="trace") if paid else Fails()
    reply: Reply | Fails = failed if fails else Reply(chunks=script, data=data)
    yield FakeEngine(replies={(APP, ""): reply}, runners=RUNNERS, streaming=chunks is not None)


@asynccontextmanager
async def real(
    *,
    fails: bool = False,
    chunks: tuple[bytes, ...] | None = None,
    data: dict[str, Any] | None = None,
    fails_mid_stream: bool = False,
    paid: bool = False,
) -> AsyncIterator[ActorApi]:
    if chunks is not None:
        broken = [RunnerCallError("http", "cut", payment_sent=True)] if fails_mid_stream else []
        transport: ScriptedTransport = ScriptedStreamTransport(
            opens=[StreamScript(events=[*chunks, *broken])]
        )
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
    assert attempt.outcome == "succeeded" and attempt.payment_sent


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
    assert job.attempts[-1].payment_sent
