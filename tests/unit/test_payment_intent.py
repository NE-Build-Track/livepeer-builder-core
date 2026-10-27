"""Real transport, SDK signer client and services over a loopback runner and signer (D54): signing
ambiguity is never unpaid evidence."""

import asyncio
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock

import pytest

from livepeer_builder.adapters.sdk import SdkRunnerTransport
from livepeer_builder.contracts import (
    ActorContext,
    JobRequest,
    Money,
    ProvisionRequest,
    SignedSession,
)
from livepeer_builder.errors import JobFailed
from livepeer_builder.runtime.assembly import EngineSupply
from livepeer_builder.services.costs import classify
from livepeer_builder.store import SqlStore
from livepeer_builder.testing import FakeUsageSource
from tests.conftest import EngineFactory
from tests.unit.livepeer_net import Net, start
from tests.unit.test_costs import job_with, row


@pytest.fixture
async def signing() -> AsyncIterator[Net]:
    async for net in start():
        yield net


def charges(net: Net) -> list[str]:
    """The manifests the signer was asked to sign."""
    return [request["ManifestID"] for request in net.signer_requests]


def dispatched(net: Net) -> list[str]:
    """Paid requests the runner received."""
    return [entry for entry in net.log if entry.startswith("request pay")]


async def paid_engine(make_engine: EngineFactory, admin: ActorContext, net: Net):  # type: ignore[no-untyped-def]
    runners = tuple(net.runner(name=str(i)) for i in (1, 2))  # one orchestrator
    engine = await make_engine(runners=runners, supply=EngineSupply(transport=SdkRunnerTransport(net.url)))
    await engine.payments.provision(
        admin,
        ProvisionRequest(
            application_id="default",
            actor_id="alice",
            amount=Money(amount=Decimal(1), currency="USD"),
            grant_id="grant_1",
        ),
    )
    return engine


async def test_lost_signer_reply_never_fails_over_and_later_usage_matches_intent(
    make_engine: EngineFactory,
    admin: ActorContext,
    alice: ActorContext,
    usage: FakeUsageSource,
    signing: Net,
) -> None:
    signing.sign = ["drop"]  # the connection closes before the signer's reply
    engine = await paid_engine(make_engine, admin, signing)
    with pytest.raises(JobFailed) as failed:
        await engine.jobs.run(alice, JobRequest(app="example/echo"))
    job = failed.value.job
    assert len(charges(signing)) == 1, "an ambiguous signing request must not pay another runner"
    assert job.state == "uncertain"
    assert not job.attempts[0].payment_sent
    assert job.attempts[0].sessions[0].payment_state == "unknown"
    assert (await engine.costs.for_job(alice, job.id)).status == "pending"
    usage.rows = [row("u1", "s1", "0.001", usd="0.1", manifest="m-1")]
    await engine.costs.sync_once()
    cost = await engine.costs.for_job(alice, job.id)
    assert cost.status == "observed" and cost.attempts[0].cost_class == "uncertain"
    assert await engine.store.unattributed_usage("alloc_1") == []
    resolved = await engine.store.get_job(job.id)
    assert resolved is not None and resolved.attempts[0].payment_sent
    assert resolved.attempts[0].sessions[0].payment_state == "paid"


async def test_explicit_signer_refusal_can_fail_over_without_a_pending_cost(
    make_engine: EngineFactory,
    admin: ActorContext,
    alice: ActorContext,
    signing: Net,
) -> None:
    signing.sign = [481]  # the first runner's price is above the ceiling; the second signs
    engine = await paid_engine(make_engine, admin, signing)
    job = (await engine.jobs.run(alice, JobRequest(app="example/echo"))).job
    assert [a.sessions[0].payment_state for a in job.attempts] == ["unpaid", "paid"]
    cost = await engine.costs.for_job(alice, job.id)
    assert [a.attempt_number for a in cost.attempts] == [2]


