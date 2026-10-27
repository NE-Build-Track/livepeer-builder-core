"""Resolve the session log against synced Batteries usage rows.

A key is (allocation_id, manifest_id). One Batteries payment session and one log entry match.
Anything else is ambiguous: the entries keep no payment_session_id, and the rows stay unattributed.
"""

from collections.abc import Iterable, Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import and_, false, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql.elements import ColumnElement

from livepeer_builder.contracts import UsageRow
from livepeer_builder.store import schema as t

_CHUNK = 500


ENDED = ("succeeded", "failed", "uncertain")  # a job in one of these states can no longer pay


def projection_current() -> ColumnElement[bool]:
    """The projection was computed from the job's current evidence."""
    return t.cost_projections.c.evidence_version >= t.jobs.c.evidence_version


def projection_out_of_date() -> ColumnElement[bool]:
    """No projection, one stored before the job ended, or one computed from older evidence."""
    return or_(
        t.cost_projections.c.job_id.is_(None),
        t.jobs.c.completed_at > t.cost_projections.c.updated_at,
        ~projection_current(),
    )


async def bump_evidence(conn: AsyncConnection, job_ids: Iterable[str]) -> None:
    """These jobs' evidence changed: their projections no longer stand for them (#43 review).

    Called in the transaction that changes the evidence. A version, not a flag: a sync that classified
    the job earlier stores the version it read, which is already behind, so no save can hide the change.
    Reads compute an out-of-date job live; the next sync stores it again."""
    for chunk in _chunks(sorted(set(job_ids))):
        await conn.execute(
            update(t.jobs)
            .where(t.jobs.c.id.in_(chunk))
            .values(evidence_version=t.jobs.c.evidence_version + 1)
        )


async def bump_evidence_on_new_rows(conn: AsyncConnection, rows: Sequence[UsageRow]) -> None:
    """A new applied row on a session that is already matched adds to that job's fee."""
    sessions = sorted(
        {row.payment_session_id for row in rows if row.status == "applied" and row.payment_session_id}
    )
    for chunk in _chunks(sessions):
        found = await conn.execute(
            select(t.payment_sessions.c.job_id).where(
                t.payment_sessions.c.match_status == "matched",
                t.payment_sessions.c.payment_session_id.in_(chunk),
            )
        )
        await bump_evidence(conn, found.scalars())


def jobs_needing_projection(job_ids: Sequence[str]) -> ColumnElement[bool]:
    """Ended jobs whose projection should be recomputed, including observed rows stored with no attempts.

    A running job is never projected: it may still pay, and a projection of it would be stale the
    moment it did (#33). Reads compute it live instead (``jobs_not_covered``)."""
    touched = t.jobs.c.id.in_(job_ids) if job_ids else false()
    missing_attempts = and_(
        t.cost_projections.c.status.in_(("observed", "corrected")),
        func.json_array_length(t.cost_projections.c.attempts) == 0,
    )
    return and_(
        t.jobs.c.state.in_(ENDED),
        or_(touched, projection_out_of_date(), t.cost_projections.c.status == "pending", missing_attempts),
    )


def in_window(
    application_id: str, actor_id: str | None, start: datetime, end: datetime
) -> list[ColumnElement[bool]]:
    """Jobs of one application (and actor, when given) created in [start, end)."""
    clauses = [
        t.jobs.c.application_id == application_id,
        t.jobs.c.created_at >= start,
        t.jobs.c.created_at < end,
    ]
    if actor_id is not None:
        clauses.append(t.jobs.c.actor_id == actor_id)
    return clauses


def jobs_not_covered() -> ColumnElement[bool]:
    """Jobs a cost read must compute: still running, or ended with no projection since they ended."""
    return or_(t.jobs.c.state.not_in(ENDED), projection_out_of_date())


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
            t.payment_sessions.c.payment_state != "unpaid",
            t.attempts.c.allocation_id.is_not(None),
        )
        .distinct()
    )
    keys.update((allocation, manifest) for allocation, manifest in pending if allocation and manifest)
    changed: set[UUID] = set()
    for allocation_id, manifest_id in sorted(keys):
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
                    t.payment_sessions.c.payment_state,
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
                    t.payment_sessions.c.payment_state != "unpaid",
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
        confirmed = status == "matched" and entry["payment_state"] == "unknown"
        if entry["match_status"] == status and entry["payment_session_id"] == session_id and not confirmed:
            continue
        # Same lock order as record_payment_evidence: attempt before session.
        if confirmed:
            await conn.execute(
                update(t.attempts)
                .where(
                    t.attempts.c.job_id == entry["job_id"],
                    t.attempts.c.number == entry["attempt_number"],
                )
                .values(payment_sent=True)
            )
        await conn.execute(
            update(t.payment_sessions)
            .where(
                t.payment_sessions.c.job_id == entry["job_id"],
                t.payment_sessions.c.attempt_number == entry["attempt_number"],
                t.payment_sessions.c.seq == entry["seq"],
            )
            .values(
                match_status=status,
                payment_session_id=session_id,
                payment_state="paid" if confirmed else t.payment_sessions.c.payment_state,
            )
        )
        changed.add(UUID(entry["job_id"]))
    return changed


def _chunks(values: list[str]) -> Iterable[list[str]]:
    for start in range(0, len(values), _CHUNK):
        yield values[start : start + _CHUNK]
