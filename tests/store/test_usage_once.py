"""#115 C20: a job's usage is written once (``UsageOnceRepo.set_usage_once``), on SQLite and PostgreSQL.

On a file database every call has its own connection, so the reports in the last test really do overlap.
"""

import asyncio
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import insert, text

from livepeer_builder.contracts import MeasuredUsage
from livepeer_builder.store import SqlStore
from livepeer_builder.store import schema as t
from livepeer_builder.testing import FixedClock
from tests.store.conftest import make_job


@pytest.fixture
async def sql(database_url: str) -> AsyncIterator[SqlStore]:
    store = await SqlStore.connect(database_url)
    yield store
    await store.close()


def usage(clock: FixedClock, tokens: int) -> MeasuredUsage:
    return MeasuredUsage(quantities={"tokens": tokens}, source="app_reported", reported_at=clock.now())


async def test_usage_is_stored_where_the_job_has_none_and_never_replaced(
    sql: SqlStore, clock: FixedClock
) -> None:
    job = make_job(clock)
    await sql.create_job(job, None)
    assert await sql.set_usage_once(job.id, usage(clock, 3)) is True
    assert await sql.set_usage_once(job.id, usage(clock, 9)) is False
    assert await sql.set_usage_once(job.id, usage(clock, 3)) is False  # the same again: nothing written
    stored = await sql.get_job(job.id)
    assert stored is not None and stored.usage == usage(clock, 3)


@pytest.mark.parametrize("state", ["running", "failed", "uncertain"])
async def test_usage_is_stored_only_for_a_succeeded_job(sql: SqlStore, clock: FixedClock, state: str) -> None:
    job = make_job(clock, state=state)
    await sql.create_job(job, None)
    assert await sql.set_usage_once(job.id, usage(clock, 3)) is False
    stored = await sql.get_job(job.id)
    assert stored is not None and stored.usage is None
    assert await sql.set_usage_once(make_job(clock).id, usage(clock, 3)) is False  # no such job


async def test_a_job_row_written_with_no_usage_column_takes_its_first_report(
    sql: SqlStore, clock: FixedClock
) -> None:
    """The store writes a job without usage as a JSON ``null``; a row inserted without the column holds a
    SQL ``NULL``. Both mean "none stored"."""
    job = make_job(clock)
    async with sql.engine.begin() as conn:
        await conn.execute(insert(t.jobs).values(
            id=str(job.id), application_id="default", actor_id="alice", mode="immediate", app="a/b",
            state="succeeded", created_at=clock.now(), completed_at=clock.now(),
        ))  # fmt: skip
        held = await conn.execute(text("SELECT usage IS NULL FROM lpb_jobs"))
        assert bool(held.scalar_one()) is True
    assert await sql.set_usage_once(job.id, usage(clock, 3)) is True
    assert await sql.set_usage_once(job.id, usage(clock, 9)) is False
    stored = await sql.get_job(job.id)
    assert stored is not None and stored.usage == usage(clock, 3)


async def test_of_many_reports_at_the_same_time_one_is_stored(sql: SqlStore, clock: FixedClock) -> None:
    job = make_job(clock)
    await sql.create_job(job, None)
    reports = [usage(clock, tokens) for tokens in range(1, 9)]
    stored_by = await asyncio.gather(*(sql.set_usage_once(job.id, report) for report in reports))
    assert stored_by.count(True) == 1
    stored = await sql.get_job(job.id)
    assert stored is not None and stored.usage == reports[stored_by.index(True)]
