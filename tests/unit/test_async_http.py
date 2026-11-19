"""ISS-26 over REST: one test per answer.

``POST /v1/jobs`` with ``mode="async"`` answers 202 with the queued job; the job and its result are read
back by id; ``POST /v1/jobs/{id}/cancel`` cancels a job that is still queued
(docs/contracts/engine.md#async-jobs).
"""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx
import pytest

from livepeer_builder import BuilderEngine
from livepeer_builder.contracts import ActorContext
from livepeer_builder.http import create_app
from livepeer_builder.http.replies import JOB_ID_HEADER
from livepeer_builder.testing import ScriptedTransport
from tests.conftest import EngineFactory, allot
from tests.unit.test_async_jobs import APP, calls, echo

SUBMIT: dict[str, Any] = {"app": APP, "mode": "async", "payload": {"n": 1}}


@dataclass
class Service:
    client: httpx.AsyncClient
    engine: BuilderEngine
    transport: ScriptedTransport
    alice: dict[str, str]
    bob: dict[str, str]

    async def submit(self, **fields: Any) -> httpx.Response:
        return await self.client.post("/v1/jobs", json=SUBMIT | fields, headers=self.alice)

    async def job(self, job_id: str) -> httpx.Response:
        return await self.client.get(f"/v1/jobs/{job_id}", headers=self.alice)

    async def result(self, job_id: str) -> httpx.Response:
        return await self.client.get(f"/v1/jobs/{job_id}/result", headers=self.alice)

    async def cancel(self, job_id: str, headers: dict[str, str] | None = None) -> httpx.Response:
        return await self.client.post(f"/v1/jobs/{job_id}/cancel", headers=headers or self.alice)


