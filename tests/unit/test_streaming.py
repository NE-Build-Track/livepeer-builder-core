"""Streaming jobs (docs/contracts/engine.md#streaming): open, deliver, finish, fail, time out, abandon."""

import asyncio
import hashlib
from decimal import Decimal

import httpx
import pytest

from livepeer_builder import EngineSupply
from livepeer_builder.adapters.static import StaticDiscoverySource
from livepeer_builder.contracts import ActorContext, JobRequest, Money, ProvisionRequest
from livepeer_builder.errors import JobFailed, OperationExists, OperationInProgress, RunnerCallError
from livepeer_builder.http import create_app
from livepeer_builder.testing import ScriptedStreamTransport, ScriptedTransport, StreamScript, runner
from tests.conftest import EngineFactory

APP = "example/chat"
CHUNKS = [b"data: a\n\n", b"data: b\n\n", b"data: [DONE]\n\n"]


async def provisioned(make_engine: EngineFactory, admin: ActorContext, **settings: object):  # type: ignore[no-untyped-def]
    engine = await make_engine(
        runners=(runner(APP, url="https://r1"), runner(APP, url="https://r2")), **settings
    )
    request = ProvisionRequest(
        application_id="default",
        actor_id="alice",
        amount=Money(amount=Decimal(5), currency="USD"),
        grant_id="g",
    )
    await engine.payments.provision(admin, request)
    return engine


async def read_all(stream) -> bytes:  # type: ignore[no-untyped-def]
    return b"".join([chunk async for chunk in stream])


async def collect(stream, into: list[bytes]) -> None:  # type: ignore[no-untyped-def]
    async for chunk in stream:
        into.append(chunk)


async def test_stream_delivers_chunks_then_records_a_succeeded_job(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, transport: ScriptedStreamTransport
) -> None:
    engine = await provisioned(make_engine, admin)
    transport.opens = [StreamScript(events=list(CHUNKS), manifest_ids=("m-1",))]
    stream = await engine.jobs.streaming.stream(alice, JobRequest(app=APP, path="/v1/chat/completions"))
    assert stream.job.state == "running" and stream.status_code == 200
    assert stream.content_type == "text/event-stream"
    assert engine.admission.running == 1  # the slot is held while the stream is open
    body = await read_all(stream)
    assert body == b"".join(CHUNKS)
    assert stream.finished and stream.job.state == "succeeded"
    assert engine.admission.running == 0
    stored = await engine.jobs.get(alice, stream.job.id)
    assert stored.state == "succeeded" and stored.result is not None
    assert stored.result.size_bytes == len(body) and stored.result.sha256 == hashlib.sha256(body).hexdigest()
    assert stored.result.content_type == "text/event-stream"
    attempt = stored.attempts[0]
    assert attempt.outcome == "succeeded" and attempt.payment_sent and attempt.ended_at is not None
    assert [s.manifest_id for s in attempt.sessions] == ["m-1"]
    assert transport.streams[0].closed  # the connection was released


async def test_payment_evidence_is_written_before_the_first_byte(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, transport: ScriptedStreamTransport
) -> None:
    engine = await provisioned(make_engine, admin)
    transport.opens = [StreamScript(events=[b"x"], manifest_ids=("m-7",))]
    stream = await engine.jobs.streaming.stream(alice, JobRequest(app=APP))
    running = await engine.jobs.get(alice, stream.job.id)
    attempt = running.attempts[0]
    assert attempt.outcome == "in_flight" and attempt.payment_sent
    assert [s.manifest_id for s in attempt.sessions] == ["m-7"]
    # a restart now would resolve it as uncertain, because payment went out
    await read_all(stream)


async def test_open_fails_over_before_the_first_byte(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, transport: ScriptedStreamTransport
) -> None:
    engine = await provisioned(make_engine, admin)
    transport.opens = [RunnerCallError("refused", "503", payment_sent=False), StreamScript(events=[b"ok"])]
    stream = await engine.jobs.streaming.stream(alice, JobRequest(app=APP))
    await read_all(stream)
    assert [a.outcome for a in stream.job.attempts] == ["refused", "succeeded"]

    transport.opens = [
        RunnerCallError("http", "500", payment_sent=True, manifest_ids=("m-9",), status_code=500)
    ]
    with pytest.raises(JobFailed) as failed:
        await engine.jobs.streaming.stream(alice, JobRequest(app=APP))
    # r2 shares the orchestrator that took the payment, so it is not paid again
    assert failed.value.job.state == "failed" and len(failed.value.job.attempts) == 1
    assert engine.admission.running == 0  # released when open fails


