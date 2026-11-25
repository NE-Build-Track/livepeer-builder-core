"""ISS-26: every write after a claim checks that it still holds that claim, on SQLite and PostgreSQL.

``claim_job`` answers with the claim's number, the row's ``claims`` after the claim. The writes that
follow carry it: the first attempt, the requeue and both ends of the job apply only while the job is
``running`` under that number. A writer whose claim was taken from it (the job was put back and claimed
again, or ended by another writer) changes nothing and is told so. A write that names no claim, an
immediate job's or a stream's, is guarded as before.
"""

from datetime import timedelta
from uuid import UUID

from sqlalchemy import select

from livepeer_builder.contracts import Failure
from livepeer_builder.store import SqlStore
from livepeer_builder.store import schema as t
from livepeer_builder.testing import FixedClock
from tests.store.conftest import make_attempt, make_job
from tests.store.test_job_queue import CONTENT, count, enqueued, sql, stored_ref  # noqa: F401 - sql: a fixture

GONE = Failure(kind="runner_error", message="engine error: OSError")


async def claims(sql: SqlStore, job_id: UUID) -> int:  # noqa: F811 - the fixture, by name
    async with sql.engine.connect() as conn:
        found = await conn.execute(select(t.jobs.c.claims).where(t.jobs.c.id == str(job_id)))
        return int(found.scalar_one())


async def claimed_twice(sql: SqlStore, clock: FixedClock) -> UUID:  # noqa: F811
    """A job whose first claim was taken from its holder: put back, then claimed again as number 2."""
    job = await enqueued(sql, clock)
    first = await sql.claim_job(job.id, clock.now())
    assert first is not None and first[3] == 1
    assert await sql.requeue_unstarted(job.id, 3)
    second = await sql.claim_job(job.id, clock.now())
    assert second is not None and second[3] == 2
    return job.id


async def test_a_claim_answers_with_its_number(sql: SqlStore, clock: FixedClock) -> None:  # noqa: F811
    job = await enqueued(sql, clock)
    taken = await sql.claim_job(job.id, clock.now())
    assert taken is not None
    running, _, _, number = taken
    assert (running.state, number, await claims(sql, job.id)) == ("running", 1, 1)
    assert await sql.requeue_unstarted(job.id, 3, claim=1)
    again = await sql.claim_job(job.id, clock.now())
    assert again is not None and again[3] == 2


async def test_a_first_attempt_is_written_only_under_the_claim_that_holds_the_job(
    sql: SqlStore,  # noqa: F811
    clock: FixedClock,
) -> None:
    job_id = await claimed_twice(sql, clock)

    assert await sql.start_attempt(make_attempt(job_id, clock), claim=1) is False  # the claim it lost
    assert await count(sql, t.attempts, job_id) == 0
    assert await sql.start_attempt(make_attempt(job_id, clock), claim=2) is True
    assert await count(sql, t.attempts, job_id) == 1

    ended = await enqueued(sql, clock)
    taken = await sql.claim_job(ended.id, clock.now())
    assert taken is not None
    assert await sql.finish_job(ended.id, "failed", GONE, None, clock.now(), claim=taken[3])
    assert await sql.start_attempt(make_attempt(ended.id, clock), claim=taken[3]) is False  # it has ended
    assert await count(sql, t.attempts, ended.id) == 0


async def test_an_attempt_that_names_no_claim_is_written_as_before(
    sql: SqlStore,  # noqa: F811
    clock: FixedClock,
) -> None:
    """An immediate job and a stream have no claim: their attempt is written whatever the job's row says."""
    immediate = make_job(clock, state="running")
    await sql.create_job(immediate, None)
    assert await sql.start_attempt(make_attempt(immediate.id, clock)) is True
    assert await count(sql, t.attempts, immediate.id) == 1


async def test_a_job_ends_only_under_the_claim_that_holds_it(sql: SqlStore, clock: FixedClock) -> None:  # noqa: F811
    job_id = await claimed_twice(sql, clock)
    ref = stored_ref(CONTENT, clock.now() + timedelta(days=7))

    assert not await sql.finish_job(job_id, "failed", GONE, None, clock.now(), claim=1)
    assert not await sql.finish_job_with_result(job_id, ref, CONTENT, clock.now(), claim=1)
    left = await sql.get_job(job_id)
    assert left is not None and (left.state, left.failure, left.result) == ("running", None, None)
    assert await sql.read_result(job_id, clock.now()) is None
    assert await count(sql, t.job_requests, job_id) == 1  # its request is still the holder's to run

    assert await sql.finish_job_with_result(job_id, ref, CONTENT, clock.now(), claim=2)
    assert await sql.read_result(job_id, clock.now()) == CONTENT

    other = await claimed_twice(sql, clock)
    assert await sql.finish_job(other, "failed", GONE, None, clock.now(), claim=2)
    ended = await sql.get_job(other)
    assert ended is not None and (ended.state, ended.failure) == ("failed", GONE)


async def test_a_job_goes_back_to_the_queue_only_under_the_claim_that_holds_it(
    sql: SqlStore,  # noqa: F811
    clock: FixedClock,
) -> None:
    job_id = await claimed_twice(sql, clock)

    assert not await sql.requeue_unstarted(job_id, 3, claim=1)
    still = await sql.get_job(job_id)
    assert still is not None and still.state == "running"
    assert await sql.requeue_unstarted(job_id, 3, claim=2)
    back = await sql.get_job(job_id)
    assert back is not None and (back.state, back.started_at) == ("queued", None)
    assert await claims(sql, job_id) == 2  # a requeue keeps the count: three claims at most


async def test_a_claim_given_back_uncounted_leaves_the_claim_budget_as_it_was(
    sql: SqlStore,  # noqa: F811
    clock: FixedClock,
) -> None:
    """A job that could not be dispatched for a reason that passes is given back without its claim
    counting, any number of times: the three-claim budget is for a job that stops the engine."""
    job = await enqueued(sql, clock)
    for _ in range(5):
        taken = await sql.claim_job(job.id, clock.now())
        assert taken is not None and taken[3] == 1
        assert await sql.requeue_unstarted(job.id, 3, claim=1, counted=False)
        back = await sql.get_job(job.id)
        assert back is not None and (back.state, back.started_at) == ("queued", None)
        assert await claims(sql, job.id) == 0

    third = await claimed_twice(sql, clock)
    assert await sql.requeue_unstarted(third, 3, claim=2)
    last = await sql.claim_job(third, clock.now())
    assert last is not None and last[3] == 3
    assert not await sql.requeue_unstarted(third, 3, claim=3)  # counted: the budget is used up
    assert not await sql.requeue_unstarted(third, 3, claim=2, counted=False)  # not the claim that holds it
    assert await sql.requeue_unstarted(third, 3, claim=3, counted=False)
    assert await claims(sql, third) == 2

    started = await enqueued(sql, clock)
    taken = await sql.claim_job(started.id, clock.now())
    assert taken is not None
    assert await sql.start_attempt(make_attempt(started.id, clock), claim=taken[3])
    assert not await sql.requeue_unstarted(started.id, 3, claim=taken[3], counted=False)  # something went out
