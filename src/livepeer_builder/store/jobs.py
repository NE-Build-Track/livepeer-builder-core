"""Jobs and their attempts in SQL (``lpb_jobs``, ``lpb_attempts``, ``lpb_payment_sessions``): ``JobRepo`` for
``SqlStore``."""

import asyncio
from datetime import datetime
from typing import Any
from uuid import UUID
from weakref import WeakValueDictionary

from sqlalchemy import String, and_, cast, insert, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from livepeer_builder.contracts import (
    ActorContext,
    Attempt,
    Failure,
    Job,
    JobRequest,
    JobState,
    MeasuredUsage,
    ResultRef,
)
from livepeer_builder.store import job_queue as queue
from livepeer_builder.store import operation_refs as refs
from livepeer_builder.store import schema as t
from livepeer_builder.store.matching import bump_evidence
from livepeer_builder.store.rows import load_jobs


class SqlJobs:
    """``JobRepo`` for ``SqlStore``, which makes the engine and the evidence locks."""

    engine: AsyncEngine
    _evidence_locks: WeakValueDictionary[tuple[str, int], asyncio.Lock]

    async def create_job(self, job: Job, fingerprint: str | None, *, retry_of: UUID | None = None) -> None:
        """Insert a job. A reference already held raises ``OperationExists`` with the holder, or
        ``OperationConflict`` when the holder's fingerprint differs (B2).

        ``retry_of`` re-dispatches under the same reference (D57): one transaction marks that failed job
        superseded by this one, a compare-and-set, then inserts this one. If another retry already
        superseded it, the answer comes from the job now holding the reference."""
        await self._insert_job(job, fingerprint, retry_of, None)

    async def enqueue_job(
        self,
        job: Job,
        fingerprint: str | None,
        request: JobRequest,
        actor: ActorContext,
        *,
        retry_of: UUID | None = None,
        max_queued: int | None = None,
    ) -> None:
        """``create_job`` for a ``queued`` async job (ISS-26): the same transaction stores what it will
        run with, so a job is never queued without its request, and asks the queue's limit first when
        ``max_queued`` names one (``queue.room``): ``NoCapacity``, and nothing stored, when it is full."""
        waiting = queue.request_values(job.id, request, actor)
        await self._insert_job(job, fingerprint, retry_of, waiting, max_queued)

    async def _insert_job(
        self,
        job: Job,
        fingerprint: str | None,
        retry_of: UUID | None,
        waiting: dict[str, Any] | None,
        max_queued: int | None = None,
    ) -> None:
        values = _job_values(job) | {"request_fingerprint": fingerprint}
        if retry_of is not None:
            values["retry_of"] = str(retry_of)
        try:
            async with self.engine.begin() as conn:
                if max_queued is not None:
                    await queue.room(conn, max_queued)
                if retry_of is not None:
                    await refs.supersede(conn, retry_of, job)
                await conn.execute(insert(t.jobs).values(values))
                if waiting is not None:
                    await conn.execute(insert(t.job_requests).values(waiting))
        except (IntegrityError, refs.Superseded):
            if job.operation_ref is None:
                raise
            found = await self.job_by_operation_ref(job.tenant_id, job.actor_id, job.operation_ref)
            if found is None:
                if retry_of is not None:  # the failed job is gone (retention): the reference is free
                    return await self._insert_job(job, fingerprint, None, waiting, max_queued)
                raise
            raise refs.taken(*found, fingerprint) from None

    async def job_by_operation_ref(
        self, tenant_id: str, actor_id: str, operation_ref: str
    ) -> tuple[Job, str | None] | None:
        """The job holding the reference: the one no retry superseded (D57)."""
        async with self.engine.connect() as conn:
            row = (await conn.execute(refs.holder(tenant_id, actor_id, operation_ref))).mappings().first()
            if row is None:
                return None
            return (await load_jobs(conn, [row]))[0], row["request_fingerprint"]

    async def get_job(self, job_id: UUID) -> Job | None:
        async with self.engine.connect() as conn:
            row = (await conn.execute(select(t.jobs).where(t.jobs.c.id == str(job_id)))).mappings().first()
            return (await load_jobs(conn, [row]))[0] if row else None

    async def list_jobs(
        self, tenant_id: str, actor_id: str | None, limit: int = 50, before: datetime | None = None
    ) -> list[Job]:
        query = select(t.jobs).where(t.jobs.c.tenant_id == tenant_id)
        if actor_id is not None:
            query = query.where(t.jobs.c.actor_id == actor_id)
        if before is not None:
            query = query.where(t.jobs.c.created_at < before)
        query = query.order_by(t.jobs.c.created_at.desc()).limit(limit)
        async with self.engine.connect() as conn:
            return await load_jobs(conn, list((await conn.execute(query)).mappings()))

    async def jobs_in_state(self, state: JobState) -> list[Job]:
        async with self.engine.connect() as conn:
            rows = list((await conn.execute(select(t.jobs).where(t.jobs.c.state == state))).mappings())
            return await load_jobs(conn, rows)

    async def start_attempt(self, attempt: Attempt, *, claim: int | None = None) -> bool:
        """Insert the attempt, before its call. With ``claim``, an async job's (ISS-26), only while the
        job is ``running`` under that claim, in the same transaction: ``False``, and nothing written,
        for a worker that no longer holds the job."""
        async with self.engine.begin() as conn:
            if claim is not None and not await queue.holds(conn, attempt.job_id, claim):
                return False
            await conn.execute(insert(t.attempts).values(_attempt_values(attempt)))
        return True

    async def finish_attempt(self, attempt: Attempt) -> None:
        key = and_(t.attempts.c.job_id == str(attempt.job_id), t.attempts.c.number == attempt.number)
        async with self._evidence_lock(attempt), self.engine.begin() as conn:
            values = _attempt_values(attempt) | {"payment_sent": _paid(attempt)}  # never unpaid again (D54)
            await conn.execute(update(t.attempts).where(key).values(values))
            await _append_sessions(conn, attempt)

    async def record_payment_evidence(self, attempt: Attempt) -> None:
        key = and_(t.attempts.c.job_id == str(attempt.job_id), t.attempts.c.number == attempt.number)
        async with self._evidence_lock(attempt), self.engine.begin() as conn:  # outcome and timing stay
            # attempt before session, as matching locks them (D54)
            await conn.execute(select(t.attempts.c.number).where(key).with_for_update())
            if attempt.payment_sent:  # an intent stored before signing (D54) is not a payment
                await conn.execute(update(t.attempts).where(key).values(payment_sent=True))
            await _append_sessions(conn, attempt)
            await bump_evidence(conn, [str(attempt.job_id)])  # a late report on an ended job changes its cost

    def _evidence_lock(self, attempt: Attempt) -> asyncio.Lock:
        """One writer at a time for an attempt's evidence. The write before signing (D54), ``on_signed``
        and the close after a cancel can overlap, and each numbers new sessions from what it read, so two
        at once would both claim the next ``seq``. Every writer of an attempt runs in the process that
        runs its job, so an in-process lock is enough."""
        key = (str(attempt.job_id), attempt.number)
        lock = self._evidence_locks.get(key)
        if lock is None:
            lock = self._evidence_locks[key] = asyncio.Lock()
        return lock

    async def finish_job(
        self,
        job_id: UUID,
        state: JobState,
        failure: Failure | None,
        result: ResultRef | None,
        completed_at: datetime,
        *,
        claim: int | None = None,
    ) -> bool:
        """End a ``running`` job, and only a running one (ISS-26): a late writer never reopens a job that
        ended, and a queued one ends by its own writes (``store/job_queue.py``). With ``claim``, only
        while that claim holds the job. The request stored for an async job goes in the same
        transaction."""
        async with self.engine.begin() as conn:
            return await queue.end_running(conn, job_id, state, failure, result, completed_at, claim)

    async def set_usage(self, job_id: UUID, usage: MeasuredUsage) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(
                update(t.jobs).where(t.jobs.c.id == str(job_id)).values(usage=usage.model_dump(mode="json"))
            )

    async def set_usage_once(self, job_id: UUID, usage: MeasuredUsage) -> bool:
        """Store ``usage`` only where the job has none and is ``succeeded``, in one statement (#115, C20):
        of two reports at the same moment, one is stored. A job without usage holds a JSON ``null``."""
        none = or_(t.jobs.c.usage.is_(None), cast(t.jobs.c.usage, String) == "null")
        free = and_(t.jobs.c.id == str(job_id), t.jobs.c.state == "succeeded", none)
        async with self.engine.begin() as conn:
            stored = await conn.execute(
                update(t.jobs).where(free).values(usage=usage.model_dump(mode="json"))
            )
        return stored.rowcount == 1


