"""#115: ``lpb_actors.secret_ref`` is unbounded text (migration 0013), on SQLite and PostgreSQL.

The column was ``varchar(200)``. SQLite ignores the length, but PostgreSQL enforces it, and the actor row
is written after the provider key is funded: a supplied secret store whose reference is longer (a vault
path) left a funded allocation with no record. A reference is opaque text of any length.
"""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import Text, func, insert, inspect, select, text
from sqlalchemy.exc import IntegrityError, NoSuchTableError
from sqlalchemy.ext.asyncio import AsyncEngine

from livepeer_builder.contracts import ActorAllocation, SecretRef
from livepeer_builder.store import SqlStore, create_engine, current_revision, head_revision, upgrade
from livepeer_builder.store import schema as t
from livepeer_builder.store.migrate import alembic_config, downgrade
from livepeer_builder.testing import FixedClock
from tests.store.conftest import as_before_0014

LONG = "vault:kv/data/livepeer/" + "p" * 5000


@pytest.fixture
async def sql(database_url: str) -> AsyncIterator[SqlStore]:
    store = await SqlStore.connect(database_url)
    yield store
    await store.close()


def actor(
    clock: FixedClock, actor_id: str = "alice", allocation_id: str = "a1", ref: str = "db:1"
) -> ActorAllocation:
    return ActorAllocation(
        tenant_id="default", actor_id=actor_id, grant_id="g", allocation_id=allocation_id,
        api_key_id="k1", secret_ref=SecretRef(ref=ref), currency="USD", status="active",
        created_at=clock.now(), updated_at=clock.now(), label="Alice",
    )  # fmt: skip


# Revision 0012 names an actor's tenant ``application_id`` (0014 renamed it, #163), so a row is written
# and read there under that name, not through the store.
ACTORS_AT_0012 = as_before_0014(t.actors)


def row_at_0012(actor: ActorAllocation) -> dict[str, object]:
    values = actor.model_dump(exclude={"secret_ref", "tenant_id", "credential_custody"})  # 0015's column
    return values | {"application_id": actor.tenant_id, "secret_ref": actor.secret_ref.ref}


async def save_at_0012(engine: AsyncEngine, actor: ActorAllocation) -> None:
    async with engine.begin() as conn:
        await conn.execute(insert(ACTORS_AT_0012).values(row_at_0012(actor)))


async def alice_at_0012(engine: AsyncEngine) -> ActorAllocation:
    async with engine.connect() as conn:
        found = await conn.execute(select(ACTORS_AT_0012).where(ACTORS_AT_0012.c.actor_id == "alice"))
        row = dict(found.mappings().one())
    row["tenant_id"] = row.pop("application_id")
    return ActorAllocation.model_validate(row | {"secret_ref": SecretRef(ref=row["secret_ref"])})


async def secret_ref_length(engine: AsyncEngine) -> int | None:
    """The declared length of the column in the database itself; ``None`` is unbounded."""
    async with engine.connect() as conn:
        columns = await conn.run_sync(lambda sync: inspect(sync).get_columns("lpb_actors"))
    [column] = [column for column in columns if column["name"] == "secret_ref"]
    return getattr(column["type"], "length", None)


def test_the_schema_declares_the_reference_as_unbounded_text() -> None:
    column = t.actors.c.secret_ref
    assert isinstance(column.type, Text) and column.type.length is None and column.nullable is False


async def test_the_migrated_column_has_no_length(sql: SqlStore) -> None:
    assert await current_revision(sql.engine) == head_revision()
    assert await secret_ref_length(sql.engine) is None


async def test_an_actor_keeps_a_secret_reference_of_any_length(sql: SqlStore, clock: FixedClock) -> None:
    """On PostgreSQL this write was refused: "value too long for type character varying(200)"."""
    saved = actor(clock, ref=LONG)
    await sql.save_actor(saved)
    assert await sql.get_actor("default", "alice") == saved
    rotated = await sql.set_actor_key(
        "default", "alice", "a1", "k2", SecretRef(ref=LONG + "/v2"), clock.now(), only_from="k1"
    )
    assert rotated is not None and rotated.secret_ref.ref == LONG + "/v2"
    assert await sql.list_actors("default") == [rotated]