async def test_a_paid_refusal_at_open_fails_over_to_another_orchestrator(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, transport: ScriptedStreamTransport
) -> None:
    engine = await make_engine(
        runners=(
            runner(APP, url="https://a1", orchestrator_url="https://orch-a.example"),
            runner(APP, url="https://b1", orchestrator_url="https://orch-b.example"),
        )
    )
    provision = ProvisionRequest(
        application_id="default",
        actor_id="alice",
        amount=Money(amount=Decimal(5), currency="USD"),
        grant_id="g",
    )
    await engine.payments.provision(admin, provision)
    transport.opens = [
        RunnerCallError("refused", "no capacity", payment_sent=True, manifest_ids=("m-1",), status_code=503),
        StreamScript(events=[b"ok"], manifest_ids=("m-2",)),
    ]
    stream = await engine.jobs.streaming.stream(alice, JobRequest(app=APP))
    assert await read_all(stream) == b"ok"
    job = await engine.jobs.get(alice, stream.job.id)
    assert job.state == "succeeded"
    assert [(a.outcome, a.payment_sent, a.runner_url) for a in job.attempts] == [
        ("refused", True, "https://a1"),
        ("succeeded", True, "https://b1"),
    ]


async def test_runner_failure_mid_stream_ends_the_job_uncertain(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, transport: ScriptedStreamTransport
) -> None:
    engine = await provisioned(make_engine, admin)
    boom = RunnerCallError("unreachable", "connection reset", payment_sent=True, manifest_ids=("m-1",))
    transport.opens = [StreamScript(events=[b"partial ", boom])]
    stream = await engine.jobs.streaming.stream(alice, JobRequest(app=APP))
    received: list[bytes] = []
    with pytest.raises(JobFailed):
        await collect(stream, received)
    assert received == [b"partial "]
    job = stream.job
    assert job.state == "uncertain" and job.failure is not None
    assert job.failure.kind == "unreachable" and job.failure.payment_sent
    assert job.attempts[0].outcome == "unreachable"
    assert engine.admission.running == 0


async def test_runner_5xx_mid_stream_is_a_runner_error(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, transport: ScriptedStreamTransport
) -> None:
    engine = await provisioned(make_engine, admin)
    error = RunnerCallError("http", "bad trailer", payment_sent=True, status_code=502, body="upstream")
    transport.opens = [StreamScript(events=[b"a", error])]
    stream = await engine.jobs.streaming.stream(alice, JobRequest(app=APP))
    with pytest.raises(JobFailed):
        await read_all(stream)
    assert stream.job.failure is not None and stream.job.failure.kind == "runner_error"
    assert stream.job.failure.body == "upstream" and stream.job.attempts[0].status_code == 502


async def test_idle_and_total_timeouts_end_the_job_uncertain(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, transport: ScriptedStreamTransport
) -> None:
    engine = await provisioned(make_engine, admin)
    transport.opens = [StreamScript(events=[b"a", 0.2, b"never"])]
    stream = await engine.jobs.streaming.stream(alice, JobRequest(app=APP, idle_timeout_s=0.02))
    with pytest.raises(JobFailed):
        await read_all(stream)
    assert stream.job.state == "uncertain" and stream.job.failure is not None
    assert stream.job.failure.kind == "timeout" and stream.job.attempts[0].outcome == "timeout"
    assert transport.streams[0].closed

    transport.opens = [StreamScript(events=[b"a", 0.03, b"b", 0.03, b"c", 0.03, b"d"])]
    stream = await engine.jobs.streaming.stream(alice, JobRequest(app=APP, timeout_s=0.05))
    with pytest.raises(JobFailed):
        await read_all(stream)
    assert stream.job.failure is not None and stream.job.failure.kind == "timeout"
    assert engine.admission.running == 0