@pytest.fixture
async def service(
    make_engine: EngineFactory,
    admin: ActorContext,
    alice: ActorContext,
    bob: ActorContext,
    transport: ScriptedTransport,
) -> AsyncIterator[Service]:
    engine = await make_engine(async_max_queued=3)
    await allot(engine, alice, bob)
    assert engine.keys is not None
    headers = []
    for actor in ("alice", "bob"):
        key = await engine.keys.issue(admin, kind="actor", actor_id=actor, operation_ref=f"key-{actor}")
        headers.append({"Authorization": f"Bearer {key.token}"})
    app = create_app(engine, manage_lifecycle=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://engine") as client:
        yield Service(client, engine, transport, *headers)


async def test_an_async_submit_is_202_with_the_queued_job_and_where_to_read_it(service: Service) -> None:
    accepted = await service.submit()
    assert accepted.status_code == 202
    job = accepted.json()
    assert (job["state"], job["mode"], job["attempts"], job["result"]) == ("queued", "async", [], None)
    assert accepted.headers["location"] == f"/v1/jobs/{job['id']}"
    assert accepted.headers[JOB_ID_HEADER] == job["id"]
    assert "idempotent-replayed" not in accepted.headers
    assert service.transport.calls == []  # nothing ran


async def test_a_job_read_carries_retry_after_until_it_has_ended(service: Service) -> None:
    job_id = (await service.submit()).json()["id"]
    waiting = await service.job(job_id)
    assert (waiting.status_code, waiting.json()["state"], waiting.headers["retry-after"]) == (
        200,
        "queued",
        "1",
    )

    service.transport.steps = [echo]
    service.transport.gate = asyncio.Event()
    step = asyncio.create_task(service.engine.jobs.worker.run_once())
    await calls(service.transport, 1)
    running = await service.job(job_id)
    assert (running.json()["state"], running.headers["retry-after"]) == ("running", "1")
    service.transport.gate.set()
    await step

    ended = await service.job(job_id)
    assert ended.json()["state"] == "succeeded" and "retry-after" not in ended.headers
    assert ended.json()["result"]["location"] == f"/v1/jobs/{job_id}/result"


async def test_the_result_route_answers_the_stored_bytes_with_their_type(service: Service) -> None:
    job_id = (await service.submit()).json()["id"]
    not_yet = await service.result(job_id)
    assert (not_yet.status_code, not_yet.headers["retry-after"]) == (409, "1")
    body = not_yet.json()
    assert (body["code"], body["job"]["state"]) == ("job_not_finished", "queued")
    assert not_yet.headers[JOB_ID_HEADER] == job_id

    service.transport.steps = [echo]
    await service.engine.jobs.worker.run_once()
    stored = await service.result(job_id)
    assert (stored.status_code, stored.content, stored.headers["content-type"]) == (
        200,
        b'{"n": 1}',
        "application/json",
    )
    # another actor's job, and a missing one, read the same: 404 (D44)
    other = await service.client.get(f"/v1/jobs/{job_id}/result", headers=service.bob)
    assert other.status_code == 404


async def test_the_result_of_an_immediate_job_and_of_an_expired_one_is_410(service: Service) -> None:
    service.transport.steps = [ScriptedTransport.ok(), echo]
    ran = await service.client.post("/v1/jobs", json={"app": APP}, headers=service.alice)
    never_kept = await service.result(ran.headers[JOB_ID_HEADER])
    assert never_kept.status_code == 410
    assert (never_kept.json()["code"], never_kept.json()["job"]["state"]) == ("result_expired", "succeeded")
    assert never_kept.headers[JOB_ID_HEADER] == ran.headers[JOB_ID_HEADER]

    job_id = (await service.submit()).json()["id"]
    await service.engine.jobs.worker.run_once()
    service.engine.clock.advance(7 * 86400)  # type: ignore[attr-defined]
    expired = await service.result(job_id)
    assert (expired.status_code, expired.json()["code"]) == (410, "result_expired")
    assert expired.json()["job"]["result"]["sha256"]  # the record still says what it was


async def test_the_result_of_a_failed_job_is_the_failure_run_would_have_answered(service: Service) -> None:
    from livepeer_builder.errors import RunnerCallError

    service.transport.steps = [RunnerCallError("http", "bad input", payment_sent=True, status_code=422)]
    job_id = (await service.submit()).json()["id"]
    await service.engine.jobs.worker.run_once()
    failed = await service.result(job_id)
    assert (failed.status_code, failed.json()["job"]["failure"]["kind"]) == (422, "runner_rejected")
    assert failed.headers[JOB_ID_HEADER] == job_id


async def test_a_reply_the_engine_could_not_store_is_502(
    service: Service, make_engine: EngineFactory
) -> None:
    service.engine.jobs.worker.limits = service.engine.jobs.worker.limits.__class__(result_max_bytes=4)
    service.transport.steps = [echo]
    job_id = (await service.submit()).json()["id"]
    await service.engine.jobs.worker.run_once()
    failed = await service.result(job_id)
    assert (failed.status_code, failed.json()["job"]["failure"]["kind"]) == (502, "result_not_stored")
    assert failed.json()["job"]["result"]["location"] is None


async def test_a_full_queue_is_503_with_retry_after(service: Service) -> None:
    for n in range(3):
        assert (await service.submit(payload={"n": n})).status_code == 202
    full = await service.submit(payload={"n": 3})
    assert (full.status_code, full.headers["retry-after"]) == (503, "5")
    assert len((await service.client.get("/v1/jobs", headers=service.alice)).json()) == 3


async def test_an_async_job_cannot_be_streamed_or_return_http_errors(service: Service) -> None:
    streamed = await service.client.post(
        "/v1/jobs", params={"stream": "true"}, json=SUBMIT, headers=service.alice
    )
    assert streamed.status_code == 400 and "stream" in streamed.json()["error"]
    by_accept = await service.client.post(
        "/v1/jobs", json=SUBMIT, headers=service.alice | {"Accept": "text/event-stream"}
    )
    assert by_accept.status_code == 400
    assert (await service.submit(return_http_errors=True)).status_code == 400
    assert (await service.client.get("/v1/jobs", headers=service.alice)).json() == []


async def test_a_cancel_ends_a_queued_job_and_only_a_queued_one(service: Service) -> None:
    job_id = (await service.submit()).json()["id"]
    assert (await service.cancel(job_id, service.bob)).status_code == 404  # another actor's job (D44)

    cancelled = await service.cancel(job_id)
    assert cancelled.status_code == 200
    job = cancelled.json()
    assert (job["state"], job["failure"]["kind"], job["failure"]["reason"]) == (
        "failed",
        "abandoned",
        "cancelled",
    )
    assert (job["failure"]["payment_sent"], job["attempts"]) == (False, [])
    assert (await service.cancel(job_id)).json() == job  # again: the same answer, nothing written
    assert await service.engine.jobs.worker.run_once() == 0  # and it never runs
    cost = await service.client.get(f"/v1/jobs/{job_id}/cost", headers=service.alice)
    assert (cost.json()["status"], cost.json()["attempts"]) == ("none", [])
    gone = await service.result(job_id)
    assert (gone.status_code, gone.json()["job"]["failure"]["reason"]) == (410, "cancelled")

    service.transport.steps = [echo, ScriptedTransport.ok()]
    service.transport.gate = asyncio.Event()
    running_id = (await service.submit()).json()["id"]
    step = asyncio.create_task(service.engine.jobs.worker.run_once())
    await calls(service.transport, 1)
    refused = await service.cancel(running_id)
    assert refused.status_code == 409
    assert (refused.json()["code"], refused.json()["job"]["state"]) == ("job_not_queued", "running")
    assert refused.headers[JOB_ID_HEADER] == running_id
    service.transport.gate.set()
    await step
    assert (await service.cancel(running_id)).status_code == 409  # it succeeded: nothing to cancel
    assert (await service.job(running_id)).json()["state"] == "succeeded"

    service.transport.gate = None
    immediate = await service.client.post("/v1/jobs", json={"app": APP}, headers=service.alice)
    assert (await service.cancel(immediate.headers[JOB_ID_HEADER])).status_code == 409


async def test_readyz_reports_the_queue(service: Service) -> None:
    await service.submit()
    service.engine.clock.advance(12)  # type: ignore[attr-defined]
    await service.submit(payload={"n": 2})
    health = (await service.client.get("/readyz")).json()
    assert (health["queued_jobs"], health["oldest_queued_s"], health["running_jobs"]) == (2, 12.0, 0)
