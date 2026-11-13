"""#114 C17: a runner that advertises zero capacity stays in selection, tried last.

Advertised capacity is a hint that can lag by a discovery interval and more. When every runner for an app
said zero, the job failed ``no_capacity`` before any attempt, though a runner might have had room by
then. Now the engine tries them anyway, after any runner with room. If each refuses, the job fails
``refused`` through the normal failover path: 503 and ``Retry-After: 5`` as before. ``no_capacity`` is
then only the engine's own admission limits.

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
from livepeer_builder.testing import ScriptedTransport
from tests.unit.discovery_wire import ALICE, record, wire_engine

APP = "good/app"
REQUEST = JobRequest(app=APP)


def runner_record(name: str, **fields: Any) -> dict[str, Any]:
    """A runner on its own orchestrator, so one refusal never excludes another runner."""
    return record(url=f"https://{name}.example/app", **fields)


def answer(*records: dict[str, Any]) -> bytes:
    """A discovery answer with one orchestrator entry per runner record."""
    entries = [{"address": item["url"].removesuffix("/app"), "runners": [item]} for item in records]
    return json.dumps(entries).encode()


def refusal() -> RunnerCallError:
    return RunnerCallError("refused", "HTTP 503 from the runner", payment_sent=False, status_code=503)


OPERATOR = ActorContext(actor_id="operator", credential_kind="admin")


@asynccontextmanager
async def engine_offering(
    inventory: bytes, transport: ScriptedTransport, **supply: Any
) -> AsyncIterator[BuilderEngine]:
    """An engine whose discovery answered ``inventory``, with alice provisioned to run jobs."""
    async with wire_engine(inventory, supply=EngineSupply(transport=transport, **supply)) as engine:
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


async def test_a_job_is_tried_when_every_runner_for_its_app_advertises_zero_capacity() -> None:
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


async def test_when_every_zero_capacity_runner_refuses_the_job_fails_refused_with_503_and_retry_after() -> (
    None
):
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


async def post_job(engine: BuilderEngine) -> httpx.Response:
    """The same job over REST, with a key issued to alice."""
    key = await engine.keys.issue(OPERATOR, kind="actor", actor_id="alice", operation_ref="issue-alice")
    transport = httpx.ASGITransport(app=create_app(engine, manage_lifecycle=False))
    headers = {"Authorization": f"Bearer {key.token}"}
    async with httpx.AsyncClient(transport=transport, base_url="http://engine", headers=headers) as client:
        return await client.post("/v1/jobs", json={"app": APP})


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
