"""#158: a migration run that dies changes nothing, at every revision, in both directions.

SQLite's driver opens a transaction at the first ``INSERT``, ``UPDATE`` or ``DELETE`` and not at a
``CREATE TABLE`` or an ``ALTER TABLE``. A migration's statements that ran before any row was written were
therefore committed one by one, and a run that died left them under the old revision: 0011 left
``challenge_ms`` on ``lpb_attempts`` at revision 0010, and every later ``lpb migrate`` failed with
"duplicate column name: challenge_ms"; its step down left ``_alembic_tmp_lpb_attempts``. The migration
environment opens the transaction itself before the first statement, so the tables, the columns and the
revision of a run are committed together or not at all. PostgreSQL always did that.
"""

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import event, inspect, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine

from livepeer_builder.store import SqlStore, create_engine, current_revision, head_revision, upgrade
from livepeer_builder.store.migrate import alembic_config, downgrade
from livepeer_builder.testing import FixedClock
from tests.store.conftest import make_attempt, make_job

TIMINGS = {"challenge_ms", "signing_ms", "response_ms"}  # the columns revision 0011 adds to lpb_attempts
COPY = "_alembic_tmp_lpb_attempts"  # the table 0011's step down copies the attempts into on SQLite
# Every revision with the one below it, oldest first.
STEPS = [
    (script.revision, str(script.down_revision or "base"))
    for script in reversed(list(ScriptDirectory.from_config(alembic_config()).walk_revisions()))
]


class _Killed(Exception):
    """The process died part-way through a migration run."""


class _Dies:
    """Counts the statements a run sends, and kills the run once ``after`` of them were sent or when the
    next one holds ``at``. With neither, the run is killed after its last statement, before its commit."""

    def __init__(self, after: int | None, at: str | None) -> None:
        self.after, self.at, self.sent = after, at, 0

    def __call__(self, _conn: Any, _cursor: Any, statement: str, *_: Any) -> None:
        if self.sent == self.after or (self.at is not None and self.at in statement):
            raise _Killed
        self.sent += 1


async def dies(
    engine: AsyncEngine, direction: str, revision: str, *, after: int | None = None, at: str | None = None
) -> int:
    """Run the migration to ``revision`` and kill it (see ``_Dies``). Answers how many statements ran."""
    run = command.upgrade if direction == "up" else command.downgrade
    killer = _Dies(after, at)
    event.listen(engine.sync_engine, "before_cursor_execute", killer)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(lambda sync: run(alembic_config(sync), revision))
            raise _Killed
    except _Killed:
        return killer.sent
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", killer)


async def read(engine: AsyncEngine, reader: Callable[[Any], Any]) -> Any:
    async with engine.connect() as conn:
        return await conn.run_sync(lambda sync: reader(inspect(sync)))


async def schema(engine: AsyncEngine) -> dict[str, Any]:
    """The revision the database records, and every table with its columns and indexes."""

    def tables(inspector: Any) -> dict[str, Any]:
        return {
            name: (
                [(c["name"], str(c["type"]), c["nullable"]) for c in inspector.get_columns(name)],
                sorted(str(index["name"]) for index in inspector.get_indexes(name)),
            )
            for name in inspector.get_table_names()
        }

    return {"revision": await current_revision(engine), "tables": await read(engine, tables)}


async def attempt_columns(engine: AsyncEngine) -> set[str]:
    return await read(engine, lambda inspector: {c["name"] for c in inspector.get_columns("lpb_attempts")})


async def table_names(engine: AsyncEngine) -> set[str]:
    return await read(engine, lambda inspector: set(inspector.get_table_names()))


@pytest.mark.parametrize("direction", ["up", "down"])
@pytest.mark.parametrize(("revision", "below"), STEPS, ids=[revision for revision, _ in STEPS])
async def test_a_run_that_dies_after_any_statement_changes_nothing(
    database_url: str, revision: str, below: str, direction: str
) -> None:
    """Each revision's step, up and down, killed after each of its statements in turn and once more
    before its commit: the database has the tables, the columns and the revision it had, and the next
    run goes through."""
    start, target = (below, revision) if direction == "up" else (revision, below)
    engine = create_engine(database_url)
    try:
        if start != "base":
            await upgrade(engine, start)
        before = await schema(engine)
        statements = await dies(engine, direction, target)
        assert await schema(engine) == before, f"{direction} {revision}: died before its commit"
        for sent in range(1, statements):
            await dies(engine, direction, target, after=sent)
            assert await schema(engine) == before, f"{direction} {revision}: died after {sent} statements"
        await (upgrade if direction == "up" else downgrade)(engine, target)
        assert await current_revision(engine) == (None if target == "base" else target)
    finally:
        await engine.dispose()


