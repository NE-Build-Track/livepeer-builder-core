"""Resolve the session log against synced Batteries usage rows.

A key is (allocation_id, manifest_id). One Batteries payment session and one log entry match.
Anything else is ambiguous: the entries keep no payment_session_id, and the rows stay unattributed.
"""

from collections.abc import Iterable, Sequence
from uuid import UUID

from sqlalchemy import and_, false, func, insert, or_, select, update
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql.elements import ColumnElement

from livepeer_builder.contracts import Attempt, CostStatus, JobState, UsageRow
from livepeer_builder.store import schema as t

_CHUNK = 500


def unprojected() -> ColumnElement[bool]:
    """No stored projection covers this ended job (on ``jobs`` outer-joined to ``cost_projections``).

    None is stored, it was stored before the job ended (an earlier release stored running jobs), or
    it has a cost and no attempts. Reads compute these jobs and cost sync stores them. The service
    applies the same rule to a row it holds (``services.costs._unprojected``).
    """
    missing_attempts = and_(
        t.cost_projections.c.status.in_([status for status in CostStatus if status.has_cost]),
        func.json_array_length(t.cost_projections.c.attempts) == 0,
    )
    return or_(
        t.cost_projections.c.job_id.is_(None),
        t.jobs.c.completed_at > t.cost_projections.c.updated_at,
        missing_attempts,
    )


def jobs_needing_projection(job_ids: Sequence[str]) -> ColumnElement[bool]:
    """Ended jobs cost sync recomputes: touched by new usage, still ``pending``, or unprojected.

    A job that has not ended is never projected: it may still pay, so reads compute it live.
    """
    touched = t.jobs.c.id.in_(job_ids) if job_ids else false()
    return and_(
        t.jobs.c.state.in_([state for state in JobState if state.ended]),
        or_(touched, t.cost_projections.c.status == CostStatus.PENDING, unprojected()),
    )


async def append_sessions(conn: AsyncConnection, attempt: Attempt) -> None:
    """Append-only (D39): cost sync owns the match columns, so a row it matched is never rewritten."""
    session_key = and_(
        t.payment_sessions.c.job_id == str(attempt.job_id),
        t.payment_sessions.c.attempt_number == attempt.number,
    )
    stored = await conn.execute(select(t.payment_sessions.c.manifest_id).where(session_key))
    known = [row[0] for row in stored]
    new = [s for s in attempt.sessions if s.manifest_id not in known]
    for seq, session in enumerate(new, start=len(known)):
        await conn.execute(
            insert(t.payment_sessions).values(
                job_id=str(attempt.job_id),
                attempt_number=attempt.number,
                seq=seq,
                manifest_id=session.manifest_id,
                payment_session_id=session.payment_session_id,
                match_status=session.match,
            )
        )


async def match_payment_sessions(conn: AsyncConnection, rows: Sequence[UsageRow]) -> set[UUID]:
    """Returns every job whose match changed, or that received a new row on a matched session."""
    keys = {
        (row.allocation_id, row.manifest_id)
        for row in rows
        if row.status == "applied" and row.allocation_id and row.manifest_id
    }
    pending = await conn.execute(
        select(t.attempts.c.allocation_id, t.payment_sessions.c.manifest_id)
        .join(
            t.attempts,
            and_(
                t.attempts.c.job_id == t.payment_sessions.c.job_id,
                t.attempts.c.number == t.payment_sessions.c.attempt_number,
            ),
        )
        .where(
            t.payment_sessions.c.match_status == "unmatched",
            t.attempts.c.allocation_id.is_not(None),
        )
        .distinct()
    )
    keys.update((allocation, manifest) for allocation, manifest in pending if allocation and manifest)
    changed: set[UUID] = set()
    for allocation_id, manifest_id in keys:
        changed.update(await _match_key(conn, allocation_id, manifest_id))
    touched = [row.payment_session_id for row in rows if row.status == "applied" and row.payment_session_id]
    for chunk in _chunks(touched):
        found = await conn.execute(
            select(t.payment_sessions.c.job_id).where(
                t.payment_sessions.c.match_status == "matched",
                t.payment_sessions.c.payment_session_id.in_(chunk),
            )
        )
        changed.update(UUID(value) for value in found.scalars())
    return changed


async def _match_key(conn: AsyncConnection, allocation_id: str, manifest_id: str) -> set[UUID]:
    """An empty Batteries set leaves the log entries as they are."""
    found = await conn.execute(
        select(t.usage_rows.c.payment_session_id)
        .where(
            t.usage_rows.c.status == "applied",
            t.usage_rows.c.allocation_id == allocation_id,
            t.usage_rows.c.manifest_id == manifest_id,
            t.usage_rows.c.payment_session_id.is_not(None),
        )
        .distinct()
    )
    session_ids = list(found.scalars())
    if not session_ids:
        return set()
    entries = (
        (
            await conn.execute(
                select(
                    t.payment_sessions.c.job_id,
                    t.payment_sessions.c.attempt_number,
                    t.payment_sessions.c.seq,
                    t.payment_sessions.c.match_status,
                    t.payment_sessions.c.payment_session_id,
                )
                .join(
                    t.attempts,
                    and_(
                        t.attempts.c.job_id == t.payment_sessions.c.job_id,
                        t.attempts.c.number == t.payment_sessions.c.attempt_number,
                    ),
                )
                .where(
                    t.attempts.c.allocation_id == allocation_id,
                    t.payment_sessions.c.manifest_id == manifest_id,
                )
            )
        )
        .mappings()
        .all()
    )
    if len(session_ids) == 1 and len(entries) == 1:
        status, session_id = "matched", session_ids[0]
    else:
        status, session_id = "ambiguous", None
    changed: set[UUID] = set()
    for entry in entries:
        if entry["match_status"] == status and entry["payment_session_id"] == session_id:
            continue
        await conn.execute(
            update(t.payment_sessions)
            .where(
                t.payment_sessions.c.job_id == entry["job_id"],
                t.payment_sessions.c.attempt_number == entry["attempt_number"],
                t.payment_sessions.c.seq == entry["seq"],
            )
            .values(match_status=status, payment_session_id=session_id)
        )
        changed.add(UUID(entry["job_id"]))
    return changed


def _chunks(values: list[str]) -> Iterable[list[str]]:
    for start in range(0, len(values), _CHUNK):
        yield values[start : start + _CHUNK]