def _paid(attempt: Attempt) -> Any:
    """``payment_sent`` as stored or as reported: a stale writer cannot unpay a confirmed payment."""
    return t.attempts.c.payment_sent | attempt.payment_sent


async def _append_sessions(conn: AsyncConnection, attempt: Attempt) -> None:
    """Add the attempt's sessions not stored yet, and move a stored one's ``payment_state`` on until it
    is ``paid``, which is final (D54). Cost sync owns the match columns, which this never writes (D39)."""
    session_key = and_(
        t.payment_sessions.c.job_id == str(attempt.job_id),
        t.payment_sessions.c.attempt_number == attempt.number,
    )
    stored = await conn.execute(
        select(t.payment_sessions.c.manifest_id, t.payment_sessions.c.payment_state).where(session_key)
    )
    known: dict[str, str] = dict(stored.all())
    for session in attempt.sessions:
        current = known.get(session.manifest_id)
        if current is None or current in (session.payment_state, "paid"):
            continue  # new (inserted below), unchanged, or final: no write (#64 review)
        await conn.execute(
            update(t.payment_sessions)
            .where(
                session_key,
                t.payment_sessions.c.manifest_id == session.manifest_id,
                t.payment_sessions.c.payment_state != "paid",  # cost sync may have confirmed it since
            )
            .values(payment_state=session.payment_state)
        )
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
                payment_state=session.payment_state,
            )
        )


def _job_values(job: Job) -> dict[str, Any]:
    return {
        "id": str(job.id),
        "tenant_id": job.tenant_id,
        "actor_id": job.actor_id,
        "operation_ref": job.operation_ref,
        "mode": job.mode,
        "app": job.app,
        "capability": job.capability,
        "model": job.model,
        "state": job.state,
        "failure": job.failure.model_dump(mode="json") if job.failure else None,
        "result": job.result.model_dump(mode="json") if job.result else None,
        "usage": job.usage.model_dump(mode="json") if job.usage else None,
        "created_at": job.created_at,
        "completed_at": job.completed_at,
        "started_at": job.started_at,
    }


def _attempt_values(attempt: Attempt) -> dict[str, Any]:
    values = attempt.model_dump(exclude={"sessions", "timings"})
    values["job_id"] = str(attempt.job_id)
    return values | attempt.timings.model_dump() if attempt.timings else values
