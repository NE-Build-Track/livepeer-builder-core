"""``return_http_errors`` keeps failover (D52, D55): an answer another runner could do better fails over as
it does for a default call, and only the answer the engine stops at reaches the caller unchanged."""

from typing import Any

import pytest

from livepeer_builder import ActorContext, EngineSupply, JobRequest
from livepeer_builder.contracts import Runner
from livepeer_builder.runtime import JobStream
from livepeer_builder.testing import (
    FakeEngine,
    Reply,
    ScriptedStreamTransport,
    StreamScript,
    make_engine,
    runner,
)

ACTOR = ActorContext(actor_id="a", application_id="app", permissions=frozenset({"jobs.run", "jobs.read"}))
RUNNERS = [
    runner("chat", "https://runner-a.example", orchestrator_url="https://orch-a.example"),
    runner("chat", "https://runner-b.example", orchestrator_url="https://orch-b.example"),
]

REQUEST = JobRequest(app="chat", payload={"a": 1}, return_http_errors=True)


def answer(status: int, body: bytes) -> StreamScript:
    return StreamScript(events=[body], status_code=status)


async def returned(
    transport: ScriptedStreamTransport,
    runners: list[Runner] = RUNNERS,
    max_attempts: int = 2,
    **settings: Any,
) -> tuple[JobStream, bytes]:
    """The stream the caller gets, read to its end."""
    async with make_engine(
        runners=runners,
        supply=EngineSupply(transport=transport),
        allocation_mode="per_application",
        shared_payment_key="fixture",
        shared_allocation_id="fixture",
        **settings,
    ) as engine:
        streaming = engine.jobs.streaming
        assert streaming is not None
        request = REQUEST.model_copy(update={"max_attempts": max_attempts})
        stream = await streaming.stream(ACTOR, request)
        async with stream:
            body = b"".join([chunk async for chunk in stream])
        return stream, body


def outcomes(stream: JobStream) -> list[tuple[str, int | None]]:
    return [(attempt.outcome, attempt.status_code) for attempt in stream.job.attempts]


async def test_a_busy_runner_fails_over_and_the_next_answer_is_returned() -> None:
    transport = ScriptedStreamTransport(opens=[answer(503, b"busy"), answer(200, b"ok")])
    stream, body = await returned(transport)
    assert (stream.status_code, body, stream.job.state) == (200, b"ok", "succeeded")
    assert outcomes(stream) == [("refused", 503), ("succeeded", 200)]


async def test_the_answer_the_engine_stops_at_comes_back_unchanged() -> None:
    transport = ScriptedStreamTransport(opens=[answer(503, b"busy"), answer(404, b"no such model")])
    stream, body = await returned(transport)
    assert (stream.status_code, body) == (404, b"no such model")
    assert stream.job.state == "failed" and stream.job.failure is not None
    assert stream.job.failure.status_code == 404
    assert outcomes(stream) == [("refused", 503), ("http", 404)]


async def test_an_application_error_comes_back_without_failover() -> None:
    transport = ScriptedStreamTransport(opens=[answer(400, b"bad request"), answer(200, b"ok")])
    stream, body = await returned(transport)
    assert (stream.status_code, body, stream.job.state) == (400, b"bad request", "failed")
    assert len(transport.calls) == 1


@pytest.mark.parametrize("status", [502, 504])
async def test_a_paid_gateway_error_comes_back_and_leaves_the_job_uncertain(status: int) -> None:
    """The app may still be running behind a lost proxy, as for a default call (D52)."""
    transport = ScriptedStreamTransport(opens=[answer(status, b"gateway"), answer(200, b"ok")])
    stream, body = await returned(transport)
    assert (stream.status_code, body, stream.job.state) == (status, b"gateway", "uncertain")
    assert len(transport.calls) == 1


async def test_the_paid_attempt_cap_returns_the_answer_it_stopped_at() -> None:
    transport = ScriptedStreamTransport(opens=[answer(503, b"busy"), answer(200, b"ok")])
    stream, body = await returned(transport, max_paid_attempts=1)
    assert (stream.status_code, body, stream.job.state) == (503, b"busy", "failed")
    assert len(transport.calls) == 1


async def test_a_paid_answer_comes_back_when_only_its_orchestrator_is_left() -> None:
    """A paid failover never pays the same orchestrator again (D52), so its second runner is no option."""
    same = [
        RUNNERS[0],
        runner("chat", "https://runner-a2.example", orchestrator_url="https://orch-a.example"),
    ]
    transport = ScriptedStreamTransport(opens=[answer(503, b"busy"), answer(200, b"ok")])
    stream, body = await returned(transport, runners=same)
    assert (stream.status_code, body, stream.job.state) == (503, b"busy", "failed")
    assert len(transport.calls) == 1


async def test_the_answer_comes_back_when_the_caller_allows_one_attempt() -> None:
    transport = ScriptedStreamTransport(opens=[answer(503, b"busy"), answer(200, b"ok")])
    stream, body = await returned(transport, max_attempts=1)
    assert (stream.status_code, body) == (503, b"busy")
    assert len(transport.calls) == 1


@pytest.mark.parametrize("status", [307, 400, 404, 500, 502, 503, 504])
async def test_the_fake_engine_ends_a_returned_error_as_the_engine_does(status: int) -> None:
    """One runner: nothing to fail over to, so both return the answer and end the job alike."""
    transport = ScriptedStreamTransport(opens=[answer(status, b"error")])
    async with make_engine(
        runners=RUNNERS[:1],
        supply=EngineSupply(transport=transport),
        allocation_mode="per_application",
        shared_payment_key="fixture",
        shared_allocation_id="fixture",
    ) as engine:
        assert engine.jobs.streaming is not None
        real = await engine.jobs.streaming.stream(ACTOR, REQUEST)
        async with real:
            assert b"".join([chunk async for chunk in real]) == b"error"
    fake = FakeEngine(
        streaming=True,
        runners=RUNNERS[:1],
        replies={("chat", ""): Reply(status_code=status, chunks=(b"error",))},
    )
    assert fake.jobs.streaming is not None
    scripted = await fake.jobs.streaming.stream(ACTOR, REQUEST)
    assert b"".join([chunk async for chunk in scripted]) == b"error"
    await scripted.aclose()
    ended = []
    for job in (real.job, scripted.job):
        assert job.failure is not None
        ended.append((job.state, job.failure.kind, job.attempts[-1].outcome))
    assert ended[0] == ended[1]