async def test_aclose_records_an_abandoned_attempt(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, transport: ScriptedStreamTransport
) -> None:
    engine = await provisioned(make_engine, admin)
    transport.opens = [StreamScript(events=[b"a", b"b", b"c"])]
    stream = await engine.jobs.streaming.stream(alice, JobRequest(app=APP))
    first = await stream.__aiter__().__anext__()
    assert first == b"a"
    await stream.aclose()
    job = stream.job
    assert job.state == "uncertain" and job.failure is not None and job.failure.kind == "abandoned"
    assert job.attempts[0].outcome == "abandoned" and job.attempts[0].payment_sent
    assert transport.streams[0].closed and engine.admission.running == 0
    await stream.aclose()  # idempotent
    assert [chunk async for chunk in stream] == []  # nothing more after the end


async def test_cancelling_the_consumer_abandons_the_stream(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, transport: ScriptedStreamTransport
) -> None:
    engine = await provisioned(make_engine, admin, stream_settle_s=0)
    transport.opens = [StreamScript(events=[b"a", 5.0, b"b"])]
    stream = await engine.jobs.streaming.stream(alice, JobRequest(app=APP))

    async def consume() -> None:
        async for _ in stream:
            pass

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stream.job.state == "uncertain" and stream.job.attempts[0].outcome == "abandoned"
    assert engine.admission.running == 0


async def test_a_stream_obeys_the_paid_attempt_cap(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, transport: ScriptedStreamTransport
) -> None:
    engine = await make_engine(
        runners=(
            runner(APP, url="https://a1", orchestrator_url="https://orch-a.example"),
            runner(APP, url="https://b1", orchestrator_url="https://orch-b.example"),
        ),
        max_paid_attempts=1,
    )
    provision = ProvisionRequest(
        application_id="default",
        actor_id="alice",
        amount=Money(amount=Decimal(5), currency="USD"),
        grant_id="g",
    )
    await engine.payments.provision(admin, provision)
    transport.opens = [
        RunnerCallError("refused", "no capacity", payment_sent=True, manifest_ids=("m-1",), status_code=503),
        StreamScript(events=[b"ok"], manifest_ids=("m-2",)),
    ]
    assert engine.jobs.streaming is not None
    with pytest.raises(JobFailed) as failed:
        await engine.jobs.streaming.stream(alice, JobRequest(app=APP))
    assert failed.value.job.state == "failed" and len(failed.value.job.attempts) == 1
    assert engine.admission.running == 0


async def test_unpaid_stream_failure_is_failed_not_uncertain(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, transport: ScriptedStreamTransport
) -> None:
    engine = await provisioned(make_engine, admin)
    error = RunnerCallError("unreachable", "reset", payment_sent=False)
    transport.opens = [StreamScript(events=[b"a", error], manifest_ids=(), payment_sent=False)]
    stream = await engine.jobs.streaming.stream(alice, JobRequest(app=APP))
    with pytest.raises(JobFailed):
        await read_all(stream)
    assert stream.job.state == "failed"


async def test_a_stream_failure_after_a_paid_failover_says_a_payment_went_out(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, transport: ScriptedStreamTransport
) -> None:
    """The stream that broke was unpaid, so the job is ``failed``, but the runner it failed over from was
    paid: ``failure.payment_sent`` covers every attempt, as ``failure_of`` does (Cursor, #55)."""
    engine = await make_engine(
        runners=(
            runner(APP, url="https://a1", orchestrator_url="https://orch-a.example"),
            runner(APP, url="https://b1", orchestrator_url="https://orch-b.example"),
        )
    )
    provision = ProvisionRequest(
        application_id="default",
        actor_id="alice",
        amount=Money(amount=Decimal(5), currency="USD"),
        grant_id="g",
    )
    await engine.payments.provision(admin, provision)
    transport.opens = [
        RunnerCallError("refused", "no capacity", payment_sent=True, manifest_ids=("m-1",), status_code=503),
        StreamScript(
            events=[b"a", RunnerCallError("unreachable", "reset", payment_sent=False)],
            manifest_ids=(),
            payment_sent=False,
        ),
    ]
    stream = await engine.jobs.streaming.stream(alice, JobRequest(app=APP))
    with pytest.raises(JobFailed):
        await read_all(stream)
    assert stream.job.state == "failed" and stream.job.failure is not None
    assert stream.job.failure.payment_sent
    assert [a.payment_sent for a in stream.job.attempts] == [True, False]


