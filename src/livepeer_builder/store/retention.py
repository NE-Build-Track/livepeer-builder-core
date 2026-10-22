"""Configurable retention (docs/contracts/persistence.md#retention).

- Completed jobs (``succeeded``/``failed``) older than ``retention_jobs_days`` are deleted with their
  attempts, payment sessions and cost projections. ``uncertain`` jobs are kept until an operator resolves
  them, because they may hold charges that still need reconciling.
- Cached usage rows older than ``retention_usage_days`` are deleted. Batteries remains the
  authoritative ledger; the engine copy is a cache for attribution.
Both default to keeping everything.
"""

from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine

from livepeer_builder.contracts import Contract, JobState
from livepeer_builder.store import schema as t


class PurgeReport(Contract):
    jobs_deleted: int
    usage_rows_deleted: int


async def purge(
    engine: AsyncEngine, now: datetime, jobs_days: int | None, usage_days: int | None
) -> PurgeReport:
    jobs_deleted = usage_deleted = 0
    async with engine.begin() as conn:
        if jobs_days is not None:
            cutoff = now - timedelta(days=jobs_days)
            expired = select(t.jobs.c.id).where(
                t.jobs.c.state.in_((JobState.SUCCEEDED, JobState.FAILED)), t.jobs.c.completed_at < cutoff
            )
            ids = list((await conn.execute(expired)).scalars())
            for start in range(0, len(ids), 500):
                chunk = ids[start : start + 500]
                await conn.execute(delete(t.payment_sessions).where(t.payment_sessions.c.job_id.in_(chunk)))
                await conn.execute(delete(t.attempts).where(t.attempts.c.job_id.in_(chunk)))
                await conn.execute(delete(t.cost_projections).where(t.cost_projections.c.job_id.in_(chunk)))
                await conn.execute(delete(t.jobs).where(t.jobs.c.id.in_(chunk)))
            jobs_deleted = len(ids)
        if usage_days is not None:
            cutoff = now - timedelta(days=usage_days)
            result = await conn.execute(delete(t.usage_rows).where(t.usage_rows.c.created_at < cutoff))
            usage_deleted = result.rowcount
    return PurgeReport(jobs_deleted=jobs_deleted, usage_rows_deleted=usage_deleted)
