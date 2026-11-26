"""ISS-26: what a repeated async submit gets, and what a queued job is checked for and when.

A repeated ``operation_ref`` returns the job it names, in any state (D69 (1)): an async job is read back
by its id, so the repeat is handed the job it already has. A changed request is still a conflict, and
so is the same reference used once for an immediate job and once for an async one. A job that failed
without ever reaching a runner is queued again under its reference (D57). Authorization is decided at
submit and payment at dispatch (D69 (8)).
"""

import asyncio
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select

from livepeer_builder.contracts import OPERATION_REPLAYED, ActorContext, Job, JobRequest
from livepeer_builder.errors import (
    JobFailed,
    OperationConflict,
    OperationExists,
    OperationInProgress,
    RunnerCallError,
)
from livepeer_builder.http import create_app
from livepeer_builder.http.replies import JOB_ID_HEADER
from livepeer_builder.store import SqlStore
from livepeer_builder.store import schema as t
from livepeer_builder.testing import FixedClock, ScriptedTransport, runner
from tests.conftest import EngineFactory, allot
from tests.unit.test_async_jobs import APP, calls, echo, queued
from tests.unit.test_jobs import ORCH_A, ORCH_B, two_orchestrators

REF = "order-1"


async def links(store: SqlStore, job_id: UUID) -> tuple[str | None, str | None]:
    """(retry_of, superseded_by) of a job, as the store holds them."""
    async with store.engine.connect() as conn:
        query = select(t.jobs.c.retry_of, t.jobs.c.superseded_by).where(t.jobs.c.id == str(job_id))
        retry_of, superseded_by = (await conn.execute(query)).one()
    return retry_of, superseded_by


async def test_a_repeated_submit_returns_the_same_job_in_any_state(
    make_engine: EngineFactory, alice: ActorContext, transport: ScriptedTransport
) -> None:
    engine = await make_engine()
    await allot(engine, alice)
    request = queued(operation_ref=REF)
    first = await engine.jobs.submit(alice, request)
    assert OPERATION_REPLAYED.get() is False

    again = await engine.jobs.submit(alice, request)  # still queued
    assert again == first and OPERATION_REPLAYED.get() is True
    # limits may differ between repeats, as for an immediate job
    assert await engine.jobs.submit(alice, queued(operation_ref=REF, timeout_s=30, max_attempts=1)) == first

    transport.steps = [echo]
    transport.gate = asyncio.Event()
    step = asyncio.create_task(engine.jobs.worker.run_once())
    await calls(transport, 1)
    running = await engine.jobs.submit(alice, request)
    assert (running.id, running.state) == (first.id, "running")
    transport.gate.set()
    await step

    done = await engine.jobs.submit(alice, request)
    assert (done.id, done.state) == (first.id, "succeeded") and OPERATION_REPLAYED.get() is True
    assert done.result is not None and done.result.location == f"/v1/jobs/{first.id}/result"
    assert await engine.jobs.worker.run_once() == 0  # nothing was queued by the repeats
    assert len(transport.calls) == 1  # one dispatch in all
    assert [job.id for job in await engine.jobs.list_jobs(alice)] == [first.id]

    assert (await engine.jobs.submit(alice, queued(operation_ref="order-2"))).id != first.id
    assert OPERATION_REPLAYED.get() is False  # a new reference is a new job, and says so


@pytest.mark.parametrize(
    ("error", "state"),
    [
        (RunnerCallError("http", "boom", payment_sent=True, status_code=500), "failed"),
        (RunnerCallError("timeout", "slow", payment_sent=True, manifest_ids=("m-1",)), "uncertain"),
        (RunnerCallError("unreachable", "cut", payment_sent=False), "failed"),  # it may have run (D57)
    ],
)
async def test_a_repeat_after_a_job_that_reached_a_runner_returns_that_job_and_runs_nothing(
    make_engine: EngineFactory,
    alice: ActorContext,
    transport: ScriptedTransport,
    error: RunnerCallError,
    state: str,
) -> None:
    engine = await make_engine()
    await allot(engine, alice)
    first = await engine.jobs.submit(alice, queued(operation_ref=REF, max_attempts=1))
    transport.steps = [error]
    await engine.jobs.worker.run_once()

    again = await engine.jobs.submit(alice, queued(operation_ref=REF, max_attempts=1))
    assert (again.id, again.state) == (first.id, state) and OPERATION_REPLAYED.get() is True
    assert await engine.jobs.worker.run_once() == 0
    assert len(transport.calls) == 1


