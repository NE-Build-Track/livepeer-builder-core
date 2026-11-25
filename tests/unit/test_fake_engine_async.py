"""FakeEngine and the real engine honor the same contract for async jobs (ISS-26).

As ``test_fake_engine_contract.py``: each test runs once against ``FakeEngine`` and once against a real
engine from ``make_engine``, scripted to the same outcome. The engine's worker is driven with
``run_once()`` and the fake's with ``advance()``: neither runs a loop.
"""

import hashlib

import pytest

from livepeer_builder import ActorApi, ActorContext, BuilderEngine, JobFailed, JobRequest
from livepeer_builder.errors import (
    AccessDenied,
    InvalidRequest,
    JobNotFinished,
    JobNotQueued,
    NotFound,
    ResultExpired,
)
from livepeer_builder.testing import FakeEngine
from tests.unit.test_fake_engine_contract import ALICE, APP, BOB, NONE, Side, fake, real


@pytest.fixture(params=[fake, real], ids=["fake", "real"])
def side(request: pytest.FixtureRequest) -> Side:
    return request.param  # type: ignore[no-any-return]


def queued(**fields: object) -> JobRequest:
    return JobRequest.model_validate({"app": APP, "mode": "async", **fields})


async def test_submit_returns_a_queued_job_that_reads_back(side: Side) -> None:
    engine: ActorApi
    async with side() as engine:
        job = await engine.jobs.submit(ALICE, queued())
        assert (job.state, job.mode, job.attempts, job.result) == ("queued", "async", (), None)
        assert (job.started_at, job.completed_at) == (None, None)
        assert await engine.jobs.get(ALICE, job.id) == job
        assert [listed.id for listed in await engine.jobs.list_jobs(ALICE)] == [job.id]


async def advance(engine: ActorApi) -> int:
    """Run what is queued: the fake's own step, or one step of the engine's worker."""
    if isinstance(engine, FakeEngine):
        return await engine.jobs.advance()
    assert isinstance(engine, BuilderEngine)
    return await engine.jobs.worker.run_once()


async def test_a_queued_job_runs_when_the_queue_is_advanced_and_its_result_is_stored(side: Side) -> None:
    async with side(data={"answer": 42}) as engine:
        job = await engine.jobs.submit(ALICE, queued())
        assert await advance(engine) == 1
        ran = await engine.jobs.get(ALICE, job.id)
        assert (ran.state, ran.mode) == ("succeeded", "async")
        assert ran.started_at is not None and ran.completed_at is not None
        assert [(a.outcome, a.payment_sent, a.runner_url) for a in ran.attempts] == [
            ("succeeded", True, "https://orch.example/r1")
        ]
        assert ran.result is not None
        assert (ran.result.storage, ran.result.location) == ("stored", f"/v1/jobs/{job.id}/result")
        result = await engine.jobs.result(ALICE, job.id)
        assert (result.status_code, result.data, result.job) == (200, {"answer": 42}, ran)
        assert (ran.result.size_bytes, ran.result.sha256, ran.result.content_type) == (
            len(result.content),
            hashlib.sha256(result.content).hexdigest(),
            result.content_type,
        )
        assert await advance(engine) == 0  # nothing is run twice


async def test_a_job_that_has_not_ended_has_no_result_and_an_immediate_one_keeps_none(side: Side) -> None:
    async with side() as engine:
        waiting = await engine.jobs.submit(ALICE, queued())
        with pytest.raises(JobNotFinished) as not_yet:
            await engine.jobs.result(ALICE, waiting.id)
        assert not_yet.value.job.state == "queued"
        with pytest.raises(NotFound):  # another actor's job reads as a missing one (D44)
            await engine.jobs.result(BOB, waiting.id)

        ran = await engine.jobs.run(ALICE, JobRequest(app=APP))
        assert ran.job.mode == "immediate" and ran.job.result is not None
        assert ran.job.result.storage == "inline"
        with pytest.raises(ResultExpired) as gone:
            await engine.jobs.result(ALICE, ran.job.id)
        assert gone.value.job.id == ran.job.id


