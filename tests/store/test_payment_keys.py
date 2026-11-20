"""#166: where a clearinghouse keeps the payment keys it issues (migration 0015), on SQLite and PostgreSQL.

A payment key is what an enterprise's engine presents when it asks the clearinghouse to sign a payment
for one actor. The clearinghouse keeps the key's SHA-256 hash and never the key, as it does for an access
key, in ``lpb_payment_keys``: one row for each actor, bound to the allocation the key was issued for. A
hash finds its actor only while that actor still holds that allocation and it is not revoked.
"""

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

import pytest
from sqlalchemy import func, insert, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from livepeer_builder.contracts import AccessKey, ActorAllocation, OperationRecord, SecretRef
from livepeer_builder.protocols import PaymentKeyRepo
from livepeer_builder.store import SqlStore, create_engine, current_revision, head_revision, upgrade
from livepeer_builder.store import schema as t
from livepeer_builder.store.migrate import downgrade
from livepeer_builder.testing import FixedClock
from livepeer_builder.utils import token_hash

TABLE = "lpb_payment_keys"


@pytest.fixture
async def sql(database_url: str) -> AsyncIterator[SqlStore]:
    store = await SqlStore.connect(database_url)
    yield store
    await store.close()


def actor(
    clock: FixedClock,
    actor_id: str = "alice",
    allocation_id: str = "alloc_1",
    *,
    tenant_id: str = "default",
    status: str = "active",
) -> ActorAllocation:
    return ActorAllocation(
        tenant_id=tenant_id, actor_id=actor_id, grant_id=tenant_id, allocation_id=allocation_id,
        api_key_id=f"key-of-{allocation_id}", secret_ref=SecretRef(ref=f"db:{allocation_id}"),
        currency="USD", status=status, created_at=clock.now(), updated_at=clock.now(),  # type: ignore[arg-type]
    )  # fmt: skip


async def issued(
    sql: SqlStore, holder: ActorAllocation, token: str, now: datetime, *, only_if_absent: bool = False
) -> bool:
    return await sql.put_payment_key(
        holder.tenant_id,
        holder.actor_id,
        holder.allocation_id,
        token_hash(token),
        now,
        only_if_absent=only_if_absent,
    )


async def rows(engine: AsyncEngine) -> list[dict[str, Any]]:
    async with engine.connect() as conn:
        found = await conn.execute(select(t.payment_keys).order_by(t.payment_keys.c.actor_id))
        return [dict(row) for row in found.mappings()]


async def tables(engine: AsyncEngine) -> list[str]:
    async with engine.connect() as conn:
        return await conn.run_sync(lambda sync: inspect(sync).get_table_names())


# -- the migration --------------------------------------------------------------------------------------


async def test_the_migration_adds_the_table_and_every_existing_row_survives_it_and_a_downgrade(
    database_url: str, clock: FixedClock
) -> None:
    """Records written at revision 0014, by a release before payment keys: an actor with an allocation,
    an access key and a finished allotment. The upgrade adds a table and touches no other."""
    engine = create_engine(database_url)
    try:
        await upgrade(engine, "0014")
        assert TABLE not in await tables(engine)
        store = SqlStore(engine)
        alice = actor(clock)
        await store.save_actor(alice)
        key = AccessKey(
            key_id="key_1", kind="tenant_admin", actor_id="tenant-admin", tenant_id="default",
            permissions=(), label="", created_at=clock.now(), revoked_at=None,
        )  # fmt: skip
        await store.insert_access_key(key, token_hash("lpk_before"))
        allotment = OperationRecord(
            tenant_id="default", actor_id="operator", operation_ref="allot-alice", command="allot",
            fingerprint="f" * 64, state="started", started_at=clock.now(),
        )  # fmt: skip
        assert await store.begin_operation(allotment) is None
        assert await store.complete_operation(allotment, alice.model_dump(mode="json"), clock.now())
        recorded = await store.get_operation("default", "operator", "allot-alice")

        await upgrade(engine)
        assert await current_revision(engine) == head_revision() == "0016"
        assert TABLE in await tables(engine)
        assert await rows(engine) == []  # an allocation allotted before has no payment key
        assert await store.get_actor("default", "alice") == alice
        assert await store.get_access_key("key_1") == key
        assert await store.access_key_by_hash(token_hash("lpk_before")) == key
        assert await store.get_operation("default", "operator", "allot-alice") == recorded

        assert await issued(store, alice, "lpp_first", clock.now())
        await downgrade(engine, "0014")  # the table goes, and the payment keys with it; nothing else does
        assert TABLE not in await tables(engine)
        assert await current_revision(engine) == "0014"
        assert await store.get_actor("default", "alice") == alice
        assert await store.get_access_key("key_1") == key
        assert await store.get_operation("default", "operator", "allot-alice") == recorded

        await upgrade(engine)
        assert await rows(engine) == []  # a key issued before the downgrade is gone: rotate for a new one
        assert await store.get_actor("default", "alice") == alice
    finally:
        await engine.dispose()


