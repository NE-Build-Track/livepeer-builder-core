"""FakeEngine and the real engine honor the same journey contract (livepeer_builder.api.JourneyApi).

Each test runs once against ``FakeEngine`` and once against a real engine from ``make_engine``, scripted
to the same outcome. When a case passes on both, application unit tests written against the fake mean
the same thing against the engine.
"""

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from decimal import Decimal
from uuid import uuid4

import pytest

from livepeer_builder import ActorContext, EngineSupply, JobFailed, JobRequest, JourneyApi, Money
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

Side = Callable[..., AbstractAsyncContextManager[JourneyApi]]


@asynccontextmanager
async def fake(*, fails: bool = False, chunks: tuple[bytes, ...] | None = None) -> AsyncIterator[JourneyApi]:
    reply: Reply | Fails = Fails() if fails else Reply(chunks=chunks or ())
    yield FakeEngine(replies={(APP, ""): reply}, runners=RUNNERS, streaming=chunks is not None)


@asynccontextmanager
async def real(*, fails: bool = False, chunks: tuple[bytes, ...] | None = None) -> AsyncIterator[JourneyApi]:
    if chunks is not None:
        transport: ScriptedTransport = ScriptedStreamTransport(opens=[StreamScript(events=list(chunks))])
    elif fails:
        transport = ScriptedTransport([RunnerCallError("http", "boom", payment_sent=False, status_code=500)])
    else:
        transport = ScriptedTransport([ScriptedTransport.ok()])
    async with make_engine(runners=RUNNERS, supply=EngineSupply(transport=transport)) as engine:
        amount = Money(amount=Decimal("5"), currency="USD")
        request = ProvisionRequest(
            application_id="default", actor_id="alice", amount=amount, grant_id="grant_1"
        )
        await engine.payments.provision(ADMIN, request)
        yield engine


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
