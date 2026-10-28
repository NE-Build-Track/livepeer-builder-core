"""SqlStore: the EngineStore over SQLAlchemy Core (async). SQLite by default, PostgreSQL via [postgres]."""

import asyncio
from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID
from weakref import WeakValueDictionary

from sqlalchemy import and_, delete, event, func, insert, select, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool

from livepeer_builder.contracts import (
    AccessKey,
    Attempt,
    Failure,
    Job,
    JobCost,
    JobState,
    MeasuredUsage,
    ProvisionedActor,
    ResultRef,
    SecretRef,
    SyncCheckpoint,
    UsageRow,
)
from livepeer_builder.errors import OperationExists
from livepeer_builder.store import cost_reads
from livepeer_builder.store import schema as t
from livepeer_builder.store.maintenance import SqlMaintenance
from livepeer_builder.store.matching import (
    bump_evidence,
    bump_evidence_on_new_rows,
    in_window,
    jobs_needing_projection,
    match_payment_sessions,
)
from livepeer_builder.store.migrate import upgrade
from livepeer_builder.store.rows import chunks, load_jobs


def create_engine(url: str) -> AsyncEngine:
    """An async engine with the pragmas SQLite needs (WAL, foreign keys, busy timeout)."""
    parsed = make_url(url)
    if parsed.get_backend_name() != "sqlite":
        return create_async_engine(url, pool_pre_ping=True)
    memory = parsed.database in (None, "", ":memory:")
    engine = create_async_engine(url, poolclass=StaticPool if memory else None)

    @event.listens_for(engine.sync_engine, "connect")
    def _pragmas(dbapi_connection: Any, _: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        if not memory:
            cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

    return engine


class SqlStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine
        self.maintenance = SqlMaintenance(engine)
        self._evidence_locks: WeakValueDictionary[tuple[str, int], asyncio.Lock] = WeakValueDictionary()

    @classmethod
    async def connect(cls, url: str, *, migrate: bool = True) -> "SqlStore":
        engine = create_engine(url)
        if migrate:
            await upgrade(engine)
        return cls(engine)

    async def close(self) -> None:
        await self.engine.dispose()

    # -- actors -------------------------------------------------------------------------------------

    async def get_actor(self, application_id: str, actor_id: str) -> ProvisionedActor | None:
        query = select(t.actors).where(
            t.actors.c.application_id == application_id, t.actors.c.actor_id == actor_id
        )
        async with self.engine.connect() as conn:
            row = (await conn.execute(query)).mappings().first()
        return _actor(row) if row else None

    async def save_actor(self, actor: ProvisionedActor) -> None:
        values = actor.model_dump(exclude={"secret_ref"}) | {"secret_ref": actor.secret_ref.ref}
        key = and_(t.actors.c.application_id == actor.application_id, t.actors.c.actor_id == actor.actor_id)
        async with self.engine.begin() as conn:
            result = await conn.execute(update(t.actors).where(key).values(values))
            if result.rowcount == 0:
                await conn.execute(insert(t.actors).values(values))

    async def list_actors(self, application_id: str | None = None) -> list[ProvisionedActor]:
        query = select(t.actors).order_by(t.actors.c.application_id, t.actors.c.actor_id)
        if application_id is not None:
            query = query.where(t.actors.c.application_id == application_id)
        async with self.engine.connect() as conn:
            return [_actor(row) for row in (await conn.execute(query)).mappings()]

    # -- access keys --------------------------------------------------------------------------------

    async def insert_access_key(self, key: AccessKey, token_hash: str) -> None:
        values = key.model_dump() | {"permissions": list(key.permissions), "token_hash": token_hash}
        async with self.engine.begin() as conn:
            await conn.execute(insert(t.access_keys).values(values))

    async def access_key_by_hash(self, token_hash: str) -> AccessKey | None:
        query = select(t.access_keys).where(t.access_keys.c.token_hash == token_hash)
        async with self.engine.connect() as conn:
            row = (await conn.execute(query)).mappings().first()
        return _access_key(row) if row else None

    async def revoke_access_key(self, key_id: str, revoked_at: datetime) -> bool:
        query = (
            update(t.access_keys)
            .where(t.access_keys.c.key_id == key_id, t.access_keys.c.revoked_at.is_(None))
            .values(revoked_at=revoked_at)
        )
        async with self.engine.begin() as conn:
            return (await conn.execute(query)).rowcount > 0

    async def get_access_key(self, key_id: str) -> AccessKey | None:
        query = select(t.access_keys).where(t.access_keys.c.key_id == key_id)
        async with self.engine.connect() as conn:
            row = (await conn.execute(query)).mappings().first()
        return _access_key(row) if row else None

    async def rotate_access_key(
        self, old_key_id: str, new_key: AccessKey, new_token_hash: str, revoked_at: datetime
    ) -> bool:
        revoke = (
            update(t.access_keys)
            .where(t.access_keys.c.key_id == old_key_id, t.access_keys.c.revoked_at.is_(None))
            .values(revoked_at=revoked_at)
        )
        values = new_key.model_dump() | {
            "permissions": list(new_key.permissions),
            "token_hash": new_token_hash,
        }
        async with self.engine.begin() as conn:
            if (await conn.execute(revoke)).rowcount == 0:
                return False
            await conn.execute(insert(t.access_keys).values(values))
        return True

    async def list_access_keys(self, application_id: str | None = None) -> list[AccessKey]:
        query = select(t.access_keys).order_by(t.access_keys.c.created_at)
        if application_id is not None:
            query = query.where(t.access_keys.c.application_id == application_id)
        async with self.engine.connect() as conn:
            return [_access_key(row) for row in (await conn.execute(query)).mappings()]

    async def count_active_admin_keys(self) -> int:
        query = select(func.count()).where(
            t.access_keys.c.kind == "admin", t.access_keys.c.revoked_at.is_(None)
        )
        async with self.engine.connect() as conn:
            return int((await conn.execute(query)).scalar_one())

    # -- jobs and attempts --------------------------------------------------------------------------

    async def create_job(self, job: Job, fingerprint: str | None) -> None:
        values = _job_values(job) | {"request_fingerprint": fingerprint}
        try:
            async with self.engine.begin() as conn:
                await conn.execute(insert(t.jobs).values(values))
        except IntegrityError:
            if job.operation_ref is None:
                raise
            found = await self.job_by_operation_ref(job.application_id, job.actor_id, job.operation_ref)
            if found is None:
                raise
            raise OperationExists(found[0]) from None

    async def job_by_operation_ref(
        self, application_id: str, actor_id: str, operation_ref: str
    ) -> tuple[Job, str | None] | None:
        query = select(t.jobs).where(
            t.jobs.c.application_id == application_id,
            t.jobs.c.actor_id == actor_id,
            t.jobs.c.operation_ref == operation_ref,
        )
        async with self.engine.connect() as conn:
            row = (await conn.execute(query)).mappings().first()
            if row is None:
                return None
            return (await load_jobs(conn, [row]))[0], row["request_fingerprint"]

    async def get_job(self, job_id: UUID) -> Job | None:
        async with self.engine.connect() as conn:
            row = (await conn.execute(select(t.jobs).where(t.jobs.c.id == str(job_id)))).mappings().first()
            return (await load_jobs(conn, [row]))[0] if row else None

    async def list_jobs(
        self, application_id: str, actor_id: str | None, limit: int = 50, before: datetime | None = None
    ) -> list[Job]:
        query = select(t.jobs).where(t.jobs.c.application_id == application_id)
        if actor_id is not None:
            query = query.where(t.jobs.c.actor_id == actor_id)
        if before is not None:
            query = query.where(t.jobs.c.created_at < before)
        query = query.order_by(t.jobs.c.created_at.desc()).limit(limit)
        async with self.engine.connect() as conn:
            return await load_jobs(conn, list((await conn.execute(query)).mappings()))

    async def jobs_between(
        self, application_id: str, actor_id: str | None, start: datetime, end: datetime
    ) -> list[Job]:
        query = select(t.jobs).where(*in_window(application_id, actor_id, start, end))
        async with self.engine.connect() as conn:
            return await load_jobs(
                conn, list((await conn.execute(query.order_by(t.jobs.c.created_at))).mappings())
            )

    async def jobs_in_state(self, state: JobState) -> list[Job]:
        async with self.engine.connect() as conn:
            rows = list((await conn.execute(select(t.jobs).where(t.jobs.c.state == state))).mappings())
            return await load_jobs(conn, rows)

    async def start_attempt(self, attempt: Attempt) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(insert(t.attempts).values(_attempt_values(attempt)))

    async def finish_attempt(self, attempt: Attempt) -> None:
        key = and_(t.attempts.c.job_id == str(attempt.job_id), t.attempts.c.number == attempt.number)
        async with self._evidence_lock(attempt), self.engine.begin() as conn:
            values = _attempt_values(attempt) | {"payment_sent": _paid(attempt)}  # never unpaid again (D54)
            await conn.execute(update(t.attempts).where(key).values(values))
            await _append_sessions(conn, attempt)

    async def record_payment_evidence(self, attempt: Attempt) -> None:
        key = and_(t.attempts.c.job_id == str(attempt.job_id), t.attempts.c.number == attempt.number)
        async with self._evidence_lock(attempt), self.engine.begin() as conn:  # outcome and timing stay
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
    ) -> None:
        values = {
            "state": state,
            "failure": failure.model_dump(mode="json") if failure else None,
            "result": result.model_dump(mode="json") if result else None,
            "completed_at": completed_at,
        }
        async with self.engine.begin() as conn:
            await conn.execute(update(t.jobs).where(t.jobs.c.id == str(job_id)).values(values))

    async def set_usage(self, job_id: UUID, usage: MeasuredUsage) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(
                update(t.jobs).where(t.jobs.c.id == str(job_id)).values(usage=usage.model_dump(mode="json"))
            )

    # -- costs ----------------------------------------------------------------------------------------

    async def cost_checkpoint(self) -> SyncCheckpoint | None:
        async with self.engine.connect() as conn:
            row = (
                (await conn.execute(select(t.cost_cursor).where(t.cost_cursor.c.id == 1))).mappings().first()
            )
        if row is None:
            return None
        return SyncCheckpoint(resume_cursor=row["resume_cursor"], filters=dict(row["filters"]))

    async def apply_usage(self, rows: Sequence[UsageRow], checkpoint: SyncCheckpoint) -> list[UsageRow]:
        """Insert rows not seen before and move the cursor, in one transaction. Returns the new rows.

        A new row on an already matched session bumps that job's evidence version in the same transaction."""
        new: list[UsageRow] = []
        synced_at = datetime.now(UTC)
        async with self.engine.begin() as conn:
            seen: set[str] = set()
            for chunk in chunks([row.id for row in rows]):
                found = await conn.execute(select(t.usage_rows.c.id).where(t.usage_rows.c.id.in_(chunk)))
                seen.update(found.scalars())
            for row in rows:
                if row.id in seen:
                    continue
                seen.add(row.id)
                await conn.execute(insert(t.usage_rows).values(_usage_values(row) | {"synced_at": synced_at}))
                new.append(row)
            await bump_evidence_on_new_rows(conn, new)
            cursor = {"resume_cursor": checkpoint.resume_cursor, "filters": checkpoint.filters}
            moved = await conn.execute(update(t.cost_cursor).where(t.cost_cursor.c.id == 1).values(cursor))
            if moved.rowcount == 0:
                await conn.execute(insert(t.cost_cursor).values(id=1, **cursor))
        return new

    async def match_sessions(self, rows: Sequence[UsageRow]) -> set[UUID]:
        """Resolve session-log entries on (allocation, manifest). Returns jobs to recompute, and bumps their
        evidence versions in the same transaction."""
        async with self.engine.begin() as conn:
            changed = await match_payment_sessions(conn, rows)
            await bump_evidence(conn, (str(job_id) for job_id in changed))
            return changed

    async def jobs_for_projection(self, job_ids: Sequence[UUID]) -> list[Job]:
        selected = (
            select(t.jobs.c.id)
            .outerjoin(t.cost_projections, t.cost_projections.c.job_id == t.jobs.c.id)
            .where(jobs_needing_projection([str(job_id) for job_id in job_ids]))
            .distinct()
        )
        query = select(t.jobs).where(t.jobs.c.id.in_(selected)).order_by(t.jobs.c.created_at)
        async with self.engine.connect() as conn:
            return await load_jobs(conn, list((await conn.execute(query)).mappings()))

    async def job_for_projection(self, job_id: UUID) -> tuple[Job, int] | None:
        """The job and its evidence version, read first: evidence changed after this read bumps it again,
        so a projection saved with this version can never claim evidence it did not see."""
        async with self.engine.connect() as conn:
            query = select(t.jobs.c.evidence_version).where(t.jobs.c.id == str(job_id))
            version = (await conn.execute(query)).scalar_one_or_none()
            if version is None:
                return None
            rows = list((await conn.execute(select(t.jobs).where(t.jobs.c.id == str(job_id)))).mappings())
            [job] = await load_jobs(conn, rows)
        return job, version

    async def usage_for_payment_sessions(self, payment_session_ids: Sequence[str]) -> list[UsageRow]:
        async with self.engine.connect() as conn:
            return await cost_reads.usage_for_payment_sessions(conn, payment_session_ids)

    async def unattributed_usage(self, allocation_id: str) -> list[UsageRow]:
        async with self.engine.connect() as conn:
            return await cost_reads.unattributed_usage(conn, allocation_id)

    async def get_projection(self, job_id: UUID) -> JobCost | None:
        async with self.engine.connect() as conn:
            return await cost_reads.get_projection(conn, job_id)

    def cost_snapshot(self) -> AbstractAsyncContextManager[cost_reads.SqlCostSnapshot]:
        return cost_reads.snapshot(self.engine)

    async def save_projection(self, cost: JobCost, *, evidence_version: int) -> None:
        """Store the cost computed from ``evidence_version``. Never replaces one computed from newer evidence
        (a slower concurrent sync)."""
        values = {
            "status": cost.status,
            "fee_eth": str(cost.fees.fee_eth),
            "fee_usd": None if cost.fees.fee_usd is None else str(cost.fees.fee_usd),
            "row_count": cost.fees.row_count,
            "attempts": [attempt.model_dump(mode="json") for attempt in cost.attempts],
            "first_observed_at": cost.first_observed_at,
            "updated_at": cost.updated_at,
            "evidence_version": evidence_version,
        }
        key = t.cost_projections.c.job_id == str(cost.job_id)
        not_newer = t.cost_projections.c.evidence_version <= evidence_version
        async with self.engine.begin() as conn:
            if (await conn.execute(update(t.cost_projections).where(key, not_newer).values(values))).rowcount:
                return
            if (await conn.execute(select(t.cost_projections.c.job_id).where(key))).first() is None:
                await conn.execute(insert(t.cost_projections).values(job_id=str(cost.job_id), **values))

    # -- secrets --------------------------------------------------------------------------------------

    async def put_secret(self, ref: str, ciphertext: str, created_at: datetime) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(
                insert(t.secrets).values(ref=ref, ciphertext=ciphertext, created_at=created_at)
            )

    async def get_secret(self, ref: str) -> str | None:
        async with self.engine.connect() as conn:
            value = (
                await conn.execute(select(t.secrets.c.ciphertext).where(t.secrets.c.ref == ref))
            ).scalar()
        return None if value is None else str(value)

    async def delete_secret(self, ref: str) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(delete(t.secrets).where(t.secrets.c.ref == ref))


