"""#115 C4: the admin commands' conditional actor writes (``ActorCommandRepo``), on SQLite and PostgreSQL.

Each write names its own columns and carries its condition in the ``WHERE``. On a file database every
call has its own connection, so the two writes at the end really do overlap.
"""

import asyncio
from collections.abc import AsyncIterator
from datetime import timedelta

import pytest

from livepeer_builder.contracts import ProvisionedActor, SecretRef
from livepeer_builder.store import SqlStore
from livepeer_builder.testing import FixedClock


@pytest.fixture
async def sql(database_url: str) -> AsyncIterator[SqlStore]:
    store = await SqlStore.connect(database_url)
    yield store
    await store.close()


@pytest.fixture
async def actor(sql: SqlStore, clock: FixedClock) -> ProvisionedActor:
    saved = ProvisionedActor(
        application_id="default", actor_id="alice", grant_id="g", allocation_id="a1", api_key_id="k1",
        secret_ref=SecretRef(ref="db:1"), currency="USD", status="active", created_at=clock.now(),
        updated_at=clock.now(), label="Alice",
    )  # fmt: skip
    await sql.save_actor(saved)
    return saved


async def test_a_status_write_changes_the_status_and_the_time_only(
    sql: SqlStore, actor: ProvisionedActor, clock: FixedClock
) -> None:
    later = clock.now() + timedelta(minutes=1)
    written = await sql.set_actor_status("default", "alice", "a1", "paused", later)
    assert written == actor.model_copy(update={"status": "paused", "updated_at": later})
    assert await sql.get_actor("default", "alice") == written


async def test_a_key_write_changes_the_key_its_reference_and_the_time_only(
    sql: SqlStore, actor: ProvisionedActor, clock: FixedClock
) -> None:
    later = clock.now() + timedelta(minutes=1)
    await sql.set_actor_status("default", "alice", "a1", "paused", clock.now())
    written = await sql.set_actor_key("default", "alice", "a1", "k2", SecretRef(ref="db:2"), later)
    changes = {"api_key_id": "k2", "secret_ref": SecretRef(ref="db:2"), "updated_at": later}
    assert written == actor.model_copy(update=changes | {"status": "paused"})
    assert await sql.get_actor("default", "alice") == written


async def test_a_status_write_limited_to_one_status_applies_only_from_it(
    sql: SqlStore, actor: ProvisionedActor, clock: FixedClock
) -> None:
    """A fund moves ``exhausted`` to ``active`` and nothing else."""
    assert await sql.set_actor_status("default", "alice", "a1", "paused", clock.now()) is not None
    assert (
        await sql.set_actor_status("default", "alice", "a1", "active", clock.now(), only_from="exhausted")
        is None
    )
    assert (await sql.get_actor("default", "alice")).status == "paused"  # type: ignore[union-attr]
    assert await sql.set_actor_status("default", "alice", "a1", "exhausted", clock.now()) is not None
    funded = await sql.set_actor_status(
        "default", "alice", "a1", "active", clock.now(), only_from="exhausted"
    )
    assert funded is not None and funded.status == "active"


async def test_no_write_applies_to_a_revoked_actor(
    sql: SqlStore, actor: ProvisionedActor, clock: FixedClock
) -> None:
    later = clock.now() + timedelta(minutes=1)
    revoked = await sql.set_actor_status("default", "alice", "a1", "revoked", clock.now())
    assert revoked is not None and revoked.status == "revoked"
    assert await sql.set_actor_status("default", "alice", "a1", "active", later) is None
    assert await sql.set_actor_status("default", "alice", "a1", "revoked", later) is None
    assert await sql.set_actor_status("default", "alice", "a1", "active", later, only_from="revoked") is None
    assert await sql.set_actor_key("default", "alice", "a1", "k2", SecretRef(ref="db:2"), later) is None
    assert await sql.get_actor("default", "alice") == revoked  # nothing was written


async def test_no_write_applies_to_another_allocation_or_an_unknown_actor(
    sql: SqlStore, actor: ProvisionedActor, clock: FixedClock
) -> None:
    """A command that acted on an allocation since revoked never writes onto the one provisioned after."""
    assert await sql.set_actor_status("default", "alice", "a0", "paused", clock.now()) is None
    assert await sql.set_actor_key("default", "alice", "a0", "k2", SecretRef(ref="db:2"), clock.now()) is None
    assert await sql.set_actor_status("default", "bob", "a1", "paused", clock.now()) is None
    assert await sql.set_actor_status("other", "alice", "a1", "paused", clock.now()) is None
    assert await sql.get_actor("default", "alice") == actor


async def test_a_status_write_and_a_key_write_at_the_same_time_both_last(
    sql: SqlStore, actor: ProvisionedActor, clock: FixedClock
) -> None:
    """Whole-row saves from two stale reads would keep one command's columns and lose the other's."""
    for round_ in range(1, 11):
        status = "paused" if round_ % 2 else "active"
        key, ref = f"k{round_ + 1}", SecretRef(ref=f"db:{round_ + 1}")
        wrote = await asyncio.gather(
            sql.set_actor_status("default", "alice", "a1", status, clock.now()),  # type: ignore[arg-type]
            sql.set_actor_key("default", "alice", "a1", key, ref, clock.now()),
        )
        assert None not in wrote
        stored = await sql.get_actor("default", "alice")
        assert stored is not None
        assert (stored.status, stored.api_key_id, stored.secret_ref) == (status, key, ref)
