"""Cost reads on one connection, and the snapshot that keeps several of them consistent.

A cost read makes several queries: projections, the jobs none covers, their usage rows and, for
reconciliation, the rows no job claims. Cost sync can commit between any two of them (it matches rows
and stores projections), and a read that mixed states could count a row nowhere (#43 review).
Sync marks a projection stale in the transaction that changes its evidence, and these reads skip a
stale projection, so even a state between two of sync's commits counts every row once.
``SqlCostSnapshot`` runs every query on one connection inside one transaction that sees a single state:
``REPEATABLE READ`` on PostgreSQL, a read transaction on a SQLite file (WAL). In-memory SQLite has one
connection for everything, so it reads plainly.
"""

import contextlib
from collections.abc import AsyncIterator, Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from sqlalchemy.pool import StaticPool

from livepeer_builder.contracts import Job, JobCost, UsageRow
from livepeer_builder.store import schema as t
from livepeer_builder.store.matching import in_window, jobs_not_covered
from livepeer_builder.store.rows import chunks, load_jobs, projection, usage_row


@contextlib.asynccontextmanager
async def snapshot(engine: AsyncEngine) -> AsyncIterator["SqlCostSnapshot"]:
    async with engine.connect() as conn:
        if conn.dialect.name == "postgresql":
            conn = await conn.execution_options(isolation_level="REPEATABLE READ")
            async with conn.begin():
                yield SqlCostSnapshot(conn)
        elif isinstance(engine.sync_engine.pool, StaticPool):
            yield SqlCostSnapshot(conn)  # in-memory: one shared connection, no separate snapshot
        else:
            await conn.exec_driver_sql("BEGIN")  # the snapshot is taken at the first read
            try:
                yield SqlCostSnapshot(conn)
            finally:
                await conn.exec_driver_sql("ROLLBACK")  # read only


class SqlCostSnapshot:
    """The ``CostSnapshot`` reads, all on one connection (see ``snapshot``)."""

    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn

    async def job_with_projection(self, job_id: UUID) -> tuple[Job, JobCost | None] | None:
        rows = list((await self._conn.execute(select(t.jobs).where(t.jobs.c.id == str(job_id)))).mappings())
        if not rows:
            return None
        [job] = await load_jobs(self._conn, rows)
        return job, await get_projection(self._conn, job_id, fresh=True)

    async def projections_and_unprojected(
        self, application_id: str, actor_id: str | None, start: datetime, end: datetime
    ) -> tuple[list[JobCost], list[Job]]:
        window = in_window(application_id, actor_id, start, end)
        stored = (
            select(t.cost_projections)
            .join(t.jobs, t.jobs.c.id == t.cost_projections.c.job_id)
            .where(*window, t.cost_projections.c.stale.is_(False))
        )
        projections = [projection(row) for row in (await self._conn.execute(stored)).mappings()]
        selected = (
            select(t.jobs.c.id)
            .outerjoin(t.cost_projections, t.cost_projections.c.job_id == t.jobs.c.id)
            .where(*window, jobs_not_covered())
        )
        query = select(t.jobs).where(t.jobs.c.id.in_(selected)).order_by(t.jobs.c.created_at)
        return projections, await load_jobs(self._conn, list((await self._conn.execute(query)).mappings()))

    async def recent_with_projections(
        self, application_id: str, actor_id: str | None, limit: int
    ) -> list[tuple[Job, JobCost | None]]:
        query = select(t.jobs).where(t.jobs.c.application_id == application_id)
        if actor_id is not None:
            query = query.where(t.jobs.c.actor_id == actor_id)
        query = query.order_by(t.jobs.c.created_at.desc()).limit(limit)
        jobs = await load_jobs(self._conn, list((await self._conn.execute(query)).mappings()))
        costs: dict[UUID, JobCost] = {}
        if jobs:
            stored = select(t.cost_projections).where(
                t.cost_projections.c.job_id.in_([str(j.id) for j in jobs]),
                t.cost_projections.c.stale.is_(False),
            )
            for row in (await self._conn.execute(stored)).mappings():
                cost = projection(row)
                costs[cost.job_id] = cost
        return [(job, costs.get(job.id)) for job in jobs]

    async def usage_for_payment_sessions(self, payment_session_ids: Sequence[str]) -> list[UsageRow]:
        return await usage_for_payment_sessions(self._conn, payment_session_ids)

    async def unattributed_usage(self, allocation_id: str) -> list[UsageRow]:
        return await unattributed_usage(self._conn, allocation_id)


async def get_projection(conn: AsyncConnection, job_id: UUID, *, fresh: bool = False) -> JobCost | None:
    """The stored projection. ``fresh`` leaves out a stale one, which no longer stands for the job."""
    query = select(t.cost_projections).where(t.cost_projections.c.job_id == str(job_id))
    if fresh:
        query = query.where(t.cost_projections.c.stale.is_(False))
    row = (await conn.execute(query)).mappings().first()
    return None if row is None else projection(row)


async def usage_for_payment_sessions(
    conn: AsyncConnection, payment_session_ids: Sequence[str]
) -> list[UsageRow]:
    rows: list[UsageRow] = []
    for chunk in chunks(list(payment_session_ids)):
        query = select(t.usage_rows).where(t.usage_rows.c.payment_session_id.in_(chunk))
        rows.extend(usage_row(row) for row in (await conn.execute(query)).mappings())
    return rows


async def unattributed_usage(conn: AsyncConnection, allocation_id: str) -> list[UsageRow]:
    attributed = exists().where(
        t.payment_sessions.c.payment_session_id == t.usage_rows.c.payment_session_id,
        t.payment_sessions.c.match_status == "matched",
    )
    query = select(t.usage_rows).where(
        t.usage_rows.c.allocation_id == allocation_id, t.usage_rows.c.status == "applied", ~attributed
    )
    return [usage_row(row) for row in (await conn.execute(query)).mappings()]