# -- row mapping --------------------------------------------------------------------------------------


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
    known = [
        row[0] for row in await conn.execute(select(t.payment_sessions.c.manifest_id).where(session_key))
    ]
    for session in attempt.sessions:
        if session.manifest_id in known:
            await conn.execute(
                update(t.payment_sessions)
                .where(
                    session_key,
                    t.payment_sessions.c.manifest_id == session.manifest_id,
                    t.payment_sessions.c.payment_state != "paid",
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
        "application_id": job.application_id,
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
    }


def _attempt_values(attempt: Attempt) -> dict[str, Any]:
    values = attempt.model_dump(exclude={"sessions"})
    values["job_id"] = str(attempt.job_id)
    return values


def _usage_values(row: UsageRow) -> dict[str, Any]:
    values = row.model_dump()
    for name in ("computed_fee_eth", "computed_fee_usd"):
        values[name] = None if values[name] is None else str(values[name])
    return values


def _actor(row: Any) -> ProvisionedActor:
    data = {key: row[key] for key in ProvisionedActor.model_fields if key != "secret_ref"}
    return ProvisionedActor.model_validate(data | {"secret_ref": SecretRef(ref=row["secret_ref"])})


def _access_key(row: Any) -> AccessKey:
    data = {key: row[key] for key in AccessKey.model_fields}
    data["permissions"] = tuple(row["permissions"])
    return AccessKey.model_validate(data)
