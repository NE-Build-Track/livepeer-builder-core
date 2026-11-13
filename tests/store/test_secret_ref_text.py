"""#115: ``lpb_actors.secret_ref`` is unbounded text (migration 0013), on SQLite and PostgreSQL.

The column was ``varchar(200)``. SQLite ignores the length, but PostgreSQL enforces it, and the actor row
is written after the provider key is funded: a supplied secret store whose reference is longer (a vault
path) left a funded allocation with no record. A reference is opaque text of any length.
"""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import Text, insert, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from livepeer_builder.contracts import ProvisionedActor, SecretRef
from livepeer_builder.store import SqlStore, create_engine, current_revision, head_revision, upgrade
from livepeer_builder.store import schema as t
from livepeer_builder.store.migrate import downgrade
from livepeer_builder.testing import FixedClock

LONG = "vault:kv/data/livepeer/" + "p" * 5000


@pytest.fixture
async def sql(database_url: str) -> AsyncIterator[SqlStore]:
    store = await SqlStore.connect(database_url)
    yield store
    await store.close()


def actor(
    clock: FixedClock, actor_id: str = "alice", allocation_id: str = "a1", ref: str = "db:1"
) -> ProvisionedActor:
    return ProvisionedActor(
        application_id="default", actor_id=actor_id, grant_id="g", allocation_id=allocation_id,
        api_key_id="k1", secret_ref=SecretRef(ref=ref), currency="USD", status="active",
        created_at=clock.now(), updated_at=clock.now(), label="Alice",
    )  # fmt: skip


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
        "default", "alice", "a1", "k2", SecretRef(ref=LONG + "/v2"), clock.now()
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
        values = before.model_dump(exclude={"secret_ref"}) | {"secret_ref": before.secret_ref.ref}
        async with engine.begin() as conn:  # a row written by revision 0012's schema
            await conn.execute(insert(t.actors).values(values))

        await upgrade(engine)
        assert await current_revision(engine) == head_revision() == "0013"
        assert await secret_ref_length(engine) is None
        store = SqlStore(engine)
        assert await store.get_actor("default", "alice") == before
        # the rest of the table is as it was: an allocation still belongs to one actor, a label defaults
        with pytest.raises(IntegrityError):
            await store.save_actor(actor(clock, actor_id="bob", allocation_id="a1"))
        unlabelled = {key: value for key, value in values.items() if key != "label"}
        async with engine.begin() as conn:
            await conn.execute(
                insert(t.actors).values(unlabelled | {"actor_id": "carol", "allocation_id": "a3"})
            )
            label = await conn.execute(select(t.actors.c.label).where(t.actors.c.actor_id == "carol"))
            assert label.scalar_one() == ""

        await downgrade(engine, "0012")  # no reference here is over 200 characters, so it goes back
        assert await secret_ref_length(engine) == 200
        assert await store.get_actor("default", "alice") == before
        await upgrade(engine)
        assert await store.get_actor("default", "alice") == before
    finally:
        await engine.dispose()
