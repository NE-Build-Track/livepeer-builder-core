"""A job's operation_ref on one connection (D57): the job holding it, and the compare-and-set a retry
takes it with. The reference is unique among jobs no retry superseded (migration 0009)."""

from typing import Any
from uuid import UUID

from sqlalchemy import Select, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from livepeer_builder.contracts import Job
from livepeer_builder.errors import OperationConflict, OperationExists
from livepeer_builder.store import schema as t


class Superseded(Exception):
    """Another retry superseded the failed job first; the transaction rolls back."""


def holder(application_id: str, actor_id: str, operation_ref: str) -> Select[Any]:
    """The job holding the reference: the one no retry superseded."""
    return select(t.jobs).where(
        t.jobs.c.application_id == application_id,
        t.jobs.c.actor_id == actor_id,
        t.jobs.c.operation_ref == operation_ref,
        t.jobs.c.superseded_by.is_(None),
    )


async def supersede(conn: AsyncConnection, failed_id: UUID, retry: Job) -> None:
    """Compare-and-set: mark ``failed_id`` superseded by ``retry`` only if it is still the failed job
    holding the same reference. Raises ``Superseded`` when another retry won, or it is not that job."""
    claimed = await conn.execute(
        update(t.jobs)
        .where(
            t.jobs.c.id == str(failed_id),
            t.jobs.c.application_id == retry.application_id,
            t.jobs.c.actor_id == retry.actor_id,
            t.jobs.c.operation_ref == retry.operation_ref,
            t.jobs.c.state == "failed",
            t.jobs.c.superseded_by.is_(None),
        )
        .values(superseded_by=str(retry.id))
    )
    if claimed.rowcount != 1:
        raise Superseded


def taken(found: Job, found_fingerprint: str | None, fingerprint: str | None) -> Exception:
    """The answer when the reference is held: a conflict for a different request (B2), else a repeat."""
    if found_fingerprint is not None and found_fingerprint != fingerprint:
        return OperationConflict(found)
    return OperationExists(found)