async def test_stream_keeps_idempotency_and_needs_a_streaming_transport(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, transport: ScriptedStreamTransport
) -> None:
    engine = await provisioned(make_engine, admin)
    transport.opens = [StreamScript(events=[b"a"])]
    stream = await engine.jobs.streaming.stream(alice, JobRequest(app=APP, operation_ref="op-1"))
    await read_all(stream)
    with pytest.raises(OperationExists):
        await engine.jobs.streaming.stream(alice, JobRequest(app=APP, operation_ref="op-1"))
    plain = await provisioned(make_engine, admin, supply=EngineSupply(transport=ScriptedTransport()))
    assert plain.jobs.streaming is None  # a transport without open() offers no streaming (D33)


async def test_a_streamed_repeat_follows_the_same_rules_and_never_streams_again(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, transport: ScriptedStreamTransport
) -> None:
    """D57: while the stream is open the job runs, so a repeat is in progress; once it ended it ran, so a
    repeat is OperationExists. Neither opens the runner again: a replay never re-streams."""
    engine = await provisioned(make_engine, admin)
    transport.opens = [StreamScript(events=[b"a"])]
    request = JobRequest(app=APP, operation_ref="op-1")
    stream = await engine.jobs.streaming.stream(alice, request)
    with pytest.raises(OperationInProgress) as running:
        await engine.jobs.streaming.stream(alice, request)
    assert running.value.job.id == stream.job.id
    with pytest.raises(OperationInProgress):
        await engine.jobs.run(alice, request)  # one reference, either surface
    await read_all(stream)
    with pytest.raises(OperationExists) as ran:
        await engine.jobs.streaming.stream(alice, request)
    assert type(ran.value) is OperationExists and ran.value.job.state == "succeeded"
    assert len(transport.calls) == 1


async def test_a_streamed_repeat_after_every_runner_refused_opens_a_new_stream(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, transport: ScriptedStreamTransport
) -> None:
    engine = await provisioned(make_engine, admin)
    transport.opens = [RunnerCallError("refused", "busy", payment_sent=False)] * 2  # both runners
    request = JobRequest(app=APP, operation_ref="op-1")
    with pytest.raises(JobFailed) as busy:
        await engine.jobs.streaming.stream(alice, request)
    transport.opens = [StreamScript(events=[b"again"])]
    stream = await engine.jobs.streaming.stream(alice, request)
    assert stream.job.id != busy.value.job.id
    assert await read_all(stream) == b"again"


async def test_rest_streams_the_body_with_the_job_id_header(
    make_engine: EngineFactory, admin: ActorContext, transport: ScriptedStreamTransport
) -> None:
    engine = await provisioned(make_engine, admin)
    issued = await engine.keys.issue(admin, kind="actor", actor_id="alice", application_id="default")
    headers = {"Authorization": f"Bearer {issued.token}"}
    app = create_app(engine, manage_lifecycle=False)
    transport.opens = [StreamScript(events=list(CHUNKS))]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://engine") as client:
        async with client.stream(
            "POST",
            "/v1/jobs",
            json={"app": APP, "path": "/v1/chat/completions"},
            headers={**headers, "Accept": "text/event-stream"},
        ) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            job_id = response.headers["livepeer-job-id"]
            body = await response.aread()
        assert body == b"".join(CHUNKS)
        job = (await client.get(f"/v1/jobs/{job_id}", headers=headers)).json()
        assert job["state"] == "succeeded" and job["result"]["size_bytes"] == len(body)

        # a failure after the first byte aborts the response (tests/unit/test_stream_http.py);
        # a failure before the stream opens is an ordinary error response
        transport.opens = [
            RunnerCallError("payment", "402 allowance exhausted", payment_sent=False, status_code=402)
        ]
        response = await client.post("/v1/jobs?stream=true", json={"app": APP}, headers=headers)
        assert response.status_code == 503 and response.json()["job"]["failure"]["kind"] == "payment"


async def test_restart_resolves_an_open_stream_as_uncertain(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, transport: ScriptedStreamTransport
) -> None:
    engine = await provisioned(make_engine, admin)
    transport.opens = [StreamScript(events=[b"a", 5.0, b"b"], manifest_ids=("m-1",))]
    stream = await engine.jobs.streaming.stream(alice, JobRequest(app=APP))
    assert await engine.jobs.recover_interrupted() == 1  # as a fresh process would on start
    job = await engine.jobs.get(alice, stream.job.id)
    assert job.state == "uncertain" and job.failure is not None and job.failure.kind == "interrupted"
    await stream.aclose()


def test_static_discovery_source_still_builds_runners_without_metadata() -> None:
    assert StaticDiscoverySource((runner(APP),)) is not None