async def test_a_changed_request_under_the_same_reference_is_a_conflict(
    make_engine: EngineFactory, alice: ActorContext, transport: ScriptedTransport
) -> None:
    engine = await make_engine()
    await allot(engine, alice)
    first = await engine.jobs.submit(alice, queued(operation_ref=REF))
    with pytest.raises(OperationConflict) as conflict:
        await engine.jobs.submit(alice, queued(operation_ref=REF, payload={"x": 2}))
    assert conflict.value.job is not None and conflict.value.job.id == first.id
    assert [job.id for job in await engine.jobs.list_jobs(alice)] == [first.id]


async def test_one_reference_is_an_immediate_job_or_an_async_one_never_both(
    make_engine: EngineFactory, alice: ActorContext, transport: ScriptedTransport
) -> None:
    """The fingerprint carries the mode when it is async, so the two are different requests."""
    engine = await make_engine()
    await allot(engine, alice)
    transport.steps = [ScriptedTransport.ok()]
    immediate = JobRequest(app=APP, payload={"x": 1}, operation_ref=REF)
    ran = await engine.jobs.run(alice, immediate)
    with pytest.raises(OperationConflict) as conflict:
        await engine.jobs.submit(alice, queued(operation_ref=REF))
    assert conflict.value.job is not None and conflict.value.job.id == ran.job.id

    waiting = await engine.jobs.submit(alice, queued(operation_ref="order-2"))
    with pytest.raises(OperationConflict) as other:
        await engine.jobs.run(alice, immediate.model_copy(update={"operation_ref": "order-2"}))
    assert other.value.job is not None and other.value.job.id == waiting.id
    assert len(transport.calls) == 1


async def test_an_immediate_job_stored_before_fingerprints_is_never_answered_to_an_async_repeat(
    make_engine: EngineFactory, alice: ActorContext, transport: ScriptedTransport, store: SqlStore
) -> None:
    """A job from before fingerprints matches any request (D57). It is still an immediate job, with no
    stored result to read: an async submit under its reference is a conflict, not that job."""
    engine = await make_engine()
    await allot(engine, alice)
    transport.steps = [ScriptedTransport.ok()]
    ran = await engine.jobs.run(alice, JobRequest(app=APP, payload={"x": 1}, operation_ref=REF))
    async with store.engine.begin() as conn:
        await conn.execute(t.jobs.update().values(request_fingerprint=None))
    with pytest.raises(OperationConflict) as conflict:
        await engine.jobs.submit(alice, queued(operation_ref=REF))
    assert conflict.value.job is not None and conflict.value.job.id == ran.job.id


async def test_a_job_that_never_reached_a_runner_is_queued_again_under_its_reference(
    make_engine: EngineFactory,
    alice: ActorContext,
    transport: ScriptedTransport,
    clock: FixedClock,
    store: SqlStore,
) -> None:
    """The three ways a queued job ends without a runner: the queue's time limit, a cancel, and an
    orchestrator that refuses it. Each frees the reference for a new queued job (D57)."""
    engine = await make_engine(async_queue_ttl_s=60)
    await allot(engine, alice)
    request = queued(operation_ref=REF)

    expired = await engine.jobs.submit(alice, request)
    clock.advance(61)
    assert await engine.jobs.worker.run_once() == 0
    second = await engine.jobs.submit(alice, request)
    assert (second.state, second.id != expired.id, OPERATION_REPLAYED.get()) == ("queued", True, False)
    assert await links(store, second.id) == (str(expired.id), None)
    assert await links(store, expired.id) == (None, str(second.id))

    await engine.jobs.cancel(alice, second.id)
    third = await engine.jobs.submit(alice, request)
    assert third.state == "queued" and await links(store, third.id) == (str(second.id), None)

    transport.steps = [RunnerCallError("refused", "busy", payment_sent=False)]
    await engine.jobs.worker.run_once()
    assert (await engine.jobs.get(alice, third.id)).state == "failed"
    fourth = await engine.jobs.submit(alice, request)
    assert fourth.state == "queued" and await links(store, fourth.id) == (str(third.id), None)

    transport.steps = [echo]
    assert await engine.jobs.worker.run_once() == 1
    assert (await engine.jobs.submit(alice, request)).state == "succeeded"  # the holder now ran
    assert len(transport.calls) == 2  # the refusal and the run
    with pytest.raises(OperationConflict):  # a changed request is refused before any retry
        await engine.jobs.submit(alice, queued(operation_ref=REF, payload={"x": 9}))


