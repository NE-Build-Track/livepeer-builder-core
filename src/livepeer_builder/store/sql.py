"""SqlStore: the EngineStore over SQLAlchemy Core (async). SQLite by default, PostgreSQL via [postgres]."""

from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import and_, delete, event, exists, func, insert, select, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool

from livepeer_builder.contracts import (
    AccessKey,
    Attempt,
    Failure,
    Fees,
    Job,
    JobCost,
    JobState,
    MeasuredUsage,
    ProvisionedActor,
    ResultRef,
    SecretRef,
    SignedSession,
    SyncCheckpoint,
    UsageRow,
)
from livepeer_builder.errors import OperationExists
from livepeer_builder.store import schema as t
from livepeer_builder.store.matching import match_payment_sessions
from livepeer_builder.store.migrate import upgrade

_CHUNK = 500


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
            return (await _load_jobs(conn, [row]))[0], row["request_fingerprint"]

    async def get_job(self, job_id: UUID) -> Job | None:
        async with self.engine.connect() as conn:
            row = (await conn.execute(select(t.jobs).where(t.jobs.c.id == str(job_id)))).mappings().first()
            return (await _load_jobs(conn, [row]))[0] if row else None

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
            return await _load_jobs(conn, list((await conn.execute(query)).mappings()))

    async def jobs_between(
        self, application_id: str, actor_id: str | None, start: datetime, end: datetime
    ) -> list[Job]:
        query = select(t.jobs).where(
            t.jobs.c.application_id == application_id, t.jobs.c.created_at >= start, t.jobs.c.created_at < end
        )
        if actor_id is not None:
            query = query.where(t.jobs.c.actor_id == actor_id)
        async with self.engine.connect() as conn:
            return await _load_jobs(
                conn, list((await conn.execute(query.order_by(t.jobs.c.created_at))).mappings())
            )

    async def jobs_in_state(self, state: JobState) -> list[Job]:
        async with self.engine.connect() as conn:
            rows = list((await conn.execute(select(t.jobs).where(t.jobs.c.state == state))).mappings())
            return await _load_jobs(conn, rows)

    async def start_attempt(self, attempt: Attempt) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(insert(t.attempts).values(_attempt_values(attempt)))

    async def finish_attempt(self, attempt: Attempt) -> None:
        key = and_(t.attempts.c.job_id == str(attempt.job_id), t.attempts.c.number == attempt.number)
        session_key = and_(
            t.payment_sessions.c.job_id == str(attempt.job_id),
            t.payment_sessions.c.attempt_number == attempt.number,
        )
        async with self.engine.begin() as conn:
            await conn.execute(update(t.attempts).where(key).values(_attempt_values(attempt)))
            await conn.execute(delete(t.payment_sessions).where(session_key))
            for seq, session in enumerate(attempt.sessions):
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
        """Insert rows not seen before and move the cursor, in one transaction. Returns the new rows."""
        new: list[UsageRow] = []
        synced_at = datetime.now(UTC)
        async with self.engine.begin() as conn:
            seen: set[str] = set()
            for chunk in _chunks([row.id for row in rows]):
                found = await conn.execute(select(t.usage_rows.c.id).where(t.usage_rows.c.id.in_(chunk)))
                seen.update(found.scalars())
            for row in rows:
                if row.id in seen:
                    continue
                seen.add(row.id)
                await conn.execute(insert(t.usage_rows).values(_usage_values(row) | {"synced_at": synced_at}))
                new.append(row)
            cursor = {"resume_cursor": checkpoint.resume_cursor, "filters": checkpoint.filters}
            moved = await conn.execute(update(t.cost_cursor).where(t.cost_cursor.c.id == 1).values(cursor))
            if moved.rowcount == 0:
                await conn.execute(insert(t.cost_cursor).values(id=1, **cursor))
        return new

    async def match_sessions(self, rows: Sequence[UsageRow]) -> set[UUID]:
        """Resolve session-log entries on (allocation, manifest). Returns jobs to recompute."""
        async with self.engine.begin() as conn:
            return await match_payment_sessions(conn, rows)

    async def usage_for_payment_sessions(self, payment_session_ids: Sequence[str]) -> list[UsageRow]:
        rows: list[UsageRow] = []
        async with self.engine.connect() as conn:
            for chunk in _chunks(list(payment_session_ids)):
                query = select(t.usage_rows).where(t.usage_rows.c.payment_session_id.in_(chunk))
                rows.extend(_usage_row(row) for row in (await conn.execute(query)).mappings())
        return rows

    async def unattributed_usage(self, allocation_id: str) -> list[UsageRow]:
        attributed = exists().where(
            t.payment_sessions.c.payment_session_id == t.usage_rows.c.payment_session_id,
            t.payment_sessions.c.match_status == "matched",
        )
        query = select(t.usage_rows).where(
            t.usage_rows.c.allocation_id == allocation_id, t.usage_rows.c.status == "applied", ~attributed
        )
        async with self.engine.connect() as conn:
            return [_usage_row(row) for row in (await conn.execute(query)).mappings()]

    async def get_projection(self, job_id: UUID) -> JobCost | None:
        query = select(t.cost_projections).where(t.cost_projections.c.job_id == str(job_id))
        async with self.engine.connect() as conn:
            row = (await conn.execute(query)).mappings().first()
        if row is None:
            return None
        fee_usd = row["fee_usd"]
        return JobCost(
            job_id=job_id,
            status=row["status"],
            fees=Fees(
                fee_eth=Decimal(row["fee_eth"]),
                fee_usd=None if fee_usd is None else Decimal(fee_usd),
                row_count=row["row_count"],
            ),
            attempts=(),
            first_observed_at=row["first_observed_at"],
            updated_at=row["updated_at"],
        )

    async def save_projection(self, cost: JobCost) -> None:
        values = {
            "status": cost.status,
            "fee_eth": str(cost.fees.fee_eth),
            "fee_usd": None if cost.fees.fee_usd is None else str(cost.fees.fee_usd),
            "row_count": cost.fees.row_count,
            "first_observed_at": cost.first_observed_at,
            "updated_at": cost.updated_at,
        }
        key = t.cost_projections.c.job_id == str(cost.job_id)
        async with self.engine.begin() as conn:
            if (await conn.execute(update(t.cost_projections).where(key).values(values))).rowcount == 0:
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