async def test_a_failed_async_job_is_read_as_its_failure(side: Side) -> None:
    async with side(fails=True) as engine:
        job = await engine.jobs.submit(ALICE, queued())
        assert await advance(engine) == 1
        failed = await engine.jobs.get(ALICE, job.id)
        assert failed.failure is not None
        assert (failed.state, failed.failure.kind, failed.result) == ("failed", "runner_error", None)
        with pytest.raises(JobFailed) as raised:
            await engine.jobs.result(ALICE, job.id)
        assert raised.value.job == failed


async def test_a_job_for_an_app_nobody_serves_is_queued_and_waits_for_a_runner(side: Side) -> None:
    """Nothing is discovered at submit, and a queued job is not failed because no runner is listed when
    a worker reaches it: it waits, up to the queue's time limit (which the fake does not model)."""
    async with side() as engine:
        job = await engine.jobs.submit(ALICE, queued(app=NONE))
        assert job.state == "queued"
        assert [await advance(engine), await advance(engine)] == [0, 0]
        waiting = await engine.jobs.get(ALICE, job.id)
        assert (waiting.state, waiting.failure, waiting.attempts) == ("queued", None, ())
        assert (await engine.costs.for_job(ALICE, job.id)).status == "pending"  # it may yet pay
        with pytest.raises(JobNotFinished):
            await engine.jobs.result(ALICE, job.id)


async def test_submit_and_run_each_refuse_the_other_mode(side: Side) -> None:
    def without(permission: str) -> ActorContext:
        return ALICE.model_copy(update={"permissions": ALICE.permissions - {permission}})

    async with side() as engine:
        with pytest.raises(InvalidRequest):
            await engine.jobs.run(ALICE, queued())
        with pytest.raises(InvalidRequest):
            await engine.jobs.submit(ALICE, JobRequest(app=APP))
        with pytest.raises(InvalidRequest):
            await engine.jobs.submit(ALICE, queued(return_http_errors=True))
        with pytest.raises(InvalidRequest):
            await engine.jobs.submit(ALICE, queued(path="run"))
        with pytest.raises(AccessDenied):  # the permission first, as for run
            await engine.jobs.submit(without("jobs.run"), queued(path="run"))
        assert await engine.jobs.list_jobs(ALICE) == []


async def test_a_queued_job_is_cancelled_once_and_never_runs(side: Side) -> None:
    async with side() as engine:
        job = await engine.jobs.submit(ALICE, queued())
        with pytest.raises(NotFound):
            await engine.jobs.cancel(BOB, job.id)
        cancelled = await engine.jobs.cancel(ALICE, job.id)
        assert cancelled.failure is not None
        assert (cancelled.state, cancelled.failure.kind, cancelled.failure.reason) == (
            "failed",
            "abandoned",
            "cancelled",
        )
        assert (cancelled.attempts, cancelled.failure.payment_sent) == ((), False)
        assert cancelled.completed_at is not None
        assert await engine.jobs.cancel(ALICE, job.id) == cancelled  # again: the same answer
        assert await advance(engine) == 0
        assert await engine.jobs.get(ALICE, job.id) == cancelled
        assert (await engine.costs.for_job(ALICE, job.id)).status == "none"  # nothing was paid

        ran = await engine.jobs.submit(ALICE, queued())
        await advance(engine)
        with pytest.raises(JobNotQueued) as ended:
            await engine.jobs.cancel(ALICE, ran.id)
        assert ended.value.job.state == "succeeded"
        immediate = await engine.jobs.run(ALICE, JobRequest(app=APP))
        with pytest.raises(JobNotQueued):
            await engine.jobs.cancel(ALICE, immediate.job.id)


async def test_a_queued_job_costs_pending_and_never_none(side: Side) -> None:
    async with side() as engine:
        job = await engine.jobs.submit(ALICE, queued())
        cost = await engine.costs.for_job(ALICE, job.id)
        assert (cost.status, cost.attempts) == ("pending", ())
        await advance(engine)
        assert (await engine.costs.for_job(ALICE, job.id)).status == "pending"  # paid, usage not here yet
