"""#163: migration 0014 renames what records call things, and keeps every row. SQLite and PostgreSQL.

Rows are written at revision 0013 in plain SQL, under the names that revision has, then read back through
the store at the head. Nothing here goes through the store before the upgrade: the store only knows the
new names.
"""

import json
from collections.abc import AsyncIterator
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from alembic import command
from sqlalchemy import func, insert, inspect, select
from sqlalchemy.ext.asyncio import AsyncEngine

from livepeer_builder import EngineSupply
from livepeer_builder.contracts import (
    OPERATION_REPLAYED,
    AccessKey,
    ActorAllocation,
    ActorContext,
    AllotRequest,
    Money,
    SecretRef,
    SeenActor,
)
from livepeer_builder.errors import OperationConflict, OperationExists
from livepeer_builder.store import SqlStore, create_engine, current_revision, head_revision, upgrade
from livepeer_builder.store import schema as t
from livepeer_builder.store.migrate import alembic_config, downgrade
from livepeer_builder.testing import FakeAllocationManager, FixedClock, make_engine
from tests.store.conftest import as_before_0014, make_job

TABLES = (t.access_keys, t.jobs, t.actors, t.actor_sightings, t.operations)
ADMIN = ActorContext(actor_id="operator", credential_kind="clearinghouse_admin")
# What ``lpb_operations.fingerprint`` held for this provisioning before the rename
# (tests/unit/test_hashed_forms.py): a digest, which no migration can recompute.
PROVISION = AllotRequest(
    tenant_id="default",
    actor_id="alice",
    amount=Money(amount=Decimal("5"), currency="USD"),
    label="Alice",
    grant_id="grant_1",
    operation_ref="provision",
)
PROVISION_FINGERPRINT = "8eeeba74ca6295846627a15f9de0b1994cff06254598fdd95e62b0ff000b190d"


def rows_at_0013(now: datetime, job_id: str) -> dict[str, list[dict[str, Any]]]:
    """One deployment's records as revision 0013 stored them: two applications, each kind of key, an actor
    with an allocation, a person who signed in, a job, and admin commands done and under way."""
    alice = {
        "application_id": "default", "actor_id": "alice", "grant_id": "grant_1", "allocation_id": "alloc_1",
        "api_key_id": "key_1", "currency": "USD", "status": "active", "label": "Alice",
    }  # fmt: skip
    stamped = {"created_at": now, "updated_at": now}
    key = {"permissions": [], "label": "", "created_at": now, "revoked_at": None}
    operation = {"fencing_token": 1, "context": {}, "started_at": now, "completed_at": now}
    return {
        "lpb_access_keys": [
            key | {"key_id": "key_admin", "token_hash": "h1", "kind": "admin", "actor_id": "operator",
                   "application_id": "default", "label": "bootstrap"},
            key | {"key_id": "key_admin_revoked", "token_hash": "h0", "kind": "admin", "actor_id": "operator",
                   "application_id": "default", "revoked_at": now},
            key | {"key_id": "key_auditor", "token_hash": "h2", "kind": "auditor", "actor_id": "dashboard",
                   "application_id": "acme"},
            key | {"key_id": "key_actor", "token_hash": "h3", "kind": "actor", "actor_id": "bob",
                   "application_id": "acme", "permissions": ["jobs.run"]},
        ],
        "lpb_jobs": [
            {"id": job_id, "application_id": "default", "actor_id": "alice", "operation_ref": "order-1",
             "request_fingerprint": "fp", "mode": "immediate", "app": "a/b", "state": "succeeded",
             "created_at": now, "completed_at": now},
        ],
        "lpb_actors": [
            alice | stamped | {"secret_ref": "db:1"},
            alice | stamped | {"application_id": "acme", "actor_id": "bob", "allocation_id": "alloc_2",
                               "secret_ref": "db:2", "label": ""},
        ],
        "lpb_actor_sightings": [
            {"application_id": "acme", "actor_id": "bob", "name": "Bob", "first_seen_at": now,
             "last_seen_at": now},
        ],
        "lpb_operations": [
            operation | {
                "application_id": "default", "actor_id": "operator", "operation_ref": "provision",
                "command": "provision", "fingerprint": PROVISION_FINGERPRINT, "state": "done",
                "context": {"new_secret_ref": "db:1", "new_secret_key_id": "key_1"},
                "result": alice | {"secret_ref": {"ref": "db:1"}, "created_at": now.isoformat(),
                                   "updated_at": now.isoformat()},
            },
            operation | {
                "application_id": "default", "actor_id": "operator", "operation_ref": "issue",
                "command": "issue_key", "fingerprint": "f-issue", "state": "done",
                "result": {"key_id": "key_auditor", "kind": "auditor", "actor_id": "dashboard",
                           "application_id": "acme", "permissions": [], "label": "",
                           "created_at": now.isoformat(), "revoked_at": None},
            },
            operation | {
                "application_id": "default", "actor_id": "operator", "operation_ref": "rotate",
                "command": "rotate_access_key", "fingerprint": "f-rotate", "state": "done",
                "result": {"key_id": "key_admin", "kind": "admin", "actor_id": "operator",
                           "application_id": "default", "permissions": [], "label": "bootstrap",
                           "created_at": now.isoformat(), "revoked_at": None},
            },
            operation | {
                "application_id": "acme", "actor_id": "operator", "operation_ref": "fund-1",
                "command": "fund", "fingerprint": "f-fund", "state": "started", "fencing_token": 2,
                "context": {"previous_api_key_id": "key_0"}, "result": None, "completed_at": None,
            },
        ],
    }  # fmt: skip


