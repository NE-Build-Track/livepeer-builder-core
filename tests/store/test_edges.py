"""Store edges: backup guards, usage retention, filtered listings, duplicate ids, the PostgreSQL engine."""

import sqlite3
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError

from livepeer_builder.contracts import AccessKey, ProvisionedActor, SecretRef, SyncCheckpoint, UsageRow
from livepeer_builder.errors import InvalidRequest, UnsupportedForBackend
from livepeer_builder.store import SqlStore, backup, create_engine, purge, verify
from livepeer_builder.testing import FixedClock
from tests.store.conftest import make_job


async def test_backup_guards(tmp_path: Path) -> None:
    with pytest.raises(UnsupportedForBackend, match="in-memory"):
        await backup("sqlite+aiosqlite:///:memory:", tmp_path / "x.db")
    with pytest.raises(InvalidRequest, match="does not exist"):
        await backup(f"sqlite+aiosqlite:///{tmp_path / 'absent.db'}", tmp_path / "x.db")

    garbage = tmp_path / "garbage.db"
    garbage.write_bytes(b"this is not a sqlite file at all" * 40)
    with pytest.raises(InvalidRequest, match="not a readable SQLite database"):
        verify(garbage)

    empty_version = tmp_path / "empty.db"
    with sqlite3.connect(empty_version) as conn:
        conn.execute("CREATE TABLE lpb_alembic_version (version_num TEXT)")
    with pytest.raises(InvalidRequest, match="no schema revision"):
        verify(empty_version)


def test_postgres_urls_get_a_pooled_engine() -> None:
    engine = create_engine("postgresql+asyncpg://u:p@db.example/x")  # no connection is opened
    assert engine.dialect.name == "postgresql"


async def test_usage_retention(database_url: str, clock: FixedClock) -> None:
    store = await SqlStore.connect(database_url)

    def row(row_id: str) -> UsageRow:
        return UsageRow(
            id=row_id, event_id=None, status="applied", manifest_id=None, allocation_id="a", payment_session_id=None,
            request_id=None, pipeline=None, computed_fee_eth=Decimal("1"), computed_fee_usd=None, currency=None,
            created_at=clock.now(),
        )  # fmt: skip

    await store.apply_usage([row("old")], SyncCheckpoint(resume_cursor=None))
    clock.advance(timedelta(days=10).total_seconds())
    report = await purge(store.engine, clock.now(), jobs_days=None, usage_days=7)
    remaining = await store.unattributed_usage("a")
    await store.close()
    assert report.usage_rows_deleted == 1 and remaining == []


async def test_filtered_listings_and_duplicate_ids(database_url: str, clock: FixedClock) -> None:
    store = await SqlStore.connect(database_url)
    for app, actor in (("app-1", "alice"), ("app-2", "bob")):
        await store.save_actor(
            ProvisionedActor(
                application_id=app, actor_id=actor, grant_id="g", allocation_id=f"alloc-{actor}", api_key_id="k",
                secret_ref=SecretRef(ref="db:x"), currency="USD", status="active", created_at=clock.now(),
                updated_at=clock.now(),
            )
        )  # fmt: skip
        await store.insert_access_key(
            AccessKey(
                key_id=f"k-{actor}", kind="actor", actor_id=actor, application_id=app, permissions=(), label="",
                created_at=clock.now(), revoked_at=None,
            ),
            f"hash-{actor}",
        )  # fmt: skip
    assert [a.actor_id for a in await store.list_actors("app-2")] == ["bob"]
    assert len(await store.list_actors()) == 2
    assert [k.actor_id for k in await store.list_access_keys("app-1")] == ["alice"]

    old = make_job(clock)
    await store.create_job(old, None)
    clock.advance(60)
    new = make_job(clock)
    await store.create_job(new, None)
    assert [j.id for j in await store.list_jobs("default", "alice", before=clock.now())] == [old.id]
    assert (
        len(await store.jobs_between("default", None, old.created_at, clock.now() + timedelta(seconds=1)))
        == 2
    )
    with pytest.raises(IntegrityError):
        await store.create_job(old, None)  # a duplicate id with no operation_ref is a real error
    await store.close()