async def test_migration_0011_killed_between_two_columns_leaves_none_of_them(tmp_path: Path) -> None:
    """The step up killed between its first and its second ``ADD COLUMN`` left ``challenge_ms`` at
    revision 0010, and every later migrate failed with "duplicate column name: challenge_ms"."""
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'engine.db'}")
    try:
        await upgrade(engine, "0010")
        await dies(engine, "up", "0011", at="signing_ms")  # challenge_ms was added, signing_ms not yet
        assert await current_revision(engine) == "0010" and not TIMINGS & await attempt_columns(engine)
        await upgrade(engine)
        assert await current_revision(engine) == head_revision()
        assert await attempt_columns(engine) >= TIMINGS
    finally:
        await engine.dispose()


async def test_migration_0011_killed_on_its_way_down_leaves_no_copy_and_every_attempt(
    tmp_path: Path, clock: FixedClock
) -> None:
    """The step down copies the attempts into ``_alembic_tmp_lpb_attempts``. A run killed before its
    commit left that table behind, and the next one failed with "table ... already exists"."""
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'engine.db'}")
    try:
        await upgrade(engine)
        store, job = SqlStore(engine), make_job(clock)
        await store.create_job(job, None)
        await store.start_attempt(make_attempt(job.id, clock))
        stored = await store.get_job(job.id)
        assert stored is not None and len(stored.attempts) == 1
        await downgrade(engine, "0011")

        await dies(engine, "down", "0010")
        assert await current_revision(engine) == "0011" and COPY not in await table_names(engine)
        await downgrade(engine, "0010")
        assert not TIMINGS & await attempt_columns(engine)
        await upgrade(engine)
        assert await store.get_job(job.id) == stored
    finally:
        await engine.dispose()


async def test_a_database_an_earlier_interruption_left_half_migrated_is_refused_whole_until_repaired(
    tmp_path: Path,
) -> None:
    """A database that an interrupted run of an earlier release left with 0011's columns at revision
    0010, or with the copy of its step down, is not repaired by the engine: the migrate fails on the
    column or the table that is in its way and changes nothing. Dropping what was left lets it through
    (persistence.md)."""
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'engine.db'}")
    try:
        await upgrade(engine, "0010")
        async with engine.begin() as conn:
            await conn.execute(text("ALTER TABLE lpb_attempts ADD COLUMN challenge_ms INTEGER"))
        left = await schema(engine)
        with pytest.raises(OperationalError, match="duplicate column name: challenge_ms"):
            await upgrade(engine)
        assert await schema(engine) == left
        async with engine.begin() as conn:
            await conn.execute(text("ALTER TABLE lpb_attempts DROP COLUMN challenge_ms"))
        await upgrade(engine, "0011")
        assert await attempt_columns(engine) >= TIMINGS

        async with engine.begin() as conn:
            await conn.execute(text(f"CREATE TABLE {COPY} (job_id VARCHAR(36))"))
        left = await schema(engine)
        with pytest.raises(OperationalError, match=f"table {COPY} already exists"):
            await downgrade(engine, "0010")
        assert await schema(engine) == left
        async with engine.begin() as conn:
            await conn.execute(text(f"DROP TABLE {COPY}"))
        await downgrade(engine, "0010")
        assert await current_revision(engine) == "0010" and not TIMINGS & await attempt_columns(engine)
    finally:
        await engine.dispose()


async def test_a_migrate_on_sqlite_waits_for_another_one_and_goes_on_from_its_revision(
    tmp_path: Path,
) -> None:
    """The run takes SQLite's one write lock before it reads the revision. A second migrate at the same
    moment therefore waits for the first (up to the busy timeout) and goes on from the revision that one
    wrote. It read the old revision first, and then failed on the first column the other had added."""
    engine, other = (create_engine(f"sqlite+aiosqlite:///{tmp_path / 'engine.db'}") for _ in range(2))
    try:
        await upgrade(engine, "0010")
        async with other.connect() as first:  # another process, part-way through revision 0011
            await first.exec_driver_sql("BEGIN IMMEDIATE")
            second = asyncio.ensure_future(upgrade(engine))
            await asyncio.sleep(0.3)
            assert not second.done()
            for name in sorted(TIMINGS):
                await first.exec_driver_sql(f"ALTER TABLE lpb_attempts ADD COLUMN {name} INTEGER")
            await first.exec_driver_sql("UPDATE lpb_alembic_version SET version_num = '0011'")
            await first.exec_driver_sql("COMMIT")
        await second
        assert await current_revision(engine) == head_revision()
    finally:
        await engine.dispose()
        await other.dispose()


async def test_a_migrate_with_nothing_to_do_on_sqlite_takes_no_lock(tmp_path: Path) -> None:
    """An engine that starts on a migrated database only reads the revision: it does not wait for a
    writer, and is not refused "database is locked" when the write outlasts the busy timeout."""
    engine, other = (create_engine(f"sqlite+aiosqlite:///{tmp_path / 'engine.db'}") for _ in range(2))
    try:
        await upgrade(engine)
        async with other.connect() as writer:
            await writer.exec_driver_sql("BEGIN IMMEDIATE")
            await upgrade(engine)
            await upgrade(engine, "0016")
            await writer.exec_driver_sql("ROLLBACK")
    finally:
        await engine.dispose()
        await other.dispose()