async def at_0013(database_url: str, clock: FixedClock) -> tuple[AsyncEngine, str]:
    """A database at revision 0013 holding ``rows_at_0013``; returns its engine and the job's id."""
    engine = create_engine(database_url)
    await upgrade(engine, "0013")
    job_id = str(make_job(clock).id)
    old = {table.name: as_before_0014(table) for table in TABLES}
    async with engine.begin() as conn:
        for name, rows in rows_at_0013(clock.now(), job_id).items():
            await conn.execute(insert(old[name]), rows)
    return engine, job_id


async def stored(engine: AsyncEngine, old_names: bool) -> dict[str, list[dict[str, Any]]]:
    """Every row of the five tables, as the database holds it now, in one order."""
    tables = [as_before_0014(table) if old_names else table for table in TABLES]
    async with engine.connect() as conn:
        found = {
            table.name: [dict(row) for row in (await conn.execute(select(table))).mappings()]
            for table in tables
        }
    return {name: in_order(rows) for name, rows in found.items()}


def in_order(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda row: json.dumps(row, default=str))


def renamed(row: dict[str, Any]) -> dict[str, Any]:
    """A row of revision 0013 as the head names it."""
    named = {("tenant_id" if name == "application_id" else name): value for name, value in row.items()}
    return named | ({"kind": "clearinghouse_admin"} if named.get("kind") == "admin" else {})


async def kind_length(engine: AsyncEngine) -> int | None:
    """The declared length of ``lpb_access_keys.kind`` in the database itself."""
    async with engine.connect() as conn:
        columns = await conn.run_sync(lambda sync: inspect(sync).get_columns("lpb_access_keys"))
    [column] = [column for column in columns if column["name"] == "kind"]
    return getattr(column["type"], "length", None)


@pytest.fixture
async def migrated(database_url: str, clock: FixedClock) -> AsyncIterator[tuple[AsyncEngine, str]]:
    engine, job_id = await at_0013(database_url, clock)
    await upgrade(engine)
    yield engine, job_id
    await engine.dispose()


