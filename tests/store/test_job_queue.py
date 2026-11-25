"""ISS-26: the queue of async jobs in the store, identical on SQLite and PostgreSQL.

``lpb_jobs`` is the queue. A job waits ``queued`` with its request and its actor beside it, a worker
claims it with a compare-and-set, and the transaction that ends it writes its state, its result and the
deletion of the stored request together (docs/design-docs/m4-guides/iss-26-async-jobs.md).
"""

import asyncio
import hashlib
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, insert, select
from sqlalchemy.exc import IntegrityError

from livepeer_builder.contracts import ActorContext, AuthProvenance, Failure, Job, JobRequest, ResultRef
from livepeer_builder.errors import NoCapacity, OperationConflict, OperationExists
from livepeer_builder.store import SqlStore
from livepeer_builder.store import schema as t
from livepeer_builder.testing import FixedClock
from tests.store.conftest import make_attempt, make_job

ALICE = ActorContext(
    actor_id="alice",
    tenant_id="acme",
    permissions=frozenset({"jobs.run", "jobs.read"}),
    attributes={"plan": "pro"},
    auth=AuthProvenance(method="api_key", credential_id="key_1"),
    display_name="Alice Example",
)
REQUEST = JobRequest(app="a/b", mode="async", path="/run", payload={"x": 1}, timeout_s=30)
CANCELLED = Failure(kind="abandoned", message="cancelled before it ran", reason="cancelled")
CONTENT = b'{"image": "..."}'


@pytest.fixture
async def sql(database_url: str) -> AsyncIterator[SqlStore]:
    store = await SqlStore.connect(database_url)
    yield store
    await store.close()


def waiting(clock: FixedClock, *, app: str = "a/b", ref: str | None = None) -> Job:
    job = make_job(clock, actor="alice", ref=ref, state="queued")
    return job.model_copy(update={"mode": "async", "tenant_id": "acme", "app": app, "completed_at": None})


async def enqueued(
    sql: SqlStore, clock: FixedClock, *, app: str = "a/b", request: JobRequest = REQUEST
) -> Job:
    job = waiting(clock, app=app)
    await sql.enqueue_job(job, "fp", request, ALICE)
    clock.advance(1)  # each job is older than the next
    return job


async def claimed(sql: SqlStore, clock: FixedClock) -> Job:
    job = await enqueued(sql, clock)
    taken = await sql.claim_job(job.id, clock.now())
    assert taken is not None
    return taken[0]


async def count(sql: SqlStore, table: object, job_id: UUID) -> int:
    async with sql.engine.connect() as conn:
        found = await conn.execute(
            select(func.count()).select_from(table).where(table.c.job_id == str(job_id))  # type: ignore[attr-defined, arg-type]
        )
        return int(found.scalar_one())


def stored_ref(content: bytes, expires_at: datetime | None) -> ResultRef:
    return ResultRef(
        storage="stored",
        content_type="application/json; charset=utf-8",
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        location="/v1/jobs/x/result",
        expires_at=expires_at,
    )


# -- enqueue and claim ----------------------------------------------------------------------------------


async def test_a_queued_job_keeps_its_request_and_the_actor_it_runs_as(
    sql: SqlStore, clock: FixedClock
) -> None:
    job = await enqueued(sql, clock)
    assert await sql.get_job(job.id) == job
    assert await sql.queued_jobs(10) == [(job.id, "a/b")]

    taken = await sql.claim_job(job.id, clock.now())
    assert taken is not None
    running, request, actor, number = taken
    assert number == 1  # the claim's number: what the worker's later writes for the job carry
    assert (running.state, running.started_at, running.mode) == ("running", clock.now(), "async")
    # the request as it was sent: what was not set is still not set, so its checks read the same
    assert request == REQUEST and request.model_fields_set == REQUEST.model_fields_set
    # who it runs as, and never how they signed in or what they are called
    assert actor == ALICE.model_copy(update={"auth": None, "display_name": None})


async def test_a_raw_body_is_stored_as_bytes_and_read_back_unchanged(
    sql: SqlStore, clock: FixedClock
) -> None:
    body = bytes(range(256)) * 64
    request = JobRequest(
        app="a/b", mode="async", raw_body=body, content_type="application/octet-stream", headers=(("x-a", "1"),)
    )  # fmt: skip
    job = await enqueued(sql, clock, request=request)
    async with sql.engine.connect() as conn:
        row = (await conn.execute(select(t.job_requests))).mappings().one()
    assert bytes(row["raw_body"]) == body and "raw_body" not in row["request"]
    taken = await sql.claim_job(job.id, clock.now())
    assert taken is not None and taken[1] == request


