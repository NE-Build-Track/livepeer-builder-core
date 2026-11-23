"""Migration 0015 records each actor's credential custody without losing a row, on SQLite and PostgreSQL."""

from sqlalchemy import column, insert, inspect, table, text
from sqlalchemy.ext.asyncio import AsyncEngine

from livepeer_builder.contracts import ActorAllocation, SecretRef
from livepeer_builder.store import SqlStore, create_engine, upgrade
from livepeer_builder.store import schema as t
from livepeer_builder.store.migrate import downgrade
from livepeer_builder.testing import FixedClock
from tests.store.conftest import ADDED_SINCE_0014


def actor(clock: FixedClock, actor_id: str, ref: str) -> ActorAllocation:
    return ActorAllocation(
        tenant_id="default", actor_id=actor_id, grant_id="default", allocation_id=f"alloc_{actor_id}",
        api_key_id=f"key_{actor_id}", secret_ref=SecretRef(ref=ref), currency="USD", status="active",
        created_at=clock.now(), updated_at=clock.now(), label="",
    )  # fmt: skip


ADDED = ADDED_SINCE_0014["lpb_actors"]
# lpb_actors as revision 0014 has it: the head's columns, without the custody column 0015 adds.
ACTORS_AT_0014 = table(
    t.actors.name, *(column(c.name, c.type) for c in t.actors.columns if c.name not in ADDED)
)


async def save_at_0014(engine: AsyncEngine, saved: ActorAllocation) -> None:
    values = saved.model_dump(exclude={"secret_ref", *ADDED}) | {"secret_ref": saved.secret_ref.ref}
    async with engine.begin() as conn:
        await conn.execute(insert(ACTORS_AT_0014).values(values))


async def table_names(engine: AsyncEngine) -> set[str]:
    async with engine.connect() as conn:
        return set(await conn.run_sync(lambda sync: inspect(sync).get_table_names()))


async def custody_column(engine: AsyncEngine) -> bool:
    async with engine.connect() as conn:
        columns = await conn.run_sync(lambda sync: inspect(sync).get_columns("lpb_actors"))
    return "credential_custody" in {found["name"] for found in columns}


async def test_every_existing_actor_is_the_stores_whatever_its_handle_reads(
    database_url: str, clock: FixedClock
) -> None:
    """Nothing is inferred from a reference's text: a supplied store may have issued a ``delivered:``
    handle, and that actor's key is still resolved through the store (#186 review)."""
    engine = create_engine(database_url)
    try:
        await upgrade(engine, "0014")
        plain, lookalike = actor(clock, "alice", "db:1"), actor(clock, "bob", "delivered:vault-path")
        for saved in (plain, lookalike):
            await save_at_0014(engine, saved)
        await upgrade(engine)
        store = SqlStore(engine)
        assert await store.get_actor("default", "alice") == plain
        assert await store.get_actor("default", "bob") == lookalike
        assert {found.credential_custody for found in await store.list_actors("default")} == {"secret_store"}

        await downgrade(engine, "0014")
        assert not await custody_column(engine)
        async with engine.connect() as conn:
            assert (await conn.execute(text("SELECT count(*) FROM lpb_actors"))).scalar_one() == 2
        await upgrade(engine)
        assert await custody_column(engine)
    finally:
        await engine.dispose()


async def test_no_revision_creates_the_withdrawn_payment_key_table(database_url: str) -> None:
    """Clearinghouse-issued payment keys were withdrawn before release (D87 point 1): no revision makes
    ``lpb_payment_keys``, up or down."""
    engine = create_engine(database_url)
    try:
        await upgrade(engine)
        assert "lpb_payment_keys" not in await table_names(engine)
        await downgrade(engine, "0014")
        assert "lpb_payment_keys" not in await table_names(engine)
    finally:
        await engine.dispose()