async def test_existing_actors_survive_the_change_and_a_downgrade(
    database_url: str, clock: FixedClock
) -> None:
    engine = create_engine(database_url)
    try:
        await upgrade(engine, "0012")
        assert await secret_ref_length(engine) == 200
        before = actor(clock)
        await save_at_0012(engine, before)  # a row written by revision 0012's schema

        await upgrade(engine)
        assert await current_revision(engine) == head_revision() == "0015"
        assert await secret_ref_length(engine) is None
        store = SqlStore(engine)
        assert await store.get_actor("default", "alice") == before
        # the rest of the table is as it was: an allocation still belongs to one actor, a label defaults
        with pytest.raises(IntegrityError):
            await store.save_actor(actor(clock, actor_id="bob", allocation_id="a1"))
        values = before.model_dump(exclude={"secret_ref"}) | {"secret_ref": before.secret_ref.ref}
        unlabelled = {key: value for key, value in values.items() if key != "label"}
        async with engine.begin() as conn:
            await conn.execute(
                insert(t.actors).values(unlabelled | {"actor_id": "carol", "allocation_id": "a3"})
            )
            label = await conn.execute(select(t.actors.c.label).where(t.actors.c.actor_id == "carol"))
            assert label.scalar_one() == ""

        await downgrade(engine, "0012")  # no reference here is over 200 characters, so it goes back
        assert await secret_ref_length(engine) == 200
        assert await alice_at_0012(engine) == before
        await upgrade(engine)
        assert await store.get_actor("default", "alice") == before
    finally:
        await engine.dispose()


# -- SQLite: a run of the migration that died before its commit ---------------------------------------------

COPY = "_alembic_tmp_lpb_actors"  # the table Alembic's batch mode copies the rows into on SQLite


class _Killed(Exception):
    """The process died after the migration ran and before its transaction committed."""


async def interrupted(engine: AsyncEngine, direction: str, revision: str) -> None:
    run = command.upgrade if direction == "up" else command.downgrade
    try:
        async with engine.begin() as conn:
            await conn.run_sync(lambda sync: run(alembic_config(sync), revision))
            raise _Killed
    except _Killed:
        pass


async def tables(engine: AsyncEngine) -> list[str]:
    async with engine.connect() as conn:
        return await conn.run_sync(lambda sync: inspect(sync).get_table_names())


async def test_an_interrupted_run_on_sqlite_does_not_block_the_next_migrate(
    tmp_path: Path, clock: FixedClock
) -> None:
    """SQLite creates the batch copy's table outside the migration's transaction, so a run that dies
    before its commit rolls back to the old revision and leaves ``_alembic_tmp_lpb_actors`` behind. Every
    later migrate then failed: "table _alembic_tmp_lpb_actors already exists". Up and down."""
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'engine.db'}")
    try:
        await upgrade(engine, "0012")
        store, saved = SqlStore(engine), actor(clock)
        await save_at_0012(engine, saved)

        await interrupted(engine, "up", "head")
        assert await current_revision(engine) == "0012" and COPY in await tables(engine)
        await upgrade(engine)
        assert await current_revision(engine) == head_revision() and COPY not in await tables(engine)
        assert await secret_ref_length(engine) is None and await store.get_actor("default", "alice") == saved

        await downgrade(engine, "0013")  # the revision whose downgrade makes the copy
        await interrupted(engine, "down", "0012")
        assert await current_revision(engine) == "0013" and COPY in await tables(engine)
        await downgrade(engine, "0012")
        assert await current_revision(engine) == "0012" and COPY not in await tables(engine)
        assert await secret_ref_length(engine) == 200 and await alice_at_0012(engine) == saved
    finally:
        await engine.dispose()


async def test_a_copy_that_holds_the_only_actor_rows_is_never_dropped(
    tmp_path: Path, clock: FixedClock
) -> None:
    """The leftover is dropped only while ``lpb_actors`` itself is there. Without it the copy holds the
    only rows (each one a funded allocation's record), and the migration stops instead."""
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'engine.db'}")
    try:
        await upgrade(engine, "0012")
        await save_at_0012(engine, actor(clock))
        async with engine.begin() as conn:
            await conn.execute(text(f"ALTER TABLE lpb_actors RENAME TO {COPY}"))
        with pytest.raises(NoSuchTableError):
            await upgrade(engine)
        async with engine.connect() as conn:
            kept = await conn.execute(select(func.count()).select_from(text(COPY)))
            assert kept.scalar_one() == 1
        assert await current_revision(engine) == "0012"
    finally:
        await engine.dispose()
