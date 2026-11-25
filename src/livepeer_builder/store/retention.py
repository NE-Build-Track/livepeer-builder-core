"""Configurable retention (docs/contracts/persistence.md#retention).

- Completed jobs (``succeeded``/``failed``) older than ``retention_jobs_days`` are deleted with their
  attempts, payment sessions and cost projections. ``uncertain`` jobs are kept until an operator resolves
  them, because they may hold charges that still need reconciling.
- Cached usage rows older than ``retention_usage_days`` are deleted. Batteries remains the
  authoritative ledger; the engine copy is a cache for attribution.
Both default to keeping everything.

- The stored result of an async job (ISS-26) goes once its ``expires_at`` has passed: its bytes only, and
  the job keeps its record and its ``ResultRef``. ``LPB_RETENTION_RESULTS_DAYS`` set that time when the
  job ended, so this needs no setting of its own, and it runs on every purge. A job the job purge takes
  goes with its stored result and any request left beside it, deleted by table as its attempts are.
  ``queued`` and ``running`` jobs are never taken: only ``succeeded`` and ``failed`` ones are.
"""

from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine

from livepeer_builder.contracts import PurgeReport
from livepeer_builder.store import schema as t


async def purge(
    engine: AsyncEngine, now: datetime, jobs_days: int | None, usage_days: int | None
) -> PurgeReport:
    jobs_deleted = usage_deleted = 0
    async with engine.begin() as conn:
        past_due = t.job_results.c.expires_at.is_not(None) & (t.job_results.c.expires_at <= now)
        results_deleted = (await conn.execute(delete(t.job_results).where(past_due))).rowcount
        if jobs_days is not None:
            cutoff = now - timedelta(days=jobs_days)
            expired = select(t.jobs.c.id).where(
                t.jobs.c.state.in_(("succeeded", "failed")), t.jobs.c.completed_at < cutoff
            )
            ids = list((await conn.execute(expired)).scalars())
            for start in range(0, len(ids), 500):
                chunk = ids[start : start + 500]
                await conn.execute(delete(t.payment_sessions).where(t.payment_sessions.c.job_id.in_(chunk)))
                await conn.execute(delete(t.attempts).where(t.attempts.c.job_id.in_(chunk)))
                await conn.execute(delete(t.cost_projections).where(t.cost_projections.c.job_id.in_(chunk)))
                await conn.execute(delete(t.job_results).where(t.job_results.c.job_id.in_(chunk)))
                await conn.execute(delete(t.job_requests).where(t.job_requests.c.job_id.in_(chunk)))
                await conn.execute(delete(t.jobs).where(t.jobs.c.id.in_(chunk)))
            jobs_deleted = len(ids)
        if usage_days is not None:
            cutoff = now - timedelta(days=usage_days)
            result = await conn.execute(delete(t.usage_rows).where(t.usage_rows.c.created_at < cutoff))
            usage_deleted = result.rowcount
    return PurgeReport(
        jobs_deleted=jobs_deleted, usage_rows_deleted=usage_deleted, results_deleted=results_deleted
    )