async def test_two_submits_of_one_reference_at_the_same_moment_make_one_job(
    make_engine: EngineFactory, alice: ActorContext, store: SqlStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both look, and neither sees the reference held. They meet at the unique index: the loser is
    answered with the winner's job, or with a conflict when its request is another one."""
    engine = await make_engine()
    await allot(engine, alice)
    winner = await engine.jobs.submit(alice, queued(operation_ref=REF))
    found = store.job_by_operation_ref
    looked = 0

    async def missed(*args: object) -> tuple[Job, str | None] | None:
        nonlocal looked
        looked += 1
        return None if looked == 1 else await found(*args)  # type: ignore[arg-type]

    monkeypatch.setattr(store, "job_by_operation_ref", missed)
    loser = await engine.jobs.submit(alice, queued(operation_ref=REF))
    assert loser == winner and OPERATION_REPLAYED.get() is True

    looked = 0
    with pytest.raises(OperationConflict):
        await engine.jobs.submit(alice, queued(operation_ref=REF, payload={"x": 2}))
    assert [job.id for job in await engine.jobs.list_jobs(alice)] == [winner.id]
    assert await engine.store.queued_stats() == (1, winner.created_at)


async def test_a_reference_other_submits_keep_taking_is_in_progress(
    make_engine: EngineFactory, alice: ActorContext, store: SqlStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bounded, as for an immediate job: a reference that other requests keep re-queueing is busy."""
    engine = await make_engine()
    await allot(engine, alice)
    held = await engine.jobs.submit(alice, queued(operation_ref=REF))
    cancelled = await engine.jobs.cancel(alice, held.id)

    async def always_taken(*_: object, **__: object) -> None:
        raise OperationExists(cancelled)  # another request superseded it first, every time

    monkeypatch.setattr(store, "enqueue_job", always_taken)
    with pytest.raises(OperationInProgress):
        await engine.jobs.submit(alice, queued(operation_ref=REF))


# -- what is checked at submit, and what at dispatch ----------------------------------------------------


async def test_an_actor_with_no_allocation_fails_at_submit_as_run_fails_it(
    make_engine: EngineFactory, alice: ActorContext, transport: ScriptedTransport, store: SqlStore
) -> None:
    engine = await make_engine()
    with pytest.raises(JobFailed) as failed:
        await engine.jobs.submit(alice, queued(operation_ref=REF))
    job = failed.value.job
    assert job.failure is not None
    assert (job.state, job.mode, job.failure.kind, job.attempts) == ("failed", "async", "not_provisioned", ())
    assert await engine.jobs.get(alice, job.id) == job  # recorded, as run records it
    assert await engine.store.queued_stats() == (0, None)  # and never queued
    assert await engine.jobs.worker.run_once() == 0

    await allot(engine, alice)  # an admin allots; the same reference is then queued (D57)
    queued_now = await engine.jobs.submit(alice, queued(operation_ref=REF))
    assert queued_now.state == "queued" and await links(store, queued_now.id) == (str(job.id), None)
    transport.steps = [echo]
    assert await engine.jobs.worker.run_once() == 1


async def test_an_allocation_revoked_while_the_job_waited_fails_it_unpaid_at_dispatch(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, transport: ScriptedTransport
) -> None:
    engine = await make_engine()
    await allot(engine, alice)
    job = await engine.jobs.submit(alice, queued())
    await engine.payments.revoke(admin, "default", "alice")

    assert await engine.jobs.worker.run_once() == 1  # claimed, and failed before any runner was called
    failed = await engine.jobs.get(alice, job.id)
    assert failed.failure is not None
    assert (failed.state, failed.failure.kind, failed.failure.payment_sent, failed.attempts) == (
        "failed",
        "not_provisioned",
        False,
        (),
    )
    assert transport.calls == []
    assert (await engine.costs.for_job(alice, job.id)).status == "none"


async def test_an_allocation_spent_while_the_job_waited_fails_it_with_the_signers_reason(
    make_engine: EngineFactory, alice: ActorContext, transport: ScriptedTransport
) -> None:
    engine = await make_engine()
    await allot(engine, alice)
    job = await engine.jobs.submit(alice, queued())
    transport.steps = [
        RunnerCallError("payment", "402", payment_sent=False, status_code=402, reason="allowance_exhausted")
    ]
    await engine.jobs.worker.run_once()
    failed = await engine.jobs.get(alice, job.id)
    assert failed.failure is not None
    assert (failed.state, failed.failure.kind, failed.failure.reason, failed.failure.payment_sent) == (
        "failed",
        "payment",
        "allowance_exhausted",
        False,
    )


async def test_a_job_runs_as_the_actor_it_was_submitted_by_after_its_access_key_is_revoked(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, transport: ScriptedTransport
) -> None:
    """D69 (8): the job was authorized at submit and belongs to the actor, not to the key."""
    engine = await make_engine()
    await allot(engine, alice)
    assert engine.keys is not None
    key = await engine.keys.issue(admin, kind="actor", actor_id="alice", operation_ref="key-alice")
    headers = {"Authorization": f"Bearer {key.token}"}
    app = create_app(engine, manage_lifecycle=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://engine") as client:
        accepted = await client.post("/v1/jobs", json={"app": APP, "mode": "async"}, headers=headers)
        assert accepted.status_code == 202
        job_id = UUID(accepted.headers[JOB_ID_HEADER])
        await engine.keys.revoke(admin, key.key.key_id)
        assert (await client.get(f"/v1/jobs/{job_id}", headers=headers)).status_code == 401

    transport.steps = [ScriptedTransport.ok()]
    assert await engine.jobs.worker.run_once() == 1
    ran = await engine.jobs.get(alice, job_id)
    assert (ran.state, ran.actor_id) == ("succeeded", "alice")
    assert transport.calls[0][1] is not None and transport.calls[0][1].startswith("lpg_")  # alice's own key


async def test_a_repeated_submit_over_rest_is_200_and_says_it_was_replayed(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext
) -> None:
    engine = await make_engine()
    await allot(engine, alice)
    assert engine.keys is not None
    key = await engine.keys.issue(admin, kind="actor", actor_id="alice", operation_ref="key-alice")
    headers = {"Authorization": f"Bearer {key.token}"}
    body = {"app": APP, "mode": "async", "payload": {"x": 1}, "operation_ref": REF}
    app = create_app(engine, manage_lifecycle=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://engine") as client:
        first = await client.post("/v1/jobs", json=body, headers=headers)
        assert first.status_code == 202 and "idempotent-replayed" not in first.headers
        again = await client.post("/v1/jobs", json=body, headers=headers)
        assert (again.status_code, again.headers["idempotent-replayed"]) == (200, "true")
        assert again.json() == first.json()
        assert again.headers["location"] == first.headers["location"]
        assert again.headers[JOB_ID_HEADER] == first.json()["id"]

        changed = await client.post("/v1/jobs", json=body | {"payload": {"x": 2}}, headers=headers)
        assert (changed.status_code, changed.json()["code"]) == (409, "operation_conflict")
        immediate = await client.post("/v1/jobs", json=body | {"mode": "immediate"}, headers=headers)
        assert (immediate.status_code, immediate.json()["code"]) == (409, "operation_conflict")


# -- the paid call is the one run makes: one representative test for each rule -------------------------


async def test_an_async_job_fails_over_and_never_pays_the_same_orchestrator_twice(
    make_engine: EngineFactory, alice: ActorContext, transport: ScriptedTransport
) -> None:
    """D52, as ``test_a_paid_failure_that_did_not_run_fails_over_to_another_orchestrator`` for run."""
    engine = await make_engine(two_orchestrators())
    await allot(engine, alice)
    job = await engine.jobs.submit(alice, queued(max_attempts=2))
    transport.steps = [
        RunnerCallError("http", "refused", payment_sent=True, manifest_ids=("m-1",), status_code=404),
        ScriptedTransport.ok(manifest_ids=("m-2",)),
    ]
    await engine.jobs.worker.run_once()
    ran = await engine.jobs.get(alice, job.id)
    assert ran.state == "succeeded"
    assert [url for url, _ in transport.calls] == [f"{ORCH_A}/a1", f"{ORCH_B}/b1"]  # a2: same orchestrator
    assert [(a.outcome, a.payment_sent, a.orchestrator_url) for a in ran.attempts] == [
        ("http", True, ORCH_A),
        ("succeeded", True, ORCH_B),
    ]
    assert (await engine.jobs.result(alice, job.id)).content == b'{"ok": true}'


async def test_an_async_job_stops_at_the_paid_attempt_cap(
    make_engine: EngineFactory, alice: ActorContext, transport: ScriptedTransport
) -> None:
    """D55: with the cap at 1, a paid attempt that fails over is the last one."""
    engine = await make_engine(
        (
            runner(url=f"{ORCH_A}/a1", orchestrator_url=ORCH_A),
            runner(url=f"{ORCH_B}/b1", orchestrator_url=ORCH_B),
        ),
        max_paid_attempts=1,
    )
    await allot(engine, alice)
    job = await engine.jobs.submit(alice, queued())
    transport.steps = [
        RunnerCallError("http", "refused", payment_sent=True, manifest_ids=("m-1",), status_code=404),
        ScriptedTransport.ok(manifest_ids=("m-2",)),
    ]
    await engine.jobs.worker.run_once()
    failed = await engine.jobs.get(alice, job.id)
    assert failed.failure is not None
    assert (failed.state, failed.failure.kind, failed.failure.payment_sent) == ("failed", "refused", True)
    assert len(failed.attempts) == 1 and len(transport.calls) == 1  # no second payment
