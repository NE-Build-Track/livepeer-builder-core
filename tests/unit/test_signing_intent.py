"""D53: a payment-session row is inserted only when ``on_signed`` runs.

``remember`` keeps the manifest id in memory and does not insert a row. An expired challenge, a lost
reply and a cancel leave no session. The engine, the Live Runner transport and the SDK's signer client
are real; only the network is local (tests/unit/livepeer_net.py). The job passes the recorder as
``on_signed``; nothing passes a second callback.
"""

import asyncio
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest

from livepeer_builder import EngineSupply
from livepeer_builder.adapters.sdk import SdkRunnerTransport
from livepeer_builder.contracts import ActorContext, JobRequest, Money, ProvisionRequest
from livepeer_builder.errors import JobFailed
from livepeer_builder.runtime import BuilderEngine
from livepeer_builder.store import SqlStore
from livepeer_builder.testing import FakeUsageSource
from tests.conftest import EngineFactory
from tests.unit.livepeer_net import Net, Reply, start
from tests.unit.test_costs import row


@pytest.fixture
async def net() -> AsyncIterator[Net]:
    async for value in start():
        yield value


async def paid_engine(
    make_engine: EngineFactory, admin: ActorContext, net: Net, *, retries: int = 3
) -> tuple[BuilderEngine, str]:
    runners = (net.runner(name="a1"), net.runner(name="b1", orchestrator_url="https://orch-b.example"))
    transport = SdkRunnerTransport(
        net.url,
        max_payment_challenge_retries=retries,
    )
    engine = await make_engine(
        runners=runners,
        supply=EngineSupply(
            transport=transport,
        ),
    )
    request = ProvisionRequest(
        application_id="default",
        actor_id="alice",
        amount=Money(amount=Decimal(5), currency="USD"),
        grant_id="g",
    )
    actor = await engine.payments.provision(admin, request)
    return engine, actor.allocation_id


@pytest.mark.parametrize("step", [503, "drop", "malformed"])
async def test_a_signer_that_gives_no_answer_leaves_no_session_and_an_uncertain_job(
    make_engine: EngineFactory,
    admin: ActorContext,
    alice: ActorContext,
    net: Net,
    usage: FakeUsageSource,
    step: int | str,
) -> None:
    engine, allocation = await paid_engine(make_engine, admin, net)
    net.sign = [step]
    with pytest.raises(JobFailed) as failed:
        await engine.jobs.run(alice, JobRequest(app="example/echo"))
    job = failed.value.job
    assert (
        job.state == "uncertain" and len(job.attempts) == 1
    )  # the signer may have charged: no one else is paid
    stored = await engine.jobs.get(alice, job.id)
    attempt = stored.attempts[0]
    assert attempt.outcome == "payment_unknown"
    assert attempt.sessions == () and not attempt.payment_sent

    usage.rows = [row("u1", "s1", "0.002", usd="0.2", allocation=allocation, manifest="m-1")]
    await engine.costs.sync_once()
    cost = await engine.costs.for_job(alice, job.id)
    assert cost.status == "none" and cost.attempts == ()  # no session to match, so the fee is not on the job
    assert cost.fees.fee_eth == 0 and cost.billable.fee_eth == 0
    unattributed = await engine.store.unattributed_usage(allocation)
    assert [item.computed_fee_eth for item in unattributed] == [Decimal("0.002")]


async def test_a_refused_signing_stores_only_the_delivered_session(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, net: Net
) -> None:
    engine, _allocation = await paid_engine(make_engine, admin, net)
    net.sign = [481]  # this orchestrator's price is above the ceiling: another orchestrator may succeed
    result = await engine.jobs.run(alice, JobRequest(app="example/echo"))
    job = await engine.jobs.get(alice, result.job.id)
    assert [(a.outcome, a.payment_sent) for a in job.attempts] == [
        ("payment_refused", False),
        ("succeeded", True),
    ]
    assert [s.manifest_id for s in job.attempts[0].sessions] == []
    assert [s.manifest_id for s in job.attempts[1].sessions] == ["m-2"]


async def test_a_cancel_during_signing_leaves_no_session(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, net: Net, store: SqlStore
) -> None:
    net.sign = [30.0]  # the signer accepts the request and does not answer
    engine, _ = await paid_engine(make_engine, admin, net)
    task = asyncio.create_task(engine.jobs.run(alice, JobRequest(app="example/echo")))
    for _ in range(200):
        if "sign m-1" in net.log:
            break
        await asyncio.sleep(0.01)
    else:
        raise AssertionError("the signer was not asked")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        async with asyncio.timeout(2):
            await task
    [job] = await store.jobs_in_state("uncertain")
    assert job.attempts[0].sessions == ()
    assert not job.attempts[0].payment_sent


async def test_a_refresh_stores_only_the_signed_manifest(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, net: Net
) -> None:
    engine, _allocation = await paid_engine(make_engine, admin, net)
    net.sign = [480]  # params expired: the same attempt asks for a fresh challenge and pays that
    result = await engine.jobs.run(alice, JobRequest(app="example/echo"))
    job = await engine.jobs.get(alice, result.job.id)
    assert [(a.outcome, a.payment_sent) for a in job.attempts] == [("succeeded", True)]
    assert [s.manifest_id for s in job.attempts[0].sessions] == ["m-2"]


async def test_a_refreshed_stream_stores_only_the_signed_manifest(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, net: Net
) -> None:
    engine, _allocation = await paid_engine(make_engine, admin, net)
    net.sign = [480]
    net.replies = [Reply(content_type="text/event-stream", chunks=(b"data: a\n\n",))]
    streaming = engine.jobs.streaming
    assert streaming is not None
    stream = await streaming.stream(alice, JobRequest(app="example/echo"))
    assert b"".join([chunk async for chunk in stream]) == b"data: a\n\n"
    assert stream.job.state == "succeeded"
    assert [s.manifest_id for s in stream.job.attempts[0].sessions] == ["m-2"]


async def test_an_exhausted_refresh_fails_over_to_the_other_orchestrator(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, net: Net
) -> None:
    engine, _allocation = await paid_engine(make_engine, admin, net, retries=1)
    net.sign = [480, 480]  # one in-loop refresh, then the budget is spent
    result = await engine.jobs.run(alice, JobRequest(app="example/echo"))
    job = await engine.jobs.get(alice, result.job.id)
    assert [(a.outcome, a.payment_sent) for a in job.attempts] == [
        ("payment_refused", False),
        ("succeeded", True),
    ]
    assert [s.manifest_id for s in job.attempts[0].sessions] == []
    assert [s.manifest_id for s in job.attempts[1].sessions] == ["m-3"]