async def test_the_packaged_store_keeps_payment_keys(sql: SqlStore) -> None:
    assert isinstance(sql, PaymentKeyRepo)


# -- what is kept ----------------------------------------------------------------------------------------


async def test_only_the_hash_of_a_payment_key_is_kept(sql: SqlStore, clock: FixedClock) -> None:
    alice = actor(clock)
    await sql.save_actor(alice)
    assert await issued(sql, alice, "lpp_secret-token", clock.now())
    [row] = await rows(sql.engine)
    assert row == {
        "tenant_id": "default",
        "actor_id": "alice",
        "allocation_id": "alloc_1",
        "token_hash": token_hash("lpp_secret-token"),
        "created_at": clock.now(),
    }
    assert "lpp_secret-token" not in str(row)


async def test_a_hash_finds_the_actor_whose_payment_key_it_is(sql: SqlStore, clock: FixedClock) -> None:
    alice, bob = actor(clock), actor(clock, "bob", "alloc_2", tenant_id="other")
    await sql.save_actor(alice)
    await sql.save_actor(bob)
    await issued(sql, alice, "lpp_alice", clock.now())
    await issued(sql, bob, "lpp_bob", clock.now())
    assert await sql.actor_by_payment_key(token_hash("lpp_alice")) == alice
    assert await sql.actor_by_payment_key(token_hash("lpp_bob")) == bob
    assert await sql.actor_by_payment_key(token_hash("lpp_nobody")) is None
    assert await sql.actor_by_payment_key("lpp_alice") is None  # the token itself is no hash


async def test_a_new_payment_key_replaces_the_actors_old_one_at_once(
    sql: SqlStore, clock: FixedClock
) -> None:
    alice = actor(clock)
    await sql.save_actor(alice)
    await issued(sql, alice, "lpp_old", clock.now())
    clock.advance(60)
    assert await issued(sql, alice, "lpp_new", clock.now())
    assert await sql.actor_by_payment_key(token_hash("lpp_old")) is None
    assert await sql.actor_by_payment_key(token_hash("lpp_new")) == alice
    [row] = await rows(sql.engine)  # one row for an actor, whatever was issued before
    assert (row["token_hash"], row["created_at"]) == (token_hash("lpp_new"), clock.now())


async def test_a_first_payment_key_is_issued_only_where_the_allocation_has_none(
    sql: SqlStore, clock: FixedClock
) -> None:
    """What an allotment asks: issue a key unless this allocation already has one. A key of an earlier
    allocation of the actor is not this allocation's: it is replaced."""
    alice = actor(clock)
    await sql.save_actor(alice)
    assert await issued(sql, alice, "lpp_first", clock.now(), only_if_absent=True)
    assert not await issued(sql, alice, "lpp_second", clock.now(), only_if_absent=True)
    assert await sql.actor_by_payment_key(token_hash("lpp_first")) == alice
    assert await sql.actor_by_payment_key(token_hash("lpp_second")) is None

    again = actor(clock, allocation_id="alloc_9")  # revoked and allotted again: another allocation
    await sql.save_actor(again)
    assert await issued(sql, again, "lpp_third", clock.now(), only_if_absent=True)
    assert await sql.actor_by_payment_key(token_hash("lpp_first")) is None
    assert await sql.actor_by_payment_key(token_hash("lpp_third")) == again


async def test_two_first_payment_keys_for_one_allocation_at_the_same_time_leave_one(
    database_url: str, clock: FixedClock
) -> None:
    first, second = await SqlStore.connect(database_url), await SqlStore.connect(database_url)
    try:
        alice = actor(clock)
        await first.save_actor(alice)
        won = await asyncio.gather(
            issued(first, alice, "lpp_a", clock.now(), only_if_absent=True),
            issued(second, alice, "lpp_b", clock.now(), only_if_absent=True),
        )
        assert sorted(won) == [False, True]
        winner = "lpp_a" if won[0] else "lpp_b"
        loser = "lpp_b" if won[0] else "lpp_a"
        assert await first.actor_by_payment_key(token_hash(winner)) == alice
        assert await first.actor_by_payment_key(token_hash(loser)) is None
    finally:
        await first.close()
        await second.close()


