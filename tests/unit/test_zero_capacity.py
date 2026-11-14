"""#114 C17: a runner that advertises zero capacity stays in selection, tried last.

Advertised capacity is a hint that can lag by a discovery interval and more. When every runner for an app
said zero, the job failed ``no_capacity`` before any attempt, though a runner might have had room by
then. Now the engine tries them anyway, after any runner with room, when the attempt signs no payment. If
each refuses, the job fails ``refused`` through the normal failover path: 503 and ``Retry-After: 5`` as
before.

A job that would pay keeps the free fast fail. livepeer/node asks a priced request for payment before it
looks at capacity, so a full runner's 503 arrives on the paid request and the ticket is still charged.
When every runner advertises zero, the engine tries only the runners it would sign nothing for; with none
left the job fails ``no_capacity`` before any attempt, nothing signed.

These tests carry into ISS-48 (see ``discovery_wire``).
"""

import json
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any

import httpx
import pytest

from livepeer_builder import BuilderEngine, EngineSupply
from livepeer_builder.contracts import ActorContext, JobRequest, Money, ProvisionRequest, Runner
from livepeer_builder.errors import JobFailed, RunnerCallError
from livepeer_builder.http import create_app
from livepeer_builder.testing import ScriptedStreamTransport, ScriptedTransport
from tests.unit.discovery_wire import ALICE, record, wire_engine

APP = "good/app"
REQUEST = JobRequest(app=APP)
PRICED = {"price": 1000, "currency": "wei", "unit": "fixed"}  # a price the signer signs for
FREE = {"price": 0, "currency": "wei", "unit": "fixed"}  # never asked to pay
USD_PER_HOUR = {"price": 1, "currency": "usd", "unit": "hour"}  # refused at its 402, nothing signed


def runner_record(name: str, **fields: Any) -> dict[str, Any]:
    """A runner on its own orchestrator, so one refusal never excludes another runner. With no
    ``price_info`` it advertises no price: an attempt on it signs no payment, also for alice, who pays."""
    return record(url=f"https://{name}.example/app", **fields)


def full_and_priced(name: str) -> dict[str, Any]:
    return runner_record(name, capacity_available=0, price_info=PRICED)


def answer(*records: dict[str, Any]) -> bytes:
    """A discovery answer with one orchestrator entry per runner record."""
    entries = [{"address": item["url"].removesuffix("/app"), "runners": [item]} for item in records]
    return json.dumps(entries).encode()


def refusal() -> RunnerCallError:
    return RunnerCallError("refused", "HTTP 503 from the runner", payment_sent=False, status_code=503)


def paid_refusal(manifest_id: str) -> RunnerCallError:
    """A full priced runner's answer, as ``test_a_runner_refusal_after_signing_keeps_the_payment`` reads it
    off the wire: the 503 comes on the paid request, after the signer signed."""
    return RunnerCallError(
        "refused",
        "HTTP 503 from the runner",
        payment_sent=True,
        manifest_ids=(manifest_id,),
        status_code=503,
        body="no capacity available for runner",
    )


OPERATOR = ActorContext(actor_id="operator", credential_kind="admin")


@asynccontextmanager
async def engine_offering(
    inventory: bytes, transport: ScriptedTransport, *, then: bytes | None = None, **supply: Any
) -> AsyncIterator[BuilderEngine]:
    """An engine whose discovery answered ``inventory``, with alice provisioned to run and pay for jobs.
    The next refresh reads ``then``."""
    answers = (inventory,) if then is None else (inventory, then)
    async with wire_engine(*answers, supply=EngineSupply(transport=transport, **supply)) as engine:
        request = ProvisionRequest(
            operation_ref="provision:default:alice",
            application_id="default",
            actor_id="alice",
            amount=Money(amount=Decimal(5), currency="USD"),
            grant_id="grant_1",
        )
        await engine.payments.provision(OPERATOR, request)
        yield engine


def called(transport: ScriptedTransport) -> list[str]:
    return [url.removeprefix("https://").removesuffix(".example/app") for url, _ in transport.calls]


async def test_a_job_that_signs_nothing_is_tried_when_every_runner_advertises_zero_capacity() -> None:
    transport = ScriptedTransport(steps=[ScriptedTransport.ok()])
    inventory = answer(runner_record("a", capacity_available=0), runner_record("b", capacity_available=0))
    async with engine_offering(inventory, transport) as engine:
        result = await engine.jobs.run(ALICE, REQUEST)
        (offering,) = engine.discovery.offerings()
    assert result.job.state == "succeeded"
    assert len(result.job.attempts) == 1 and len(transport.calls) == 1
    assert (offering.runner_count, offering.ready_count) == (2, 0)  # the catalog still says none is ready


