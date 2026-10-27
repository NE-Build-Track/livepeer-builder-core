"""Real SDK/adapter/services with fake HTTP: signing ambiguity is never unpaid evidence."""

import asyncio
import json
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest
from livepeer_gateway import http, live_runner
from livepeer_gateway.errors import LivepeerHTTPError

from livepeer_builder.adapters.sdk import SdkRunnerTransport
from livepeer_builder.contracts import (
    ActorContext,
    JobRequest,
    Money,
    NetworkRate,
    ProvisionRequest,
    SignedSession,
)
from livepeer_builder.errors import JobFailed
from livepeer_builder.runtime.assembly import EngineSupply
from livepeer_builder.services.costs import classify
from livepeer_builder.store import SqlStore
from livepeer_builder.testing import FakeUsageSource, runner
from tests.conftest import EngineFactory
from tests.unit.test_costs import job_with, row


@pytest.fixture
def signing(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    calls = SimpleNamespace(charges=[], dispatched=[], on_sign=None)

    async def request(url: str, *, headers=None, **kwargs):  # type: ignore[no-untyped-def]
        if not (headers or {}).get("Livepeer-Payment"):
            raise LivepeerHTTPError(
                402,
                url,
                json.dumps(
                    {
                        "payment_params": "opaque",
                        "manifest_id": "m-" + url.rsplit("/", 1)[-1],
                        "payment_url": "https://orch.example/payment",
                    }
                ),
            )
        calls.dispatched.append(url)
        return b'{"ok":true}', "application/json"

    async def sign(url: str, payload, **kwargs):  # type: ignore[no-untyped-def]
        calls.charges.append(payload["ManifestID"])
        if calls.on_sign is not None:
            return await calls.on_sign()
        return {"payment": "ticket", "segCreds": "segment", "state": {}}

    monkeypatch.setattr(
        live_runner, "get_signer_info", AsyncMock(return_value=SimpleNamespace(address="payer"))
    )
    monkeypatch.setattr(live_runner, "_request_body", request)
    monkeypatch.setattr(live_runner, "open_stream", request)
    monkeypatch.setattr(http, "post_json", sign)
    return calls


async def paid_engine(make_engine: EngineFactory, admin: ActorContext):  # type: ignore[no-untyped-def]
    rate = NetworkRate(amount=Decimal("0.1"), currency="usd", unit="fixed")
    runners = tuple(runner(url=f"https://runner/{i}").model_copy(update={"rate": rate}) for i in (1, 2))
    engine = await make_engine(
        runners=runners, supply=EngineSupply(transport=SdkRunnerTransport("https://signer"))
    )
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
    signing: SimpleNamespace,
) -> None:
    async def lost():  # type: ignore[no-untyped-def]
        raise aiohttp.ClientConnectionError("reply lost after signing")

    signing.on_sign = lost
    engine = await paid_engine(make_engine, admin)
    with pytest.raises(JobFailed) as failed:
        await engine.jobs.run(alice, JobRequest(app="example/echo"))
    job = failed.value.job
    assert len(signing.charges) == 1, "an ambiguous signing request must not pay another runner"
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
    signing: SimpleNamespace,
) -> None:
    async def refuses_first():  # type: ignore[no-untyped-def]
        if len(signing.charges) == 1:
            raise LivepeerHTTPError(481, "https://signer/generate-live-payment")
        return {"payment": "ticket", "segCreds": "segment", "state": {}}

    signing.on_sign = refuses_first
    engine = await paid_engine(make_engine, admin)
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
    signing: SimpleNamespace,
    store: SqlStore,
    streaming: bool,
) -> None:
    entered = asyncio.Event()

    async def blocked():  # type: ignore[no-untyped-def]
        [job] = await store.jobs_in_state("running")
        assert job.attempts[0].sessions[0].payment_state == "unknown"
        entered.set()
        await asyncio.Event().wait()

    signing.on_sign = blocked
    engine = await paid_engine(make_engine, admin)
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
        assert signing.charges == ["m-1"] and signing.dispatched == []
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
    signing: SimpleNamespace,
    invalid: str,
) -> None:
    async def malformed():  # type: ignore[no-untyped-def]
        return {"payment": "ticket", "segCreds": "segment", "state": {}} | {invalid: None}

    signing.on_sign = malformed
    engine = await paid_engine(make_engine, admin)
    with pytest.raises(JobFailed) as failed:
        await engine.jobs.run(alice, JobRequest(app="example/echo"))
    assert failed.value.job.state == "uncertain"
    assert failed.value.job.attempts[0].payment_unknown
    assert signing.charges == ["m-1"] and signing.dispatched == []


async def test_stream_open_timeout_preserves_unknown_payment(
    make_engine: EngineFactory,
    admin: ActorContext,
    alice: ActorContext,
    signing: SimpleNamespace,
) -> None:
    async def blocked():  # type: ignore[no-untyped-def]
        await asyncio.Event().wait()

    signing.on_sign = blocked
    engine = await paid_engine(make_engine, admin)
    with pytest.raises(JobFailed) as failed:
        async with asyncio.timeout(2):
            await engine.jobs.streaming.stream(
                alice, JobRequest(app="example/echo", first_byte_timeout_s=0.05)
            )
    job = failed.value.job
    assert job.state == "uncertain" and job.attempts[0].payment_unknown
    assert signing.charges == ["m-1"]
    assert (await engine.costs.for_job(alice, job.id)).status == "pending"


async def test_intent_write_failure_prevents_signing(
    make_engine: EngineFactory,
    admin: ActorContext,
    alice: ActorContext,
    signing: SimpleNamespace,
    store: SqlStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = await paid_engine(make_engine, admin)
    monkeypatch.setattr(store, "record_payment_evidence", AsyncMock(side_effect=ConnectionError("db down")))
    with pytest.raises(JobFailed):
        await engine.jobs.run(alice, JobRequest(app="example/echo"))
    assert signing.charges == [] and signing.dispatched == []