async def _load_jobs(conn: AsyncConnection, rows: Sequence[Any]) -> list[Job]:
    ids = [row["id"] for row in rows]
    attempts_by_job: dict[str, list[dict[str, Any]]] = {job_id: [] for job_id in ids}
    sessions_by_attempt: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for chunk in _chunks(ids):
        found = await conn.execute(
            select(t.attempts).where(t.attempts.c.job_id.in_(chunk)).order_by(t.attempts.c.number)
        )
        for attempt in found.mappings():
            attempts_by_job[attempt["job_id"]].append(dict(attempt))
        sessions = await conn.execute(
            select(t.payment_sessions)
            .where(t.payment_sessions.c.job_id.in_(chunk))
            .order_by(t.payment_sessions.c.seq)
        )
        for session in sessions.mappings():
            key = (session["job_id"], session["attempt_number"])
            sessions_by_attempt.setdefault(key, []).append(dict(session))
    jobs: list[Job] = []
    for row in rows:
        attempts = tuple(
            Attempt(
                job_id=UUID(a["job_id"]),
                number=a["number"],
                runner_url=a["runner_url"],
                orchestrator_url=a["orchestrator_url"],
                allocation_id=a["allocation_id"],
                sessions=tuple(
                    SignedSession(
                        manifest_id=session["manifest_id"],
                        payment_session_id=session["payment_session_id"],
                        match=session["match_status"],
                    )
                    for session in sessions_by_attempt.get((a["job_id"], a["number"]), ())
                ),
                payment_sent=bool(a["payment_sent"]),
                outcome=a["outcome"],
                status_code=a["status_code"],
                started_at=a["started_at"],
                ended_at=a["ended_at"],
            )
            for a in attempts_by_job[row["id"]]
        )
        jobs.append(
            Job(
                id=UUID(row["id"]),
                actor_id=row["actor_id"],
                application_id=row["application_id"],
                operation_ref=row["operation_ref"],
                mode=row["mode"],
                app=row["app"],
                capability=row["capability"],
                model=row["model"],
                state=row["state"],
                attempts=attempts,
                failure=Failure.model_validate(row["failure"]) if row["failure"] else None,
                result=ResultRef.model_validate(row["result"]) if row["result"] else None,
                usage=MeasuredUsage.model_validate(row["usage"]) if row["usage"] else None,
                created_at=row["created_at"],
                completed_at=row["completed_at"],
            )
        )
    return jobs


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


def _usage_row(row: Any) -> UsageRow:
    data = {key: row[key] for key in UsageRow.model_fields}
    return UsageRow.model_validate(data)


def _actor(row: Any) -> ProvisionedActor:
    data = {key: row[key] for key in ProvisionedActor.model_fields if key != "secret_ref"}
    return ProvisionedActor.model_validate(data | {"secret_ref": SecretRef(ref=row["secret_ref"])})


def _access_key(row: Any) -> AccessKey:
    data = {key: row[key] for key in AccessKey.model_fields}
    data["permissions"] = tuple(row["permissions"])
    return AccessKey.model_validate(data)


def _chunks(values: list[str]) -> Iterable[list[str]]:
    for start in range(0, len(values), _CHUNK):
        yield values[start : start + _CHUNK]
