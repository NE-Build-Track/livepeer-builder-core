"""SqlStore: the EngineStore over SQLAlchemy Core (async). SQLite by default, PostgreSQL via [postgres]."""

import asyncio
import logging
from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID
from weakref import WeakValueDictionary

from sqlalchemy import delete, event, func, insert, select, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool

from livepeer_builder.contracts import (
    ActorAllocation,
    Job,
    JobCost,
    SeenActor,
    SetAsideUsage,
    SyncCheckpoint,
    UsageRow,
)
from livepeer_builder.store import actors, cost_reads
from livepeer_builder.store import schema as t
from livepeer_builder.store.jobs import SqlJobs
from livepeer_builder.store.keys import SqlKeys
from livepeer_builder.store.maintenance import SqlMaintenance
from livepeer_builder.store.matching import (
    bump_evidence,
    bump_evidence_on_new_rows,
    jobs_needing_projection,
    match_payment_sessions,
)
from livepeer_builder.store.migrate import upgrade
from livepeer_builder.store.operations import SqlOperations
from livepeer_builder.store.payment_keys import SqlPaymentKeys
from livepeer_builder.store.rows import chunks, insert_once, load_jobs

_LOG = logging.getLogger(__name__)


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


class SqlStore(SqlJobs, SqlKeys, SqlOperations, actors.SqlActorCommands, SqlPaymentKeys):
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

    async def get_actor(self, tenant_id: str, actor_id: str) -> ActorAllocation | None:
        async with self.engine.connect() as conn:
            return await actors.get_actor(conn, tenant_id, actor_id)

    async def save_actor(self, actor: ActorAllocation) -> None:
        async with self.engine.begin() as conn:
            await actors.save_actor(conn, actor)

    async def list_actors(self, tenant_id: str | None = None) -> list[ActorAllocation]:
        async with self.engine.connect() as conn:
            return await actors.list_actors(conn, tenant_id)

    async def save_sighting(self, seen: SeenActor) -> None:
        async with self.engine.begin() as conn:
            await actors.save_sighting(conn, seen)

    async def get_sighting(self, tenant_id: str, actor_id: str) -> SeenActor | None:
        async with self.engine.connect() as conn:
            return await actors.get_sighting(conn, tenant_id, actor_id)

    async def list_sightings(self, tenant_id: str | None = None) -> list[SeenActor]:
        async with self.engine.connect() as conn:
            return await actors.list_sightings(conn, tenant_id)

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

        A new row on an already matched session bumps that job's evidence version in the same transaction.
        A row id seen before keeps its stored copy; delivered again with another fee, it is logged (D61).
        Two syncs may write one page at once (the loop and an admin's): each row is new to one of them."""
        new: list[UsageRow] = []
        synced_at = datetime.now(UTC)
        fees = (t.usage_rows.c.id, t.usage_rows.c.computed_fee_eth, t.usage_rows.c.computed_fee_usd)
        async with self.engine.begin() as conn:
            stored: dict[str, tuple[str | None, str | None]] = {}
            for chunk in chunks([row.id for row in rows]):
                found = await conn.execute(select(*fees).where(t.usage_rows.c.id.in_(chunk)))
                stored.update((row_id, (eth, usd)) for row_id, eth, usd in found)
            for row in rows:
                if row.id in stored:  # stored before, or earlier on this page (#83)
                    _log_another_fee(row, *stored[row.id])
                    continue
                stored[row.id] = _fee_text(row)
                values = _usage_values(row) | {"synced_at": synced_at}
                if (await conn.execute(insert_once(conn, t.usage_rows).values(values))).rowcount:
                    new.append(row)  # not new when another sync stored it since this one looked
            await bump_evidence_on_new_rows(conn, new)
            cursor = {"resume_cursor": checkpoint.resume_cursor, "filters": checkpoint.filters}
            moved = await conn.execute(update(t.cost_cursor).where(t.cost_cursor.c.id == 1).values(cursor))
            if moved.rowcount == 0:  # the first sync; of two at once, one cursor is kept
                await conn.execute(insert_once(conn, t.cost_cursor).values(id=1, **cursor))
        return new

    async def set_aside_usage(self, rows: Sequence[SetAsideUsage]) -> list[SetAsideUsage]:
        """Record rows cost sync left out, once per id (#114, C13). Returns the ones not recorded before;
        the first record of an id is kept, also when two syncs record it at once. ``raw`` is not stored."""
        new: list[SetAsideUsage] = []
        table, at = t.usage_set_aside, datetime.now(UTC)
        async with self.engine.begin() as conn:
            known: set[str] = set()
            for chunk in chunks([row.id for row in rows]):
                known.update((await conn.execute(select(table.c.id).where(table.c.id.in_(chunk)))).scalars())
            for row in (row for row in rows if row.id not in known):
                known.add(row.id)  # the same id later on this page is the same row
                fees = _fee_text(row)
                values = {"id": row.id, "reason": row.reason, "set_aside_at": at}
                values |= {"computed_fee_eth": fees[0], "computed_fee_usd": fees[1]}
                # Another sync may have recorded it since this one looked: then nothing is written.
                if (await conn.execute(insert_once(conn, table).values(values))).rowcount:
                    new.append(row)
        return new

    async def count_set_aside_usage(self) -> int:
        async with self.engine.connect() as conn:
            return int((await conn.execute(select(func.count()).select_from(t.usage_set_aside))).scalar_one())

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


def _fee_text(row: UsageRow | SetAsideUsage) -> tuple[str | None, str | None]:
    """A row's fees as the store holds them, to compare a later copy of the same id against."""
    eth, usd = row.computed_fee_eth, row.computed_fee_usd
    return (None if eth is None else str(eth), None if usd is None else str(usd))


def _log_another_fee(row: UsageRow, eth: str | None, usd: str | None) -> None:
    """A usage row id delivered again with a fee other than the stored one is ignored, never a correction
    (D61, Q2): only a new row id changes a job's cost. The difference is logged for the operator."""
    stored = (None if eth is None else Decimal(eth), None if usd is None else Decimal(usd))
    if stored == (row.computed_fee_eth, row.computed_fee_usd):
        return
    _LOG.warning(
        "a usage row was delivered again with another fee; the stored copy is kept",
        extra={
            "event": "costs.usage_redelivered",
            "row_id": row.id,
            "stored_fee_eth": eth,
            "delivered_fee_eth": None if row.computed_fee_eth is None else str(row.computed_fee_eth),
            "stored_fee_usd": usd,
            "delivered_fee_usd": None if row.computed_fee_usd is None else str(row.computed_fee_usd),
        },
    )


def _usage_values(row: UsageRow) -> dict[str, Any]:
    values = row.model_dump()
    for name in ("computed_fee_eth", "computed_fee_usd"):
        values[name] = None if values[name] is None else str(values[name])
    return values