async def test_the_claim_is_a_compare_and_set_and_one_of_two_wins(sql: SqlStore, clock: FixedClock) -> None:
    job = await enqueued(sql, clock)
    first, second = await asyncio.gather(
        sql.claim_job(job.id, clock.now()), sql.claim_job(job.id, clock.now())
    )
    assert [first is None, second is None].count(True) == 1
    assert await sql.claim_job(job.id, clock.now()) is None  # running: nothing to claim
    assert await sql.claim_job(uuid4(), clock.now()) is None  # no such job
    async with sql.engine.connect() as conn:
        claims = await conn.execute(select(t.jobs.c.claims).where(t.jobs.c.id == str(job.id)))
        assert claims.scalar_one() == 1
    assert await sql.queued_jobs(10) == []


async def test_queued_jobs_are_listed_oldest_first_and_an_app_can_be_left_out(
    sql: SqlStore, clock: FixedClock
) -> None:
    first = await enqueued(sql, clock, app="a/busy")
    second = await enqueued(sql, clock, app="a/free")
    third = await enqueued(sql, clock, app="a/busy")
    fourth = await enqueued(sql, clock, app="a/other")
    await sql.create_job(make_job(clock, state="running"), None)  # not queued: never listed
    assert await sql.queued_jobs(10) == [
        (first.id, "a/busy"),
        (second.id, "a/free"),
        (third.id, "a/busy"),
        (fourth.id, "a/other"),
    ]
    assert await sql.queued_jobs(2) == [(first.id, "a/busy"), (second.id, "a/free")]
    assert await sql.queued_jobs(10, skip_apps={"a/busy"}) == [(second.id, "a/free"), (fourth.id, "a/other")]
    assert await sql.queued_stats() == (4, first.created_at)
    await sql.claim_job(first.id, clock.now())
    assert await sql.queued_stats() == (3, second.created_at)


async def test_the_queues_limit_holds_for_submits_at_the_same_moment(
    sql: SqlStore, clock: FixedClock
) -> None:
    """The limit is asked in the transaction that stores the job, behind a lock every such transaction
    takes: of several jobs stored at once with one place free, one is stored and the others are told
    the queue is full and store nothing. The count was read before the insert, in a transaction of its
    own, so each of them saw the free place (the reviewer, #184; T45)."""
    await enqueued(sql, clock)  # one of two places is taken
    jobs = [waiting(clock) for _ in range(6)]

    async def stored(job: Job) -> bool:
        try:
            await sql.enqueue_job(job, None, JobRequest(app=job.app, mode="async"), ALICE, max_queued=2)
        except NoCapacity:
            return False
        return True

    answers = await asyncio.gather(*(stored(job) for job in jobs))

    assert sorted(answers) == [False] * 5 + [True]
    assert (await sql.queued_stats())[0] == 2
    for job, kept in zip(jobs, answers, strict=True):  # a refused job left nothing: no row, no request
        assert (await sql.get_job(job.id) is not None) == kept
        assert await count(sql, t.job_requests, job.id) == int(kept)


async def test_a_job_is_stored_whatever_the_queue_holds_when_no_limit_is_named(
    sql: SqlStore, clock: FixedClock
) -> None:
    """A store asked with no limit applies none, as before the limit was the store's to hold."""
    for _ in range(3):
        await enqueued(sql, clock)
    assert (await sql.queued_stats())[0] == 3


async def test_an_empty_queue_has_no_oldest_job(sql: SqlStore) -> None:
    assert await sql.queued_stats() == (0, None)
    assert await sql.queued_jobs(10) == []


