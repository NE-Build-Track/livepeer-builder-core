"""D53: ``on_signed`` writes a paid session. A non-refusal keeps the remembered manifest, unpaid.

An expired challenge and a signer refusal leave no session. A lost reply and a cancel keep the
manifest the signer was asked for. The engine, the Live Runner transport and the SDK's signer client
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
async def test_a_signer_that_gives_no_answer_keeps_the_manifest_and_an_uncertain_job(
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
    assert [s.manifest_id for s in attempt.sessions] == ["m-1"] and not attempt.payment_sent
    assert (await engine.costs.for_job(alice, job.id)).status == "pending"  # until usage says

    usage.rows = [row("u1", "s1", "0.002", usd="0.2", allocation=allocation, manifest="m-1")]
    await engine.costs.sync_once()
    cost = await engine.costs.for_job(alice, job.id)
    assert cost.status == "observed" and cost.fees.fee_eth == Decimal("0.002")
    assert cost.attempts[0].cost_class == "uncertain" and cost.billable.fee_eth == 0


async def test_a_refused_signing_stores_only_the_delivered_session(
    make_engine: EngineFactory,
    admin: ActorContext,
    alice: ActorContext,
    net: Net,
    usage: FakeUsageSource,
) -> None:
    engine, allocation = await paid_engine(make_engine, admin, net)
    net.sign = [481]  # this orchestrator's price is above the ceiling: another orchestrator may succeed
    result = await engine.jobs.run(alice, JobRequest(app="example/echo"))
    job = await engine.jobs.get(alice, result.job.id)
    assert [(a.outcome, a.payment_sent) for a in job.attempts] == [
        ("payment_refused", False),
        ("succeeded", True),
    ]
    assert [s.manifest_id for s in job.attempts[0].sessions] == []
    assert [s.manifest_id for s in job.attempts[1].sessions] == ["m-2"]
    waiting = await engine.costs.for_job(alice, job.id)
    assert waiting.status == "pending"  # the signed session has no usage yet
    assert [a.cost_class for a in waiting.attempts] == ["pending"]
    assert waiting.billable.fee_eth == 0

    usage.rows = [row("u1", "s1", "0.002", usd="0.2", allocation=allocation, manifest="m-1")]
    await engine.costs.sync_once()
    matched = await engine.costs.for_job(alice, job.id)
    assert matched.status == "pending"
    assert [a.cost_class for a in matched.attempts] == ["pending"]
    assert matched.fees.fee_eth == 0 and matched.billable.fee_eth == 0
    unattributed = await engine.store.unattributed_usage(allocation)
    assert [item.computed_fee_eth for item in unattributed] == [Decimal("0.002")]


async def test_a_cancel_during_signing_keeps_the_manifest_unpaid(
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
    assert [s.manifest_id for s in job.attempts[0].sessions] == ["m-1"]
    assert not job.attempts[0].payment_sent


async def test_a_refresh_stores_only_the_signed_manifest(
    make_engine: EngineFactory,
    admin: ActorContext,
    alice: ActorContext,
    net: Net,
    usage: FakeUsageSource,
) -> None:
    engine, allocation = await paid_engine(make_engine, admin, net)
    net.sign = [480]  # params expired: the same attempt asks for a fresh challenge and pays that
    result = await engine.jobs.run(alice, JobRequest(app="example/echo"))
    job = await engine.jobs.get(alice, result.job.id)
    assert [(a.outcome, a.payment_sent) for a in job.attempts] == [("succeeded", True)]
    assert [s.manifest_id for s in job.attempts[0].sessions] == ["m-2"]
    usage.rows = [
        row("u1", "s1", "0.001", usd="0.1", allocation=allocation, manifest="m-1"),
        row("u2", "s2", "0.004", usd="0.4", allocation=allocation, manifest="m-2"),
    ]
    await engine.costs.sync_once()
    cost = await engine.costs.for_job(alice, result.job.id)
    assert [(attempt.cost_class, attempt.fees.fee_eth) for attempt in cost.attempts] == [
        ("delivered", Decimal("0.004"))
    ]
    assert cost.fees.fee_eth == Decimal("0.004") and cost.billable.fee_eth == Decimal("0.004")
    unattributed = await engine.store.unattributed_usage(allocation)
    assert [item.computed_fee_eth for item in unattributed] == [Decimal("0.001")]


async def test_a_signed_session_without_usage_keeps_the_job_pending(
    make_engine: EngineFactory,
    admin: ActorContext,
    alice: ActorContext,
    net: Net,
    usage: FakeUsageSource,
) -> None:
    engine, allocation = await paid_engine(make_engine, admin, net)
    net.sign = [480]
    result = await engine.jobs.run(alice, JobRequest(app="example/echo"))
    usage.rows = [row("u1", "s1", "0.001", usd="0.1", allocation=allocation, manifest="m-1")]
    await engine.costs.sync_once()
    waiting = await engine.costs.for_job(alice, result.job.id)
    assert waiting.status == "pending"  # the signed payment has not shown up: not observed at 0
    assert [(attempt.cost_class, attempt.fees.fee_eth) for attempt in waiting.attempts] == [
        ("pending", Decimal(0))
    ]
    assert waiting.billable.fee_eth == 0

    usage.rows.append(row("u2", "s2", "0.004", usd="0.4", allocation=allocation, manifest="m-2"))
    await engine.costs.sync_once()
    cost = await engine.costs.for_job(alice, result.job.id)
    assert [(attempt.cost_class, attempt.fees.fee_eth) for attempt in cost.attempts] == [
        ("delivered", Decimal("0.004"))
    ]
    assert cost.status == "observed"
    assert cost.fees.fee_eth == Decimal("0.004") and cost.billable.fee_eth == Decimal("0.004")


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
