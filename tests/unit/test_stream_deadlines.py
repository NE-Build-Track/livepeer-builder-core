"""Stream deadlines (D35): timeout_s from the job's start, first_byte_timeout_s while opening and
before the first chunk, idle_timeout_s between chunks, and the engine's backstop for a transport that
ignores its deadline."""

import asyncio
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
from pydantic import ValidationError

from livepeer_builder import EngineSupply
from livepeer_builder.adapters.sdk import SdkRunnerTransport
from livepeer_builder.contracts import ActorContext, JobRequest, Money, ProvisionRequest
from livepeer_builder.errors import JobFailed
from livepeer_builder.testing import ScriptedStreamTransport, StreamScript
from tests.conftest import EngineFactory
from tests.unit.livepeer_net import Net, Reply, start
from tests.unit.test_streaming import APP, collect, provisioned, read_all


@pytest.fixture
async def net() -> AsyncIterator[Net]:
    async for value in start():
        yield value


def test_defaults_and_limits() -> None:
    request = JobRequest(app=APP)
    assert (request.timeout_s, request.first_byte_timeout_s, request.idle_timeout_s) == (300, 60, 60)
    assert JobRequest(app=APP, timeout_s=10).first_byte_timeout_s == 60  # the engine caps it at timeout_s
    with pytest.raises(ValidationError, match="first_byte_timeout_s cannot be longer"):
        JobRequest(app=APP, timeout_s=10, first_byte_timeout_s=20)
    assert JobRequest(app=APP, idle_timeout_s=None).idle_timeout_s is None


@pytest.mark.parametrize(("paid", "state"), [(True, "uncertain"), (False, "failed")])
async def test_a_runner_that_never_answers_times_out_without_failover(
    make_engine: EngineFactory,
    admin: ActorContext,
    alice: ActorContext,
    transport: ScriptedStreamTransport,
    paid: bool,
    state: str,
) -> None:
    engine = await provisioned(make_engine, admin)
    transport.opens = [StreamScript(open_delay_s=5, payment_sent=paid, manifest_ids=("m-1",) if paid else ())]
    with pytest.raises(JobFailed) as failed:
        await engine.jobs.streaming.stream(alice, JobRequest(app=APP, first_byte_timeout_s=0.05))
    job = failed.value.job
    assert job.state == state and job.failure is not None and job.failure.kind == "timeout"
    assert len(job.attempts) == 1  # a timeout never fails over, paid or not (same as a buffered job)
    assert job.attempts[0].outcome == "timeout" and job.attempts[0].payment_sent is paid
    assert engine.admission.running == 0


async def test_the_engine_stops_a_transport_that_ignores_its_deadline(
    make_engine: EngineFactory,
    admin: ActorContext,
    alice: ActorContext,
    transport: ScriptedStreamTransport,
) -> None:
    engine = await provisioned(make_engine, admin, stream_open_grace_s=0.05)
    transport.opens = [StreamScript(open_delay_s=5, honors_deadline=False, payment_sent=False)]
    with pytest.raises(JobFailed) as failed:
        await engine.jobs.streaming.stream(alice, JobRequest(app=APP, first_byte_timeout_s=0.05))
    job = failed.value.job
    assert job.state == "uncertain"  # whether it paid is unknown, so the engine assumes it did
    assert job.attempts[0].outcome == "timeout" and job.attempts[0].payment_sent
    assert engine.admission.running == 0


