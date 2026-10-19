"""ISS-06 evidence: migrations over retained records, schema drift, restart, backup/restore, retention."""

from datetime import timedelta
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import insert, inspect, text

from livepeer_builder import BuilderEngine, EngineSettings
from livepeer_builder.adapters.static import StaticDiscoverySource
from livepeer_builder.errors import InvalidRequest, UnsupportedForBackend
from livepeer_builder.store import (
    SqlStore,
    backup,
    create_engine,
    current_revision,
    head_revision,
    purge,
    restore,
    upgrade,
    verify,
)
from livepeer_builder.store import schema as t
from livepeer_builder.store.migrate import downgrade
from livepeer_builder.testing import FixedClock
from tests.store.conftest import make_attempt, make_job


async def test_upgrade_preserves_records_written_at_each_revision(
    database_url: str, clock: FixedClock
) -> None:
    engine = create_engine(database_url)
    await upgrade(engine, "0001")
    job = make_job(clock)
    async with engine.begin() as conn:  # a record written by revision 0001's schema
        await conn.execute(insert(t.jobs).values(
            id=str(job.id), application_id="default", actor_id="alice", operation_ref="r", mode="immediate",
            app="a/b", state="succeeded", created_at=clock.now(), completed_at=clock.now(),
        ))  # fmt: skip
    await upgrade(engine)
    assert await current_revision(engine) == head_revision()
    store = SqlStore(engine)
    loaded = await store.get_job(job.id)
    assert loaded is not None and loaded.operation_ref == "r"
    await downgrade(engine, "0001")  # the job row survives; 0001 has no session log
    async with engine.connect() as conn:
        kept = await conn.execute(
            text("SELECT operation_ref FROM lpb_jobs WHERE id = :id"), {"id": str(job.id)}
        )
    assert kept.scalar_one() == "r"
    await upgrade(engine)
    await engine.dispose()


async def test_session_log_drops_auth_ids_and_projections_keep_attempts(
    database_url: str, clock: FixedClock
) -> None:
    engine = create_engine(database_url)
    await upgrade(engine, "0002")
    job = make_job(clock)
    now = clock.now()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO lpb_jobs "
                "(id, application_id, actor_id, mode, app, state, created_at, completed_at) "
                "VALUES (:id, 'default', 'alice', 'immediate', 'a/b', 'succeeded', :now, :now)"
            ),
            {"id": str(job.id), "now": now},
        )
        await conn.execute(
            text(
                "INSERT INTO lpb_attempts "
                "(job_id, number, payment_sent, outcome, started_at, ended_at) "
                "VALUES (:id, 1, :paid, 'succeeded', :now, :now)"
            ),
            {"id": str(job.id), "paid": True, "now": now},
        )
        await conn.execute(
            text(
                "INSERT INTO lpb_attempt_auth_ids (auth_id, job_id, attempt_number) VALUES ('auth-1', :id, 1)"
            ),
            {"id": str(job.id)},
        )
        await conn.execute(
            text(
                "INSERT INTO lpb_cost_projections "
                "(job_id, status, fee_eth, row_count, updated_at) "
                "VALUES (:id, 'pending', '0', 0, :now)"
            ),
            {"id": str(job.id), "now": now},
        )

    await upgrade(engine, "0003")  # the session-log revision already on releases/m2

    async with engine.connect() as conn:
        tables = await conn.run_sync(lambda sync: set(inspect(sync).get_table_names()))
        columns = await conn.run_sync(
            lambda sync: {column["name"] for column in inspect(sync).get_columns("lpb_cost_projections")}
        )
    assert "lpb_attempt_auth_ids" not in tables
    assert "attempts" not in columns

    await upgrade(engine)

    async with engine.connect() as conn:
        stored = await conn.execute(
            text("SELECT attempts FROM lpb_cost_projections WHERE job_id = :id"),
            {"id": str(job.id)},
        )
    assert stored.scalar_one() in ("[]", [])
    await engine.dispose()