async def test_every_row_written_before_the_rename_is_read_under_the_new_names(
    migrated: tuple[AsyncEngine, str], clock: FixedClock
) -> None:
    engine, job_id = migrated
    assert await current_revision(engine) == head_revision() == "0014"
    store, now = SqlStore(engine), clock.now()

    alice = ActorAllocation(
        tenant_id="default", actor_id="alice", grant_id="grant_1", allocation_id="alloc_1",
        api_key_id="key_1", secret_ref=SecretRef(ref="db:1"), currency="USD", status="active",
        created_at=now, updated_at=now, label="Alice",
    )  # fmt: skip
    assert await store.get_actor("default", "alice") == alice
    assert [actor.actor_id for actor in await store.list_actors("acme")] == ["bob"]
    assert await store.get_sighting("acme", "bob") == SeenActor(
        tenant_id="acme", actor_id="bob", name="Bob", first_seen_at=now, last_seen_at=now
    )

    auditor = AccessKey(
        key_id="key_auditor", kind="auditor", actor_id="dashboard", tenant_id="acme", permissions=(),
        label="", created_at=now, revoked_at=None,
    )  # fmt: skip
    assert await store.get_access_key("key_auditor") == auditor
    assert await store.access_key_by_hash("h3") == auditor.model_copy(
        update={"key_id": "key_actor", "kind": "actor", "actor_id": "bob", "permissions": ("jobs.run",)}
    )
    admin = auditor.model_copy(
        update={"key_id": "key_admin", "kind": "clearinghouse_admin", "actor_id": "operator"}
        | {"tenant_id": "default", "label": "bootstrap"}
    )
    assert await store.list_access_keys("default") == [
        admin,
        admin.model_copy(update={"key_id": "key_admin_revoked", "label": "", "revoked_at": now}),
    ]
    assert await store.count_active_admin_keys() == 1  # so `lpb bootstrap-admin` still refuses a second
    assert await kind_length(engine) == 32  # clearinghouse_admin is 19 characters

    job = await store.get_job(UUID(job_id))
    assert job is not None and (job.tenant_id, job.actor_id, job.operation_ref) == (
        "default",
        "alice",
        "order-1",
    )
    held = await store.job_by_operation_ref("default", "alice", "order-1")
    assert held is not None and held[0].id == job.id and held[1] == "fp"

    done = await store.get_operation("default", "operator", "provision")
    assert done is not None and done.fingerprint == PROVISION_FINGERPRINT and done.state == "done"
    assert done.context == {"new_secret_ref": "db:1", "new_secret_key_id": "key_1"}
    assert (
        done.command == "allot" and ActorAllocation.model_validate(done.result) == alice
    )  # the answer a repeat gets back
    issued = await store.get_operation("default", "operator", "issue")
    assert issued is not None and AccessKey.model_validate(issued.result) == auditor
    rotated = await store.get_operation("default", "operator", "rotate")
    assert rotated is not None and AccessKey.model_validate(rotated.result) == admin
    started = await store.get_operation("acme", "operator", "fund-1")
    assert started is not None and started.command == "top_up"
    assert (started.state, started.fencing_token, started.result) == (
        "started",
        2,
        None,
    )
    assert started.context == {"previous_api_key_id": "key_0"} and started.completed_at is None


async def test_the_keys_and_the_indexes_still_hold_after_the_rename(
    migrated: tuple[AsyncEngine, str], clock: FixedClock
) -> None:
    """The columns were renamed in place: one job for each reference of a tenant's actor, one actor for
    each tenant and id, and a job of another tenant under the same reference is its own."""
    engine, _ = migrated
    store = SqlStore(engine)
    with pytest.raises(OperationExists):
        await store.create_job(make_job(clock, ref="order-1"), "fp")
    elsewhere = make_job(clock, ref="order-1").model_copy(update={"tenant_id": "acme"})
    await store.create_job(elsewhere, "fp")
    found = await store.job_by_operation_ref("acme", "alice", "order-1")
    assert found is not None and found[0].id == elsewhere.id
    async with engine.connect() as conn:
        indexed = await conn.run_sync(lambda sync: inspect(sync).get_indexes("lpb_jobs"))
        keys = {
            table.name: await conn.run_sync(
                lambda sync, name=table.name: inspect(sync).get_pk_constraint(name)["constrained_columns"]
            )
            for table in (t.actors, t.actor_sightings, t.operations)
        }
    columns = {index["name"]: index["column_names"] for index in indexed}
    assert columns["lpb_jobs_operation_ref"] == ["tenant_id", "actor_id", "operation_ref"]
    assert columns["lpb_jobs_owner_created"] == ["tenant_id", "actor_id", "created_at"]
    assert keys == {
        "lpb_actors": ["tenant_id", "actor_id"],
        "lpb_actor_sightings": ["tenant_id", "actor_id"],
        "lpb_operations": ["tenant_id", "actor_id", "operation_ref"],
    }