async def test_zero_capacity_runners_are_tried_after_every_runner_with_room() -> None:
    transport = ScriptedTransport(steps=[refusal(), refusal(), refusal(), ScriptedTransport.ok()])
    inventory = answer(
        runner_record("zero-1", capacity_available=0),
        runner_record("unknown"),  # no capacity_available: unknown, tried as one free slot
        runner_record("zero-2", capacity_available=0),
        runner_record("roomy", capacity_available=3),
    )
    async with engine_offering(inventory, transport) as engine:
        result = await engine.jobs.run(ALICE, REQUEST.model_copy(update={"max_attempts": 4}))
    assert result.job.state == "succeeded"
    assert called(transport) == ["roomy", "unknown", "zero-1", "zero-2"]


async def test_a_capacity_that_is_not_a_whole_number_reads_as_zero_and_is_tried_last() -> None:
    transport = ScriptedTransport(steps=[refusal(), ScriptedTransport.ok()])
    inventory = answer(
        runner_record("unreadable", capacity_available="plenty"), runner_record("roomy", capacity_available=1)
    )
    async with engine_offering(inventory, transport) as engine:
        result = await engine.jobs.run(ALICE, REQUEST)
    assert (result.job.state, called(transport)) == ("succeeded", ["roomy", "unreadable"])


async def test_when_every_zero_capacity_runner_refuses_unpaid_the_job_fails_refused_with_503() -> None:
    transport = ScriptedTransport(steps=[refusal(), refusal()])
    inventory = answer(runner_record("a", capacity_available=0), runner_record("b", capacity_available=0))
    async with engine_offering(inventory, transport) as engine:
        with pytest.raises(JobFailed) as failed:
            await engine.jobs.run(ALICE, REQUEST)
        job = failed.value.job
        assert job.failure is not None
        assert (job.state, job.failure.kind, job.failure.payment_sent) == ("failed", "refused", False)
        assert [attempt.outcome for attempt in job.attempts] == ["refused", "refused"]

        transport.steps = [refusal(), refusal()]
        answered = await post_job(engine)
    assert (answered.status_code, answered.headers["retry-after"]) == (503, "5")
    assert answered.json()["job"]["failure"]["kind"] == "refused"


async def post_job(engine: BuilderEngine, **fields: Any) -> httpx.Response:
    """The same job over REST, with a key issued to alice."""
    key = await engine.keys.issue(OPERATOR, kind="actor", actor_id="alice", operation_ref="issue-alice")
    transport = httpx.ASGITransport(app=create_app(engine, manage_lifecycle=False))
    headers = {"Authorization": f"Bearer {key.token}"}
    async with httpx.AsyncClient(transport=transport, base_url="http://engine", headers=headers) as client:
        return await client.post("/v1/jobs", json={"app": APP, **fields})


# -- a job that would pay: the node asks for payment before it looks at capacity ----------------------


async def test_a_job_that_would_pay_is_not_tried_when_every_runner_advertises_zero_capacity() -> None:
    """Tried, it signed ``LPB_MAX_PAID_ATTEMPTS`` payments that bought nothing, and a repeat was a 409."""
    transport = ScriptedTransport(steps=[paid_refusal("m-1"), paid_refusal("m-2")])
    first = REQUEST.model_copy(update={"operation_ref": "order-1"})
    busy = answer(full_and_priced("a"), full_and_priced("b"), full_and_priced("c"))
    room = answer(full_and_priced("a"), runner_record("b", capacity_available=1, price_info=PRICED))
    async with engine_offering(busy, transport, then=room) as engine:
        with pytest.raises(JobFailed) as failed:
            await engine.jobs.run(ALICE, first)
        job = failed.value.job
        assert job.failure is not None
        assert (job.state, job.failure.kind, job.failure.payment_sent) == ("failed", "no_capacity", False)
        assert job.failure.message == "every runner for 'good/app' is busy"
        assert (job.attempts, transport.calls) == ((), [])  # no runner asked, so nothing signed

        answered = await post_job(engine, operation_ref="order-1")  # the same reference: no 409
        assert (answered.status_code, answered.headers["retry-after"]) == (503, "5")
        assert answered.json()["job"]["failure"]["kind"] == "no_capacity"
        assert transport.calls == []

        await engine.discovery.refresh()  # a runner has room now
        transport.steps = [ScriptedTransport.ok()]
        result = await engine.jobs.run(ALICE, first)
    assert (result.job.state, called(transport)) == ("succeeded", ["b"])