def test_an_unresolved_intent_keeps_partial_usage_pending() -> None:
    job = job_with(("timeout", ("s",), True), state="uncertain")
    attempt = job.attempts[0]
    job = job.model_copy(
        update={
            "attempts": (
                attempt.model_copy(
                    update={
                        "sessions": (
                            *attempt.sessions,
                            SignedSession(manifest_id="later", payment_state="unknown"),
                        ),
                    }
                ),
            )
        }
    )
    cost = classify(job, [row("u", "s", "1")])
    assert cost.status == "pending"
    assert cost.fees.fee_eth == Decimal(1)


@pytest.mark.parametrize("streaming", [False, True])
async def test_cancellation_during_signing_keeps_durable_intent(
    make_engine: EngineFactory,
    admin: ActorContext,
    alice: ActorContext,
    signing: Net,
    store: SqlStore,
    streaming: bool,
) -> None:
    entered = asyncio.Event()

    async def blocked() -> str:
        [job] = await store.jobs_in_state("running")
        assert job.attempts[0].sessions[0].payment_state == "unknown"
        entered.set()
        await signing.hang(30)  # released at teardown
        return "ok"

    signing.on_sign = blocked
    engine = await paid_engine(make_engine, admin, signing)
    operation = engine.jobs.streaming.stream if streaming else engine.jobs.run
    task = asyncio.create_task(operation(alice, JobRequest(app="example/echo")))
    try:
        async with asyncio.timeout(2):
            await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            async with asyncio.timeout(2):
                await task
        [job] = await store.jobs_in_state("uncertain")
        assert job.attempts[0].payment_unknown and not job.attempts[0].payment_sent
        assert charges(signing) == ["m-1"] and dispatched(signing) == []
        assert (await engine.costs.for_job(alice, job.id)).status == "pending"
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("invalid", ["payment", "segCreds", "state"])
async def test_malformed_signer_success_is_unknown(
    make_engine: EngineFactory,
    admin: ActorContext,
    alice: ActorContext,
    signing: Net,
    invalid: str,
) -> None:
    async def malformed() -> dict[str, Any]:
        return {"payment": "ticket", "segCreds": "segment", "state": {}} | {invalid: None}

    signing.on_sign = malformed
    engine = await paid_engine(make_engine, admin, signing)
    with pytest.raises(JobFailed) as failed:
        await engine.jobs.run(alice, JobRequest(app="example/echo"))
    assert failed.value.job.state == "uncertain"
    assert failed.value.job.attempts[0].payment_unknown
    assert charges(signing) == ["m-1"] and dispatched(signing) == []


async def test_stream_open_timeout_preserves_unknown_payment(
    make_engine: EngineFactory,
    admin: ActorContext,
    alice: ActorContext,
    signing: Net,
) -> None:
    async def blocked() -> str:
        await signing.hang(30)  # released at teardown
        return "ok"

    signing.on_sign = blocked
    engine = await paid_engine(make_engine, admin, signing)
    with pytest.raises(JobFailed) as failed:
        async with asyncio.timeout(2):
            await engine.jobs.streaming.stream(
                alice, JobRequest(app="example/echo", first_byte_timeout_s=0.05)
            )
    job = failed.value.job
    assert job.state == "uncertain" and job.attempts[0].payment_unknown
    assert charges(signing) == ["m-1"]
    assert (await engine.costs.for_job(alice, job.id)).status == "pending"


async def test_intent_write_failure_prevents_signing(
    make_engine: EngineFactory,
    admin: ActorContext,
    alice: ActorContext,
    signing: Net,
    store: SqlStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = await paid_engine(make_engine, admin, signing)
    monkeypatch.setattr(store, "record_payment_evidence", AsyncMock(side_effect=ConnectionError("db down")))
    with pytest.raises(JobFailed):
        await engine.jobs.run(alice, JobRequest(app="example/echo"))
    assert charges(signing) == [] and dispatched(signing) == []