async def test_a_downgrade_puts_every_row_back_as_it_was(database_url: str, clock: FixedClock) -> None:
    engine, job_id = await at_0013(database_url, clock)
    try:
        before = await stored(engine, old_names=True)
        assert {name: len(rows) for name, rows in before.items()} == {
            "lpb_access_keys": 4,
            "lpb_jobs": 1,
            "lpb_actors": 2,
            "lpb_actor_sightings": 1,
            "lpb_operations": 4,
        }
        await upgrade(engine)
        head = await stored(engine, old_names=False)
        await downgrade(engine, "0013")
        assert await current_revision(engine) == "0013" and await kind_length(engine) == 16
        assert await stored(engine, old_names=True) == before
        await upgrade(engine)
        assert await stored(engine, old_names=False) == head
        assert job_id in {row["id"] for row in head["lpb_jobs"]}
    finally:
        await engine.dispose()


async def test_a_command_recorded_before_the_rename_is_replayed_and_not_run_again(
    migrated: tuple[AsyncEngine, str],
) -> None:
    """The repeat of a provisioning whose answer was lost, sent after the upgrade under the new names:
    the ledger finds the record written before it, by the same fingerprint, and answers from it. Nothing
    is sent to the provider. Another amount under that reference is still a conflict."""
    engine, _ = migrated
    manager = FakeAllocationManager()
    supply = EngineSupply(store=SqlStore(engine), allocation_manager=manager, grants_manager=manager)
    async with make_engine(supply=supply, auto_migrate=False) as built:
        answered = await built.payments.allot(ADMIN, PROVISION)
        assert OPERATION_REPLAYED.get() is True and manager.calls == []
        assert answered == await built.payments.get_actor(ADMIN, "default", "alice")
        more = PROVISION.model_copy(update={"amount": Money(amount=Decimal("50"), currency="USD")})
        with pytest.raises(OperationConflict):
            await built.payments.allot(ADMIN, more)
        assert manager.calls == []


# -- SQLite: a run of the migration that died before its commit ---------------------------------------------


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


async def table_names(engine: AsyncEngine) -> list[str]:
    async with engine.connect() as conn:
        return sorted(await conn.run_sync(lambda sync: inspect(sync).get_table_names()))


async def test_an_interrupted_run_on_sqlite_changes_nothing(tmp_path: Path, clock: FixedClock) -> None:
    """SQLite's driver commits an ``ALTER TABLE`` that runs before any row is written, so a run that died
    after its renames left columns of the next revision under the old one: the engine could read nothing,
    and ``lpb migrate`` saw the old revision. The run is one transaction now, up and down."""
    engine, _ = await at_0013(f"sqlite+aiosqlite:///{tmp_path / 'engine.db'}", clock)
    try:
        before = await stored(engine, old_names=True)
        tables = await table_names(engine)
        await interrupted(engine, "up", "head")
        assert await current_revision(engine) == "0013"
        assert await stored(engine, old_names=True) == before
        assert await table_names(engine) == tables  # no copy of lpb_access_keys is left behind
        await upgrade(engine)
        head = await stored(engine, old_names=False)
        for name in ("lpb_access_keys", "lpb_jobs", "lpb_actors", "lpb_actor_sightings"):
            assert head[name] == in_order([renamed(row) for row in before[name]])

        await interrupted(engine, "down", "0013")
        assert await current_revision(engine) == head_revision()
        assert await stored(engine, old_names=False) == head
        assert await table_names(engine) == tables
        async with engine.connect() as conn:
            assert (await conn.execute(select(func.count()).select_from(t.operations))).scalar_one() == 4
    finally:
        await engine.dispose()