async def test_a_reference_held_is_answered_as_for_an_immediate_job(sql: SqlStore, clock: FixedClock) -> None:
    job = waiting(clock, ref="order-1")
    await sql.enqueue_job(job, "fp-1", REQUEST, ALICE)
    with pytest.raises(OperationExists) as held:
        await sql.enqueue_job(waiting(clock, ref="order-1"), "fp-1", REQUEST, ALICE)
    assert held.value.job == job
    with pytest.raises(OperationConflict):
        await sql.enqueue_job(waiting(clock, ref="order-1"), "fp-2", REQUEST, ALICE)
    async with sql.engine.connect() as conn:  # the refused jobs left no request behind
        assert (await conn.execute(select(func.count()).select_from(t.job_requests))).scalar_one() == 1

    # a failed job that never reached a runner is superseded by the queued retry, in one transaction
    assert await sql.cancel_queued(job.id, CANCELLED, clock.now())
    retry = waiting(clock, ref="order-1")
    await sql.enqueue_job(retry, "fp-1", REQUEST, ALICE, retry_of=job.id)
    found = await sql.job_by_operation_ref("acme", "alice", "order-1")
    assert found is not None and found[0].id == retry.id
    assert await sql.queued_jobs(10) == [(retry.id, "a/b")]


# -- back to the queue after a restart --------------------------------------------------------------------


async def test_a_running_job_with_no_attempt_goes_back_to_the_queue_at_most_twice(
    sql: SqlStore, clock: FixedClock
) -> None:
    job = await enqueued(sql, clock)
    for _ in range(2):
        assert await sql.claim_job(job.id, clock.now()) is not None
        assert await sql.requeue_unstarted(job.id, 3)
        back = await sql.get_job(job.id)
        assert back is not None and (back.state, back.started_at) == ("queued", None)
    assert await sql.claim_job(job.id, clock.now()) is not None  # the third claim
    assert not await sql.requeue_unstarted(job.id, 3)
    third = await sql.get_job(job.id)
    assert third is not None and third.state == "running"


async def test_a_job_with_an_attempt_or_without_its_request_never_goes_back(
    sql: SqlStore, clock: FixedClock
) -> None:
    started = await claimed(sql, clock)
    await sql.start_attempt(make_attempt(started.id, clock))
    assert not await sql.requeue_unstarted(started.id, 3)  # something may have gone out

    immediate = make_job(clock, state="running")
    await sql.create_job(immediate, None)
    assert not await sql.requeue_unstarted(immediate.id, 3)  # no stored request to run again

    still_queued = await enqueued(sql, clock)
    assert not await sql.requeue_unstarted(still_queued.id, 3)
    assert not await sql.requeue_unstarted(uuid4(), 3)


# -- the end of a job -------------------------------------------------------------------------------------


async def test_the_finishing_transaction_writes_the_state_the_result_and_the_bytes_together(
    sql: SqlStore, clock: FixedClock
) -> None:
    job = await claimed(sql, clock)
    expires = clock.now() + timedelta(days=7)
    ref = stored_ref(CONTENT, expires)
    assert await sql.finish_job_with_result(job.id, ref, CONTENT, clock.now())
    ended = await sql.get_job(job.id)
    assert ended is not None and (ended.state, ended.result, ended.completed_at) == (
        "succeeded",
        ref,
        clock.now(),
    )
    assert await sql.read_result(job.id, clock.now()) == CONTENT
    assert await count(sql, t.job_requests, job.id) == 0  # the stored request went with the job's end
    async with sql.engine.connect() as conn:
        row = (await conn.execute(select(t.job_results))).mappings().one()
    assert (row["content_type"], row["size_bytes"], row["sha256"], row["expires_at"]) == (
        ref.content_type,
        len(CONTENT),
        ref.sha256,
        expires,
    )
    # only a running job ends: a second finish writes nothing
    assert not await sql.finish_job_with_result(job.id, ref, b"other", clock.now())
    assert not await sql.finish_job(job.id, "failed", CANCELLED, None, clock.now())
    assert await sql.read_result(job.id, clock.now()) == CONTENT
    assert await sql.get_job(job.id) == ended


async def test_a_failed_write_leaves_the_job_running_with_no_bytes_stored(
    sql: SqlStore, clock: FixedClock
) -> None:
    """The bytes cannot be written (a row is in their place), so the state is not written either."""
    job = await claimed(sql, clock)
    async with sql.engine.begin() as conn:
        await conn.execute(
            insert(t.job_results).values(
                job_id=str(job.id),
                content=b"in the way",
                content_type="text/plain",
                size_bytes=10,
                sha256="0" * 64,
                expires_at=None,
            )
        )
    with pytest.raises(IntegrityError):
        await sql.finish_job_with_result(job.id, stored_ref(CONTENT, None), CONTENT, clock.now())
    left = await sql.get_job(job.id)
    assert left is not None and (left.state, left.result, left.completed_at) == ("running", None, None)
    assert await sql.read_result(job.id, clock.now()) == b"in the way"
    assert await count(sql, t.job_requests, job.id) == 1  # and its request is still there to recover from


