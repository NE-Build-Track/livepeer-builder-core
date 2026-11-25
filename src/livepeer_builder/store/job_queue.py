"""The queue of async jobs in SQL (D69, ISS-26): ``lpb_jobs`` in state ``queued``, the request each job
waits with (``lpb_job_requests``) and the result it leaves (``lpb_job_results``).

Every move is one conditional statement on the job's state, so two writers never both win: a claim
(``queued`` to ``running``), a cancel or the queue's time limit (``queued`` to ``failed``), a requeue
(``running`` to ``queued``) and the end of a job (``running`` to an ended state). The transaction that
ends a job also writes its result and deletes its stored request, so a job is never ``succeeded``
without its bytes and no request outlives its job. Each of those statements reports whether it changed
a row: that is where a ``job.finished`` event will be written, in the same transaction, when the events
feed is built (ISS-46).

A claim has a number, the row's ``claims`` once it is made, and ``claim_job`` answers with it. A write
that names a claim applies only while the job is ``running`` under that number (``held``): a worker
whose claim was taken from it, because the job was put back and claimed again or ended by another
writer, changes nothing and is told so. A number is issued once: ``claims`` only ever grows. A claim
given back by a job that only has to wait is counted in ``claims_given_back``, and the claims a job
may use before its first attempt (``max_claims``) are the ones that were not given back.

Neither the stored request nor the stored result is encrypted here: the database is the operator's to
protect (docs/contracts/persistence.md).
"""

from collections.abc import Collection
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import and_, delete, exists, false, func, insert, or_, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from livepeer_builder.contracts import ActorContext, Failure, Job, JobRequest, JobState, ResultRef
from livepeer_builder.errors import NoCapacity
from livepeer_builder.store import schema as t
from livepeer_builder.store.rows import load_jobs

QUEUED, RUNNING = "queued", "running"
# What is kept of the actor a queued job runs as: who it is and what it may do, never how it signed in
# (``auth``) or what it is called (``display_name``).
ACTOR_SNAPSHOT = {"actor_id", "tenant_id", "permissions", "credential_kind", "attributes"}


def request_values(job_id: UUID, request: JobRequest, actor: ActorContext) -> dict[str, Any]:
    """The ``lpb_job_requests`` row of a queued job. Only what the caller set is stored, so the request
    read back passes the same checks; the raw body is stored as bytes, not as base64 inside the JSON."""
    snapshot = actor.model_dump(mode="json", include=ACTOR_SNAPSHOT)
    return {
        "job_id": str(job_id),
        "request": request.model_dump(mode="json", exclude={"raw_body"}, exclude_unset=True),
        "raw_body": request.raw_body,
        "actor": snapshot | {"permissions": sorted(snapshot["permissions"])},
    }


QUEUE_LOCK = 0x4C50424A  # PostgreSQL's advisory lock for the queue's limit: one key, the engine's own


async def room(conn: AsyncConnection, max_queued: int) -> None:
    """Refuse (``NoCapacity``) when the queue already holds ``max_queued`` jobs. Asked in the transaction
    that stores a job, behind a lock every such transaction takes first, so no two of them see the same
    free place: the limit holds for submits at the same moment (T45, closed).

    On PostgreSQL the lock is a transaction-level advisory lock, which the commit or the rollback
    releases. SQLite has one writer at a time: a write that changes no row makes this transaction that
    writer before it counts."""
    if conn.dialect.name == "postgresql":
        await conn.execute(select(func.pg_advisory_xact_lock(QUEUE_LOCK)))
    else:
        await conn.execute(update(t.jobs).where(false()).values(claims=t.jobs.c.claims))
    queued = (await conn.execute(select(func.count()).where(t.jobs.c.state == QUEUED))).scalar_one()
    if queued >= max_queued:
        raise NoCapacity("the queue is full; submit again later")


def held(job_id: UUID, claim: int | None) -> Any:
    """The job while it is ``running`` and, when a claim is named, under that claim."""
    mine = and_(t.jobs.c.id == str(job_id), t.jobs.c.state == RUNNING)
    return mine if claim is None else and_(mine, t.jobs.c.claims == claim)


async def holds(conn: AsyncConnection, job_id: UUID, claim: int) -> bool:
    """Whether ``claim`` still holds the job, asked as a write that changes nothing: the transaction
    starts as a writer, as ``_end`` does, so the answer stands until it commits."""
    kept = update(t.jobs).where(held(job_id, claim)).values(claims=t.jobs.c.claims)
    return (await conn.execute(kept)).rowcount == 1


async def end_running(
    conn: AsyncConnection,
    job_id: UUID,
    state: JobState,
    failure: Failure | None,
    result: ResultRef | None,
    completed_at: datetime,
    claim: int | None = None,
) -> bool:
    """``running`` to an ended state, and the stored request deleted. ``False`` when it was not running,
    or not under ``claim`` when one is named."""
    which = t.jobs.c.id == str(job_id)
    if claim is not None:
        which = and_(which, t.jobs.c.claims == claim)
    return await _end(conn, which, RUNNING, state, failure, result, completed_at) == 1