async def test_projection_attempts_survive_a_lab_0003_database(database_url: str) -> None:
    engine = create_engine(database_url)
    await upgrade(engine, "0003")
    async with engine.begin() as conn:  # the lab's 0003 added the attempt list and kept auth ids
        await conn.execute(
            text("ALTER TABLE lpb_cost_projections ADD COLUMN attempts JSON NOT NULL DEFAULT '[]'")
        )
        await conn.execute(
            text(
                "CREATE TABLE lpb_attempt_auth_ids ("
                "auth_id VARCHAR(200) NOT NULL, job_id VARCHAR(36) NOT NULL, attempt_number INTEGER NOT NULL, "
                "PRIMARY KEY (auth_id, job_id, attempt_number), "
                "FOREIGN KEY (job_id, attempt_number) REFERENCES lpb_attempts (job_id, number) ON DELETE CASCADE)"
            )
        )
    await upgrade(engine)
    assert await current_revision(engine) == head_revision()
    async with engine.connect() as conn:
        tables = await conn.run_sync(lambda sync: set(inspect(sync).get_table_names()))
    assert "lpb_attempt_auth_ids" not in tables
    await engine.dispose()


async def test_migrations_match_the_schema(database_url: str) -> None:
    engine = create_engine(database_url)
    await upgrade(engine)
    async with engine.connect() as conn:
        diffs = await conn.run_sync(
            lambda sync: compare_metadata(
                MigrationContext.configure(sync, opts={"compare_type": True}), t.metadata
            )
        )
    await engine.dispose()
    relevant = [d for d in diffs if "lpb_alembic_version" not in str(d)]
    assert relevant == [], f"schema.py and the migrations disagree; add a migration: {relevant}"


async def test_records_survive_restart_and_interrupted_jobs_resolve(
    database_url: str, clock: FixedClock
) -> None:
    first = await SqlStore.connect(database_url)
    running = make_job(clock, state="running")
    await first.create_job(running, None)
    await first.start_attempt(
        make_attempt(running.id, clock).model_copy(update={"outcome": "in_flight", "ended_at": None})
    )
    await first.close()  # the process dies mid-call

    settings = EngineSettings(database_url=database_url, log_json=False)
    engine = await BuilderEngine.from_settings(
        settings, clock=clock, discovery_source=StaticDiscoverySource()
    )
    await engine.start()
    recovered = await engine.store.get_job(running.id)
    await engine.stop()
    assert recovered is not None and recovered.state == "uncertain"


async def test_sqlite_backup_and_restore(tmp_path: Path, clock: FixedClock) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path / 'live.db'}"
    store = await SqlStore.connect(url)
    kept = make_job(clock)
    await store.create_job(kept, None)
    await store.close()

    copy = await backup(url, tmp_path / "backups" / "b1.db")
    assert verify(copy) == head_revision()

    store = await SqlStore.connect(url)
    await store.create_job(make_job(clock), None)  # written after the backup
    await store.close()

    await restore(url, copy)
    store = await SqlStore.connect(url)
    jobs = await store.list_jobs("default", "alice")
    await store.close()
    assert [job.id for job in jobs] == [kept.id]


async def test_backup_guards(tmp_path: Path) -> None:
    with pytest.raises(UnsupportedForBackend, match="pg_dump"):
        await backup("postgresql+asyncpg://u:p@db/x", tmp_path / "x.db")
    not_ours = tmp_path / "other.db"
    not_ours.write_bytes(b"")
    with pytest.raises(InvalidRequest):
        verify(not_ours)


async def test_retention_keeps_uncertain_and_recent_jobs(database_url: str, clock: FixedClock) -> None:
    store = await SqlStore.connect(database_url)
    old, recent, unresolved = make_job(clock), make_job(clock), make_job(clock, state="uncertain")
    for job in (old, unresolved):
        await store.create_job(job, None)
        await store.start_attempt(make_attempt(job.id, clock))
        await store.finish_attempt(make_attempt(job.id, clock, manifest_ids=(f"m-{job.id}",)))
    clock.advance(timedelta(days=40).total_seconds())
    await store.create_job(
        recent.model_copy(update={"created_at": clock.now(), "completed_at": clock.now()}), None
    )
    report = await purge(store.engine, clock.now(), jobs_days=30, usage_days=None)
    remaining = {job.id for job in await store.list_jobs("default", "alice")}
    await store.close()
    assert report.jobs_deleted == 1
    assert remaining == {recent.id, unresolved.id}