async def test_a_result_is_read_until_it_expires(sql: SqlStore, clock: FixedClock) -> None:
    job = await claimed(sql, clock)
    expires = clock.now() + timedelta(days=7)
    assert await sql.finish_job_with_result(job.id, stored_ref(CONTENT, expires), CONTENT, clock.now())
    assert await sql.read_result(job.id, expires - timedelta(seconds=1)) == CONTENT
    assert await sql.read_result(job.id, expires) is None  # gone for a reader before the purge runs
    assert await sql.read_result(uuid4(), clock.now()) is None

    kept = await claimed(sql, clock)
    assert await sql.finish_job_with_result(kept.id, stored_ref(CONTENT, None), CONTENT, clock.now())
    assert await sql.read_result(kept.id, clock.now() + timedelta(days=9999)) == CONTENT


async def test_a_failed_end_deletes_the_stored_request_and_only_a_running_job_ends(
    sql: SqlStore, clock: FixedClock
) -> None:
    failure = Failure(kind="refused", message="busy")
    still_queued = await enqueued(sql, clock)
    assert not await sql.finish_job(still_queued.id, "failed", failure, None, clock.now())
    assert await count(sql, t.job_requests, still_queued.id) == 1

    job = await claimed(sql, clock)
    assert await sql.finish_job(job.id, "failed", failure, None, clock.now())
    ended = await sql.get_job(job.id)
    assert ended is not None and (ended.state, ended.failure) == ("failed", failure)
    assert await count(sql, t.job_requests, job.id) == 0
    assert not await sql.finish_job(job.id, "succeeded", None, None, clock.now())  # it ended once
    assert await sql.get_job(job.id) == ended


# -- cancel and the queue's time limit ------------------------------------------------------------------


async def test_a_cancel_is_one_conditional_write_on_a_queued_job(sql: SqlStore, clock: FixedClock) -> None:
    job = await enqueued(sql, clock)
    assert await sql.cancel_queued(job.id, CANCELLED, clock.now())
    ended = await sql.get_job(job.id)
    assert ended is not None and (ended.state, ended.failure, ended.completed_at) == (
        "failed",
        CANCELLED,
        clock.now(),
    )
    assert await count(sql, t.job_requests, job.id) == 0
    assert not await sql.cancel_queued(job.id, CANCELLED, clock.now())  # no longer queued
    assert await sql.claim_job(job.id, clock.now()) is None  # and never claimed

    running = await claimed(sql, clock)
    assert not await sql.cancel_queued(running.id, CANCELLED, clock.now())
    assert await count(sql, t.job_requests, running.id) == 1
    assert not await sql.cancel_queued(uuid4(), CANCELLED, clock.now())


async def test_a_cancel_and_a_claim_at_the_same_moment_do_not_both_win(
    sql: SqlStore, clock: FixedClock
) -> None:
    for _ in range(5):
        job = await enqueued(sql, clock)
        taken, cancelled = await asyncio.gather(
            sql.claim_job(job.id, clock.now()), sql.cancel_queued(job.id, CANCELLED, clock.now())
        )
        assert (taken is None) == cancelled
        ended = await sql.get_job(job.id)
        assert ended is not None and ended.state == ("failed" if cancelled else "running")


async def test_jobs_queued_past_the_time_limit_end_together(sql: SqlStore, clock: FixedClock) -> None:
    expired = Failure(kind="no_capacity", message="not dispatched within the queue's time limit")
    old = await enqueued(sql, clock)
    older_but_running = await claimed(sql, clock)
    cutoff = clock.now()
    fresh = await enqueued(sql, clock)
    assert await sql.expire_queued(cutoff, expired, clock.now()) == 1
    ended = await sql.get_job(old.id)
    assert ended is not None and (ended.state, ended.failure, ended.attempts) == ("failed", expired, ())
    assert await count(sql, t.job_requests, old.id) == 0
    for kept, state in ((older_but_running, "running"), (fresh, "queued")):
        left = await sql.get_job(kept.id)
        assert left is not None and left.state == state
        assert await count(sql, t.job_requests, kept.id) == 1
    assert await sql.expire_queued(cutoff, expired, clock.now()) == 0