async def test_two_replacements_for_an_actor_with_no_key_at_the_same_time_leave_one(
    database_url: str, clock: FixedClock
) -> None:
    """Two rotations of an allocation that has no key yet (one allotted before #166), on two connections.
    Both keep a key by their own account, and the actor holds exactly one of them: one row for an actor."""
    first, second = await SqlStore.connect(database_url), await SqlStore.connect(database_url)
    try:
        alice = actor(clock)
        await first.save_actor(alice)
        kept = await asyncio.gather(
            issued(first, alice, "lpp_a", clock.now()), issued(second, alice, "lpp_b", clock.now())
        )
        assert kept == [True, True]
        [row] = await rows(first.engine)
        assert row["token_hash"] in (token_hash("lpp_a"), token_hash("lpp_b"))
        live = "lpp_a" if row["token_hash"] == token_hash("lpp_a") else "lpp_b"
        assert await first.actor_by_payment_key(token_hash(live)) == alice
    finally:
        await first.close()
        await second.close()


# -- when a hash finds nobody ------------------------------------------------------------------------------


async def test_a_payment_key_of_a_revoked_allocation_finds_nobody(sql: SqlStore, clock: FixedClock) -> None:
    alice = actor(clock)
    await sql.save_actor(alice)
    await issued(sql, alice, "lpp_alice", clock.now())
    await sql.set_actor_status("default", "alice", "alloc_1", "revoked", clock.now())
    assert await sql.actor_by_payment_key(token_hash("lpp_alice")) is None


@pytest.mark.parametrize("status", ["paused", "exhausted"])
async def test_a_paused_or_exhausted_allocation_keeps_its_payment_key(
    sql: SqlStore, clock: FixedClock, status: str
) -> None:
    """The payment provider refuses the payment (403, 402), not the clearinghouse the key."""
    alice = actor(clock, status=status)
    await sql.save_actor(alice)
    await issued(sql, alice, "lpp_alice", clock.now())
    assert await sql.actor_by_payment_key(token_hash("lpp_alice")) == alice


async def test_a_payment_key_never_finds_an_allocation_it_was_not_issued_for(
    sql: SqlStore, clock: FixedClock
) -> None:
    """The actor was revoked and allotted again, and the old key's row was never removed: it is bound to
    the allocation it was issued for, so it finds nobody."""
    alice = actor(clock)
    await sql.save_actor(alice)
    await issued(sql, alice, "lpp_old", clock.now())
    await sql.save_actor(actor(clock, allocation_id="alloc_2"))  # the same actor, another allocation
    assert await sql.actor_by_payment_key(token_hash("lpp_old")) is None


async def test_a_payment_key_is_removed_only_with_the_allocation_it_was_issued_for(
    sql: SqlStore, clock: FixedClock
) -> None:
    alice = actor(clock)
    await sql.save_actor(alice)
    await issued(sql, alice, "lpp_alice", clock.now())
    await sql.delete_payment_key("default", "alice", "alloc_0")  # a late revoke of an earlier allocation
    assert await sql.actor_by_payment_key(token_hash("lpp_alice")) == alice
    await sql.delete_payment_key("default", "alice", "alloc_1")
    assert await rows(sql.engine) == []
    await sql.delete_payment_key("default", "alice", "alloc_1")  # again: nothing to remove, no error


async def test_one_hash_is_one_actors(sql: SqlStore, clock: FixedClock) -> None:
    """A token is 32 random bytes, so two actors never share one; the table refuses it all the same."""
    row = {"allocation_id": "a", "token_hash": "h" * 64, "created_at": clock.now(), "tenant_id": "default"}
    async with sql.engine.begin() as conn:
        await conn.execute(insert(t.payment_keys).values(row | {"actor_id": "alice"}))
    with pytest.raises(IntegrityError):
        async with sql.engine.begin() as conn:
            await conn.execute(insert(t.payment_keys).values(row | {"actor_id": "bob"}))
    async with sql.engine.connect() as conn:
        assert (await conn.execute(select(func.count()).select_from(t.payment_keys))).scalar_one() == 1
