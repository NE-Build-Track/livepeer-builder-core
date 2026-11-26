"""#158: two cost syncs at once (the background loop and an admin's ``POST /v1/costs/sync``), on SQLite and
PostgreSQL.

Nothing stops the two running together, in the service or in the store, so each of sync's writes has to
take a row the other wrote first. The usage rows and the set-aside rows do (review of #144,
``test_usage_set_aside.py``). A job's first projection did not: both syncs looked, saw none and inserted
one, and on PostgreSQL the slower failed on the primary key of ``lpb_cost_projections``, which ended
its pass with ``store_error``. SQLite gives the write to one transaction at a time, so the slower one's
look already saw the row there.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import event, select

from livepeer_builder.contracts import (
    ActorContext,
    AttemptCost,
    Fees,
    JobCost,
    SetAsideUsage,
    UsagePage,
)
from livepeer_builder.services.costs import CostService, CostSyncWorker
from livepeer_builder.services.policy import DefaultPolicy
from livepeer_builder.store import SqlStore
from livepeer_builder.store import schema as t
from livepeer_builder.testing import FixedClock
from tests.store.conftest import make_attempt, make_job
from tests.store.test_usage_set_aside import stored_usage, usage_row

ADMIN = ActorContext(actor_id="operator", credential_kind="clearinghouse_admin")


@pytest.fixture
async def sql(database_url: str) -> AsyncIterator[SqlStore]:
    store = await SqlStore.connect(database_url)
    yield store
    await store.close()


class OnePageForBoth:
    """A usage feed two syncs read at the same moment: neither gets the page before the other asked."""

    def __init__(self, page: UsagePage) -> None:
        self._page = page
        self._asked = 0
        self._both_asked = asyncio.Event()

    async def page(self, cursor: str | None, limit: int = 1000) -> UsagePage:
        del cursor, limit
        self._asked += 1
        if self._asked == 2:
            self._both_asked.set()
        await self._both_asked.wait()
        return self._page


async def test_an_admin_s_sync_and_the_loop_s_over_the_same_page_both_end_and_count_it_once(
    sql: SqlStore, clock: FixedClock, caplog: pytest.LogCaptureFixture
) -> None:
    """Each sync holds the page before either writes, and both write it. Each ends with no error; every
    row, the set-aside one too, is new to one of them and is stored, counted and logged once; and the job
    the page pays for has one projection, with the fee of its one row."""
    job = make_job(clock)
    await sql.create_job(job, None)
    await sql.start_attempt(make_attempt(job.id, clock))
    await sql.finish_attempt(make_attempt(job.id, clock, manifest_ids=("m-u1",), allocation_id="a1"))
    left_out = SetAsideUsage(id="bad", reason="unknown_status", raw='{"id": "bad", "status": "mystery"}')
    page = UsagePage(items=(usage_row(clock, "u1"), usage_row(clock, "u2")), set_aside=(left_out,))
    costs = CostService(sql, DefaultPolicy(), clock, CostSyncWorker(OnePageForBoth(page), sql))

    with caplog.at_level(logging.WARNING, logger="livepeer_builder.services.costs"):
        reports = await asyncio.gather(costs.sync_now(ADMIN), costs.sync_once())

    events = [(record.event, getattr(record, "row_id", None)) for record in caplog.records]
    assert events == [("costs.usage_set_aside", "bad")]  # logged once, and no ``costs.sync_failed``
    assert costs.last_error is None and costs.set_aside_rows == 1
    assert [report.rows_seen for report in reports] == [3, 3]  # each read the whole page
    assert sum(report.rows_new for report in reports) == 2
    assert sorted(report.rows_set_aside for report in reports) == [0, 1]
    assert await stored_usage(sql) == ["u1", "u2"] and await sql.count_set_aside_usage() == 1
    cost = await sql.get_projection(job.id)
    assert cost is not None
    assert (cost.status, cost.fees.row_count, cost.fees.fee_eth) == ("observed", 1, Decimal("1E-18"))
    assert await sql.jobs_for_projection([]) == []  # the stored projection is the job's current one


def projection(job_id: UUID, clock: FixedClock, fee: str) -> JobCost:
    fees = Fees(fee_eth=Decimal(fee), fee_usd=None, row_count=1)
    return JobCost(
        job_id=job_id, status="observed", fees=fees, first_observed_at=clock.now(), updated_at=clock.now(),
        attempts=(AttemptCost(attempt_number=1, cost_class="delivered", fees=fees),),
    )  # fmt: skip


def looks_before_the_other_commits(store: SqlStore) -> None:
    """Make the next ``save_projection`` look as the slower of two syncs does: what it reads of
    ``lpb_cost_projections`` before its insert finds no row, as when the faster sync has not committed
    yet, and the row is there by the time it inserts."""
    looking = [True]

    @event.listens_for(store.engine.sync_engine, "before_cursor_execute", retval=True)
    def sees_no_row(conn: Any, cursor: Any, statement: str, parameters: Any, *_: Any) -> tuple[str, Any]:
        del conn, cursor
        if "lpb_cost_projections" in statement and looking[0]:
            if statement.startswith("INSERT"):
                looking[0] = False
            else:  # the look: an UPDATE or a SELECT by the job's id
                statement += " AND 1 = 0"
        return statement, parameters


@pytest.mark.parametrize(("slower_s_evidence", "kept"), [(2, "0.75"), (1, "0.75"), (0, "0.5")])
async def test_a_first_projection_another_sync_stored_since_this_one_looked_fails_nothing(
    sql: SqlStore, clock: FixedClock, slower_s_evidence: int, kept: str
) -> None:
    """The slower sync's insert meets the faster one's row. Nothing fails, and the job keeps one
    projection: the slower sync's when its evidence is as new or newer, else the faster one's."""
    job = make_job(clock)
    await sql.create_job(job, None)
    await sql.save_projection(projection(job.id, clock, "0.5"), evidence_version=1)  # the faster sync
    looks_before_the_other_commits(sql)
    await sql.save_projection(projection(job.id, clock, "0.75"), evidence_version=slower_s_evidence)
    async with sql.engine.connect() as conn:
        rows = list(
            await conn.execute(select(t.cost_projections.c.fee_eth, t.cost_projections.c.evidence_version))
        )
    assert rows == [(kept, max(1, slower_s_evidence))]


async def test_of_many_first_projections_of_a_job_saved_at_once_the_newest_evidence_is_kept(
    sql: SqlStore, clock: FixedClock
) -> None:
    """On a file database and on PostgreSQL every save has its own connection, so these overlap."""
    job = make_job(clock)
    await sql.create_job(job, None)
    saves = [
        sql.save_projection(projection(job.id, clock, f"0.{version}"), evidence_version=version)
        for version in range(1, 9)
    ]
    await asyncio.gather(*saves)
    stored = await sql.get_projection(job.id)
    assert stored is not None and stored.fees.fee_eth == Decimal("0.8")