async def _end(
    conn: AsyncConnection,
    which: Any,
    from_state: str,
    state: JobState,
    failure: Failure | None,
    result: ResultRef | None,
    completed_at: datetime,
) -> int:
    """Move the jobs ``which`` names out of ``from_state``, then delete the stored requests of the jobs
    that are now in ``state``. The update is the guard and comes first, so the transaction starts as a
    writer: a job this call did not move keeps its request, because it is still queued or running."""
    values = {
        "state": state,
        "failure": failure.model_dump(mode="json") if failure else None,
        "result": result.model_dump(mode="json") if result else None,
        "completed_at": completed_at,
    }
    ended = await conn.execute(update(t.jobs).where(which, t.jobs.c.state == from_state).values(values))
    if ended.rowcount:
        now_ended = select(t.jobs.c.id).where(which, t.jobs.c.state == state)
        await conn.execute(delete(t.job_requests).where(t.job_requests.c.job_id.in_(now_ended)))
    return int(ended.rowcount)


class SqlJobQueue:
    """The queue half of ``JobRepo`` for ``SqlStore``."""

    engine: AsyncEngine

    async def queued_jobs(self, limit: int, skip_apps: Collection[str] = ()) -> list[tuple[UUID, str]]:
        query = select(t.jobs.c.id, t.jobs.c.app).where(t.jobs.c.state == QUEUED)
        if skip_apps:
            query = query.where(t.jobs.c.app.not_in(sorted(skip_apps)))
        query = query.order_by(t.jobs.c.created_at, t.jobs.c.id).limit(limit)
        async with self.engine.connect() as conn:
            return [(UUID(job_id), app) for job_id, app in await conn.execute(query)]

    async def queued_stats(self) -> tuple[int, datetime | None]:
        query = select(func.count(), func.min(t.jobs.c.created_at)).where(t.jobs.c.state == QUEUED)
        async with self.engine.connect() as conn:
            count, oldest = (await conn.execute(query)).one()
        return int(count), oldest

    async def claim_job(
        self, job_id: UUID, started_at: datetime
    ) -> tuple[Job, JobRequest, ActorContext, int] | None:
        mine = and_(t.jobs.c.id == str(job_id), t.jobs.c.state == QUEUED)
        claim = {"state": RUNNING, "started_at": started_at, "claims": t.jobs.c.claims + 1}
        async with self.engine.begin() as conn:
            if (await conn.execute(update(t.jobs).where(mine).values(claim))).rowcount != 1:
                return None
            row = (await conn.execute(select(t.jobs).where(t.jobs.c.id == str(job_id)))).mappings().one()
            kept = (
                (await conn.execute(select(t.job_requests).where(t.job_requests.c.job_id == str(job_id))))
                .mappings()
                .one()
            )
            [job] = await load_jobs(conn, [row])
        request = JobRequest.model_validate(dict(kept["request"]) | _raw_body(kept["raw_body"]))
        return job, request, ActorContext.model_validate(kept["actor"]), int(row["claims"])

    async def requeue_unstarted(
        self, job_id: UUID, max_claims: int, *, claim: int | None = None, counted: bool = True
    ) -> bool:
        attempted = exists().where(t.attempts.c.job_id == t.jobs.c.id)
        kept = exists().where(t.job_requests.c.job_id == t.jobs.c.id)
        unstarted = and_(held(job_id, claim), ~attempted, kept)
        back: dict[str, Any] = {"state": QUEUED, "started_at": None}
        given_back = t.jobs.c.claims_given_back
        if counted:
            unstarted = and_(unstarted, t.jobs.c.claims - given_back < max_claims)
        else:  # the claim is given back: it never counts against ``max_claims``, and its number is spent
            back["claims_given_back"] = given_back + 1
        async with self.engine.begin() as conn:
            moved = await conn.execute(update(t.jobs).where(unstarted).values(back))
        return moved.rowcount == 1

    async def cancel_queued(self, job_id: UUID, failure: Failure, completed_at: datetime) -> bool:
        async with self.engine.begin() as conn:
            which = t.jobs.c.id == str(job_id)
            return await _end(conn, which, QUEUED, "failed", failure, None, completed_at) == 1

    async def expire_queued(self, created_before: datetime, failure: Failure, completed_at: datetime) -> int:
        async with self.engine.begin() as conn:
            which = t.jobs.c.created_at < created_before
            return await _end(conn, which, QUEUED, "failed", failure, None, completed_at)

    async def finish_job_with_result(
        self,
        job_id: UUID,
        result: ResultRef,
        content: bytes,
        completed_at: datetime,
        *,
        claim: int | None = None,
    ) -> bool:
        kept = {
            "job_id": str(job_id),
            "content": content,
            "content_type": result.content_type,
            "size_bytes": result.size_bytes,
            "sha256": result.sha256,
            "expires_at": result.expires_at,
        }
        async with self.engine.begin() as conn:  # all of it, or none of it
            if not await end_running(conn, job_id, "succeeded", None, result, completed_at, claim):
                return False
            await conn.execute(insert(t.job_results).values(kept))
        return True

    async def read_result(self, job_id: UUID, now: datetime) -> bytes | None:
        live = or_(t.job_results.c.expires_at.is_(None), t.job_results.c.expires_at > now)
        query = select(t.job_results.c.content).where(t.job_results.c.job_id == str(job_id), live)
        async with self.engine.connect() as conn:
            content = (await conn.execute(query)).scalar_one_or_none()
        return None if content is None else bytes(content)


def _raw_body(stored: Any) -> dict[str, bytes]:
    return {} if stored is None else {"raw_body": bytes(stored)}
