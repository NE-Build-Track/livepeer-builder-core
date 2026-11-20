"""ISS-26: retention of what an async job leaves, identical on SQLite and PostgreSQL.

A stored result's bytes go when their ``expires_at`` has passed (``LPB_RETENTION_RESULTS_DAYS`` set it
when the job ended), and the job keeps its record and its ``ResultRef``. The job purge
(``LPB_RETENTION_JOBS_DAYS``) removes a job with its stored result and any request left beside it, and
never takes a job that is ``queued`` or ``running`` (docs/contracts/persistence.md#retention).
"""

from datetime import timedelta

from livepeer_builder.contracts import Failure
from livepeer_builder.store import SqlStore
from livepeer_builder.store import schema as t
from livepeer_builder.testing import FixedClock
from tests.store.test_job_queue import CONTENT, claimed, count, enqueued, sql, stored_ref

__all__ = ["sql"]  # the fixture, shared with the queue's own store tests

DAY = timedelta(days=1)


async def test_result_bytes_go_when_they_expire_and_the_job_keeps_its_record(
    sql: SqlStore, clock: FixedClock
) -> None:
    expiring, kept = await claimed(sql, clock), await claimed(sql, clock)
    expires = clock.now() + 7 * DAY
    assert await sql.finish_job_with_result(expiring.id, stored_ref(CONTENT, expires), CONTENT, clock.now())
    assert await sql.finish_job_with_result(kept.id, stored_ref(CONTENT, None), CONTENT, clock.now())
    assert sql.maintenance is not None

    await sql.maintenance.purge(expires - timedelta(seconds=1), None, None)
    assert await count(sql, t.job_results, expiring.id) == 1  # not yet

    report = await sql.maintenance.purge(expires, None, None)  # no job or usage retention is needed
    assert await count(sql, t.job_results, expiring.id) == 0
    assert await count(sql, t.job_results, kept.id) == 1  # no expiry: kept as long as its job
    assert (report.results_deleted, report.jobs_deleted, report.usage_rows_deleted) == (1, 0, 0)
    job = await sql.get_job(expiring.id)
    assert job is not None and job.state == "succeeded"
    assert job.result == stored_ref(CONTENT, expires)  # the record still says what the result was
    assert (await sql.maintenance.purge(expires + DAY, None, None)).results_deleted == 0


async def test_the_job_purge_removes_a_job_with_its_result_and_its_request(
    sql: SqlStore, clock: FixedClock
) -> None:
    """A succeeded job's bytes, kept as long as the job, go with it. So does a request left beside a job
    that ended (the transaction that ends a job deletes it; this one was left by hand)."""
    succeeded = await claimed(sql, clock)
    assert await sql.finish_job_with_result(succeeded.id, stored_ref(CONTENT, None), CONTENT, clock.now())
    cancelled = await enqueued(sql, clock)
    failure = Failure(kind="abandoned", message="cancelled before it ran", reason="cancelled")
    assert await sql.cancel_queued(cancelled.id, failure, clock.now())
    async with sql.engine.begin() as conn:  # a request row that outlived its job's end
        await conn.execute(
            t.job_requests.insert().values(
                job_id=str(cancelled.id), request={"app": "a/b"}, raw_body=None, actor={"actor_id": "alice"}
            )
        )
    assert sql.maintenance is not None

    report = await sql.maintenance.purge(clock.now() + 31 * DAY, 30, None)
    assert await sql.get_job(succeeded.id) is None and await sql.get_job(cancelled.id) is None
    assert await count(sql, t.job_results, succeeded.id) == 0
    assert await count(sql, t.job_requests, cancelled.id) == 0
    assert report.jobs_deleted == 2


async def test_the_purge_never_takes_a_queued_or_a_running_job(sql: SqlStore, clock: FixedClock) -> None:
    waiting = await enqueued(sql, clock)
    running = await claimed(sql, clock)
    assert sql.maintenance is not None

    report = await sql.maintenance.purge(clock.now() + 365 * DAY, 1, 1)
    assert report.jobs_deleted == 0
    for job in (waiting, running):
        assert await sql.get_job(job.id) is not None
        assert await count(sql, t.job_requests, job.id) == 1  # still what it runs with
