"""D53: the job path records each manifest before the signer is asked, through the engine's own
SignedSessionRecorder, so a charge whose signer reply never arrives keeps its manifest on the attempt.

The engine, the Live Runner transport and the SDK's signer client are real; only the network is local
(tests/unit/livepeer_net.py). No test passes ``on_payment_state`` by hand.
"""

from collections.abc import AsyncIterator
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from livepeer_builder import EngineSupply
from livepeer_builder.adapters.sdk import SdkRunnerTransport
from livepeer_builder.contracts import ActorContext, JobRequest, Money, ProvisionRequest
from livepeer_builder.errors import JobFailed
from livepeer_builder.runtime import BuilderEngine
from livepeer_builder.store import SqlStore
from livepeer_builder.testing import FakeUsageSource
from tests.conftest import EngineFactory
from tests.unit.livepeer_net import Net, start
from tests.unit.test_costs import row


@pytest.fixture
async def net() -> AsyncIterator[Net]:
    async for value in start():
        yield value


async def paid_engine(make_engine: EngineFactory, admin: ActorContext, net: Net) -> tuple[BuilderEngine, str]:
    runners = (net.runner(name="a1"), net.runner(name="b1", orchestrator_url="https://orch-b.example"))
    engine = await make_engine(runners=runners, supply=EngineSupply(transport=SdkRunnerTransport(net.url)))
    request = ProvisionRequest(
        application_id="default",
        actor_id="alice",
        amount=Money(amount=Decimal(5), currency="USD"),
        grant_id="g",
    )
    actor = await engine.payments.provision(admin, request)
    return engine, actor.allocation_id


@pytest.mark.parametrize("step", [503, "drop", "malformed"])
async def test_a_signer_that_gives_no_answer_leaves_the_manifest_and_an_uncertain_job(
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
    assert [s.manifest_id for s in attempt.sessions] == ["m-1"] and not attempt.payment_sent
    assert (await engine.costs.for_job(alice, job.id)).status == "pending"  # until usage says

    usage.rows = [row("u1", "s1", "0.002", usd="0.2", allocation=allocation, manifest="m-1")]
    await engine.costs.sync_once()
    cost = await engine.costs.for_job(alice, job.id)
    assert cost.status == "observed" and cost.fees.fee_eth == Decimal("0.002")  # the charge is attributed
    assert cost.attempts[0].cost_class == "uncertain" and cost.billable.fee_eth == 0


async def test_a_refused_signing_records_its_manifest_but_costs_nothing(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, net: Net
) -> None:
    engine, _ = await paid_engine(make_engine, admin, net)
    net.sign = [481]  # this orchestrator's price is above the ceiling: another orchestrator may succeed
    result = await engine.jobs.run(alice, JobRequest(app="example/echo"))
    job = await engine.jobs.get(alice, result.job.id)
    assert [(a.outcome, a.payment_sent) for a in job.attempts] == [
        ("payment_refused", False),
        ("succeeded", True),
    ]
    assert [s.manifest_id for s in job.attempts[0].sessions] == ["m-1"]
    cost = await engine.costs.for_job(alice, job.id)
    assert [a.attempt_number for a in cost.attempts] == [2]  # the refusal charged nothing


async def test_nothing_is_signed_when_the_intent_cannot_be_stored(
    make_engine: EngineFactory,
    admin: ActorContext,
    alice: ActorContext,
    net: Net,
    store: SqlStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, _ = await paid_engine(make_engine, admin, net)
    monkeypatch.setattr(store, "record_payment_evidence", AsyncMock(side_effect=ConnectionError("db down")))
    with pytest.raises(JobFailed) as failed:
        await engine.jobs.run(alice, JobRequest(app="example/echo"))
    job = failed.value.job
    assert job.state == "failed" and job.failure is not None and job.failure.kind == "payment"
    assert not any(entry.startswith("sign m") for entry in net.log)  # the signer was never asked