@pytest.mark.parametrize(
    ("hang", "paid", "state", "manifests"),
    [("signer", False, "uncertain", ("m-1",)), ("runner", True, "uncertain", ("m-1",))],
    ids=["while-signing", "after-payment"],
)
async def test_the_live_transport_times_out_by_whether_signing_finished(
    make_engine: EngineFactory,
    admin: ActorContext,
    alice: ActorContext,
    net: Net,
    hang: str,
    paid: bool,
    state: str,
    manifests: tuple[str, ...],
) -> None:
    """The real transport against a loopback runner and signer: a timeout while signing leaves the
    payment unknown with its manifest already on the attempt (D53), one after the signer's payment was
    validated is paid (D41). Either may have charged, so both are uncertain, and neither fails over."""
    if hang == "signer":
        net.sign = [5.0]
    else:
        net.replies = [Reply(hang_s=5)]
    runners = (net.runner(), net.runner(orchestrator_url="https://other-orch.example"))
    engine = await make_engine(runners=runners, supply=EngineSupply(transport=SdkRunnerTransport(net.url)))
    request = ProvisionRequest(
        application_id="default",
        actor_id="alice",
        amount=Money(amount=Decimal(5), currency="USD"),
        grant_id="g",
    )
    await engine.payments.provision(admin, request)
    with pytest.raises(JobFailed) as failed:
        await engine.jobs.streaming.stream(alice, JobRequest(app=net.runner().app, first_byte_timeout_s=0.1))
    job = failed.value.job
    expected_kind = "payment_unknown" if hang == "signer" else "timeout"
    assert job.state == state and job.failure is not None and job.failure.kind == expected_kind
    assert job.failure.payment_sent is paid
    assert len(job.attempts) == 1  # a timeout never fails over, paid or not
    attempt = job.attempts[0]
    outcome = "timeout" if paid else "payment_unknown"  # stored, so a restart still sees it may have paid
    assert (attempt.outcome, attempt.payment_sent, tuple(s.manifest_id for s in attempt.sessions)) == (
        outcome,
        paid,
        manifests,
    )
    assert engine.admission.running == 0


async def test_headers_without_a_first_chunk_time_out_on_first_byte(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, transport: ScriptedStreamTransport
) -> None:
    engine = await provisioned(make_engine, admin)
    transport.opens = [StreamScript(events=[5.0, b"late"])]
    stream = await engine.jobs.streaming.stream(alice, JobRequest(app=APP, first_byte_timeout_s=0.05))
    with pytest.raises(JobFailed):
        await read_all(stream)
    assert stream.job.failure is not None and stream.job.failure.kind == "timeout"
    assert "first_byte_timeout_s" in stream.job.failure.message


async def test_time_spent_opening_counts_against_timeout_s(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, transport: ScriptedStreamTransport
) -> None:
    engine = await provisioned(make_engine, admin)
    # 60 ms to open plus 60 ms of chunks: within 0.1 s if the clock started at the first read, not overall
    transport.opens = [StreamScript(open_delay_s=0.06, events=[b"a", 0.03, b"b", 0.03, b"c"])]
    stream = await engine.jobs.streaming.stream(
        alice, JobRequest(app=APP, timeout_s=0.1, idle_timeout_s=None)
    )
    with pytest.raises(JobFailed):
        await read_all(stream)
    assert stream.job.failure is not None and "timeout_s" in stream.job.failure.message


async def test_timeout_s_does_not_apply_once_the_runner_has_finished(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, transport: ScriptedStreamTransport
) -> None:
    engine = await provisioned(make_engine, admin)
    transport.opens = [StreamScript(events=[b"a", b"b"])]  # the runner is done straight away
    stream = await engine.jobs.streaming.stream(alice, JobRequest(app=APP, timeout_s=0.3))
    received: list[bytes] = []
    async for chunk in stream:
        received.append(chunk)
        await asyncio.sleep(0.4)  # the caller is still handling a chunk when timeout_s runs out
    assert received == [b"a", b"b"]
    assert stream.job.state == "succeeded" and stream.job.attempts[0].outcome == "succeeded"
    assert engine.admission.running == 0


async def test_idle_applies_between_chunks_only(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, transport: ScriptedStreamTransport
) -> None:
    engine = await provisioned(make_engine, admin)
    # a slow first chunk is fine under first_byte_timeout_s; a gap after it is not under idle_timeout_s
    transport.opens = [StreamScript(events=[0.08, b"a", 0.08, b"b"])]
    request = JobRequest(app=APP, first_byte_timeout_s=1, idle_timeout_s=0.05)
    stream = await engine.jobs.streaming.stream(alice, request)
    received: list[bytes] = []
    with pytest.raises(JobFailed):
        await collect(stream, received)
    assert received == [b"a"]
    assert stream.job.failure is not None and "idle_timeout_s" in stream.job.failure.message