async def test_a_stream_that_would_pay_is_not_opened_when_every_runner_advertises_zero_capacity() -> None:
    transport = ScriptedStreamTransport(opens=[paid_refusal("m-1"), paid_refusal("m-2")])
    async with engine_offering(answer(full_and_priced("a"), full_and_priced("b")), transport) as engine:
        assert engine.jobs.streaming is not None
        with pytest.raises(JobFailed) as failed:
            await engine.jobs.streaming.stream(ALICE, REQUEST)
    job = failed.value.job
    assert job.failure is not None
    assert (job.failure.kind, job.failure.payment_sent, job.attempts) == ("no_capacity", False, ())
    assert transport.calls == []


@pytest.mark.parametrize(
    ("price_info", "pays"),
    [
        pytest.param(PRICED, False, id="no signer"),
        pytest.param(FREE, True, id="free"),
        pytest.param(USD_PER_HOUR, True, id="a price the signer does not pay"),
        pytest.param(None, True, id="no price"),
    ],
)
async def test_an_attempt_that_signs_nothing_is_made_when_every_runner_advertises_zero_capacity(
    price_info: dict[str, Any] | None, pays: bool
) -> None:
    """Advertised capacity is a hint that can lag, and asking costs nothing when nothing is signed."""
    transport = ScriptedTransport(steps=[refusal(), ScriptedTransport.ok(manifest_ids=())], pays=pays)
    inventory = answer(
        runner_record("a", capacity_available=0, price_info=price_info),
        runner_record("b", capacity_available=0, price_info=price_info),
    )
    async with engine_offering(inventory, transport) as engine:
        result = await engine.jobs.run(ALICE, REQUEST)
    assert (result.job.state, called(transport)) == ("succeeded", ["a", "b"])
    assert [attempt.payment_sent for attempt in result.job.attempts] == [False, False]


async def test_when_every_runner_advertises_zero_a_paying_job_tries_only_those_it_signs_nothing_for() -> None:
    transport = ScriptedTransport(steps=[refusal(), refusal()])
    inventory = answer(
        full_and_priced("priced-1"),
        runner_record("unpayable", capacity_available=0, price_info=USD_PER_HOUR),
        full_and_priced("priced-2"),
        runner_record("free", capacity_available=0, price_info=FREE),
    )
    async with engine_offering(inventory, transport) as engine:
        with pytest.raises(JobFailed) as failed:
            await engine.jobs.run(ALICE, REQUEST.model_copy(update={"max_attempts": 4}))
    job = failed.value.job
    assert job.failure is not None
    assert (job.failure.kind, job.failure.payment_sent) == ("refused", False)
    assert called(transport) == ["free", "unpayable"]  # the free one first, as on any paid job


async def test_a_paying_job_tries_a_priced_zero_capacity_runner_after_a_priced_runner_with_room() -> None:
    """One runner has room, so the job is tried as before: the runner advertising zero last, and paid."""
    transport = ScriptedTransport(steps=[refusal(), ScriptedTransport.ok()])
    inventory = answer(
        full_and_priced("zero"), runner_record("roomy", capacity_available=1, price_info=PRICED)
    )
    async with engine_offering(inventory, transport) as engine:
        result = await engine.jobs.run(ALICE, REQUEST)
    assert (result.job.state, called(transport)) == ("succeeded", ["roomy", "zero"])
    assert [attempt.payment_sent for attempt in result.job.attempts] == [False, True]


class RoomOnly:
    """An operator's SelectionPolicy that drops runners advertising no room."""

    def order(self, candidates: Sequence[Runner], request: JobRequest) -> Sequence[Runner]:
        del request
        return [runner for runner in candidates if runner.capacity_available != 0]


async def test_an_operator_who_wants_zero_capacity_runners_dropped_supplies_a_selection_policy() -> None:
    transport = ScriptedTransport()
    inventory = answer(runner_record("a", capacity_available=0))
    async with engine_offering(inventory, transport, selection=RoomOnly()) as engine:
        with pytest.raises(JobFailed) as failed:
            await engine.jobs.run(ALICE, REQUEST)
    assert failed.value.job.failure is not None and failed.value.job.failure.kind == "no_offering"
    assert transport.calls == []
