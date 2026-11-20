"""ISS-26: migration 0016, which makes ``lpb_jobs`` a queue and adds the stored request and the stored
result of an async job. SQLite and PostgreSQL."""

from pathlib import Path
from typing import Any

from sqlalchemy import insert, inspect, select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from livepeer_builder.store import SqlStore, create_engine, current_revision, head_revision, upgrade
from livepeer_builder.store import schema as t
from livepeer_builder.store.migrate import downgrade
from livepeer_builder.testing import FixedClock
from tests.store.conftest import make_job
from tests.store.test_domain_language_names import interrupted

NEW_TABLES = {"lpb_job_requests", "lpb_job_results"}
NEW_COLUMNS = {"started_at", "claims"}


async def tables(engine: AsyncEngine) -> set[str]:
    async with engine.connect() as conn:
        return set(await conn.run_sync(lambda sync: inspect(sync).get_table_names()))


async def job_columns(engine: AsyncEngine) -> set[str]:
    async with engine.connect() as conn:
        found = await conn.run_sync(lambda sync: inspect(sync).get_columns("lpb_jobs"))
    return {column["name"] for column in found}


async def job_row(engine: AsyncEngine, job_id: str) -> dict[str, Any]:
    async with engine.connect() as conn:
        found = await conn.execute(select(t.jobs).where(t.jobs.c.id == job_id))
        return dict(found.mappings().one())


async def test_the_migration_keeps_every_job_and_a_downgrade_ends_a_queued_one(
    database_url: str, clock: FixedClock
) -> None:
    """A job written at revision 0015 reads back unclaimed. A downgrade drops what 0016 added; a job
    still queued then could never run, so it ends ``failed`` and gives its reference up for a repeat."""
    engine = create_engine(database_url)
    try:
        await upgrade(engine, "0015")
        assert not NEW_TABLES & await tables(engine) and not NEW_COLUMNS & await job_columns(engine)
        store = SqlStore(engine)
        before = make_job(clock, ref="order-1")
        async with engine.begin() as conn:  # a record written by revision 0015's schema
            await conn.execute(
                text(
                    "INSERT INTO lpb_jobs (id, tenant_id, actor_id, operation_ref, mode, app, state, "
                    "created_at, completed_at) VALUES "
                    "(:id, 'default', 'alice', 'order-1', 'immediate', 'a/b', 'succeeded', :now, :now)"
                ),
                {"id": str(before.id), "now": clock.now()},
            )

        await upgrade(engine)
        assert await current_revision(engine) == head_revision() == "0016"
        assert await tables(engine) >= NEW_TABLES and await job_columns(engine) >= NEW_COLUMNS
        kept = await store.get_job(before.id)
        assert kept is not None and (kept.state, kept.operation_ref, kept.started_at) == (
            "succeeded",
            "order-1",
            None,
        )
        assert (await job_row(engine, str(before.id)))["claims"] == 0

        waiting = make_job(clock, ref="order-2", state="queued").model_copy(update={"mode": "async"})
        async with engine.begin() as conn:
            await conn.execute(
                insert(t.jobs).values(
                    id=str(waiting.id),
                    tenant_id="default",
                    actor_id="alice",
                    operation_ref="order-2",
                    mode="async",
                    app="a/b",
                    state="queued",
                    created_at=clock.now(),
                )
            )
            await conn.execute(
                insert(t.job_requests).values(job_id=str(waiting.id), request={"app": "a/b"}, actor={})
            )

        await downgrade(engine, "0015")
        assert await current_revision(engine) == "0015"
        assert not NEW_TABLES & await tables(engine) and not NEW_COLUMNS & await job_columns(engine)
        async with engine.connect() as conn:
            ended = await conn.execute(
                text("SELECT state, failure, completed_at FROM lpb_jobs WHERE id = :id"),
                {"id": str(waiting.id)},
            )
            state, failure, completed_at = ended.one()
        assert state == "failed" and "interrupted" in str(failure) and completed_at is not None

        await upgrade(engine)
        again = await store.get_job(waiting.id)
        assert again is not None and again.failure is not None
        assert (again.state, again.failure.kind, again.failure.payment_sent) == (
            "failed",
            "interrupted",
            False,
        )
        assert await store.get_job(before.id) == kept
    finally:
        await engine.dispose()


async def test_an_interrupted_run_on_sqlite_changes_nothing(tmp_path: Path) -> None:
    """SQLite's driver commits an ``ALTER TABLE`` that runs before any row is written. A run that died
    after it added the claim columns would leave them under revision 0015, and the next ``lpb migrate``
    would fail on a column that is already there. The run is one transaction, up and down."""
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'engine.db'}")
    try:
        await upgrade(engine, "0015")
        at_0015 = await tables(engine), await job_columns(engine)
        await interrupted(engine, "up", "head")
        assert await current_revision(engine) == "0015"
        assert (await tables(engine), await job_columns(engine)) == at_0015

        await upgrade(engine)
        at_head = await tables(engine), await job_columns(engine)
        await interrupted(engine, "down", "0015")
        assert await current_revision(engine) == head_revision()
        assert (await tables(engine), await job_columns(engine)) == at_head
    finally:
        await engine.dispose()
