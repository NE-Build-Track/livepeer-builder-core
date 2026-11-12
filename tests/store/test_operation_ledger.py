"""The operation ledger's store contract (D57, ISS-11), identical on SQLite and PostgreSQL.

The reference is stored with the effect under a unique constraint: one record per caller scope and
``operation_ref``. Every write after ``begin`` is fenced by the record's ``fencing_token``, so a slow run
that a resume took over can no longer complete, remember or abandon it.
"""

import asyncio
from collections.abc import AsyncIterator

import pytest

from livepeer_builder.contracts import AccessKey, OperationCommand, OperationRecord
from livepeer_builder.store import SqlStore
from livepeer_builder.testing import FixedClock


@pytest.fixture
async def sql(database_url: str) -> AsyncIterator[SqlStore]:
    store = await SqlStore.connect(database_url)
    yield store
    await store.close()


def started(
    clock: FixedClock, *, ref: str = "op-1", actor: str = "operator", fingerprint: str = "f1"
) -> OperationRecord:
    return OperationRecord(
        application_id="default",
        actor_id=actor,
        operation_ref=ref,
        command="fund",
        fingerprint=fingerprint,
        state="started",
        context={"previous_api_key_id": "key_1"},
        started_at=clock.now(),
    )


async def test_the_reference_is_unique_per_caller_scope(sql: SqlStore, clock: FixedClock) -> None:
    first = started(clock)
    assert await sql.begin_operation(first) is None  # recorded
    assert await sql.begin_operation(started(clock, fingerprint="f2")) == first  # nothing written: the holder
    assert await sql.get_operation("default", "operator", "op-1") == first
    assert await sql.begin_operation(started(clock, actor="someone-else")) is None  # another scope


async def test_complete_records_the_result_only_for_the_run_holding_the_token(
    sql: SqlStore, clock: FixedClock
) -> None:
    first = started(clock)
    await sql.begin_operation(first)
    clock.advance(1)
    stale = first.model_copy(update={"fencing_token": 0})  # a run a resume took over
    assert await sql.complete_operation(stale, {"id": "stale"}, clock.now()) is False
    assert await sql.complete_operation(first, {"id": "alloc_1"}, clock.now()) is True
    done = await sql.get_operation("default", "operator", "op-1")
    assert done == first.model_copy(
        update={"state": "done", "result": {"id": "alloc_1"}, "completed_at": clock.now()}
    )
    assert await sql.complete_operation(first, {"id": "again"}, clock.now()) is False  # done is final


async def test_a_stale_run_is_taken_over_by_one_resume_only(sql: SqlStore, clock: FixedClock) -> None:
    first = started(clock)
    await sql.begin_operation(first)
    clock.advance(200)
    resumed = await sql.resume_operation(first, clock.now())
    assert resumed == first.model_copy(update={"fencing_token": 2, "started_at": clock.now()})
    assert await sql.resume_operation(first, clock.now()) is None  # a second resumer read the same token
    assert await sql.get_operation("default", "operator", "op-1") == resumed
    assert await sql.complete_operation(first, {"id": "slow"}, clock.now()) is False  # the original is fenced


async def test_abandoning_frees_the_reference_only_for_the_run_holding_it(
    sql: SqlStore, clock: FixedClock
) -> None:
    first = started(clock)
    await sql.begin_operation(first)
    assert await sql.abandon_operation(first.model_copy(update={"fencing_token": 0})) is False
    assert await sql.get_operation("default", "operator", "op-1") == first
    assert await sql.abandon_operation(first) is True
    assert await sql.get_operation("default", "operator", "op-1") is None
    assert await sql.begin_operation(started(clock, fingerprint="corrected")) is None  # free again


async def test_a_run_remembers_its_context_while_it_holds_the_token(sql: SqlStore, clock: FixedClock) -> None:
    first = started(clock)
    await sql.begin_operation(first)
    noted = first.model_copy(update={"context": first.context | {"secret_ref": "db:abc"}})
    assert await sql.remember_operation(noted.model_copy(update={"fencing_token": 0})) is False
    assert await sql.get_operation("default", "operator", "op-1") == first
    assert await sql.remember_operation(noted) is True
    assert await sql.get_operation("default", "operator", "op-1") == noted


async def test_two_concurrent_begins_for_one_reference_record_one(sql: SqlStore, clock: FixedClock) -> None:
    answers = await asyncio.gather(*(sql.begin_operation(started(clock, fingerprint=f)) for f in ("a", "b")))
    assert sorted(answer is None for answer in answers) == [False, True]
    held = await sql.get_operation("default", "operator", "op-1")
    assert held is not None and [held] == [answer for answer in answers if answer is not None]


async def test_a_reference_freed_between_the_insert_and_the_read_is_taken_again(
    sql: SqlStore, clock: FixedClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The holder was abandoned (a definite refusal) after this insert failed and before it read the holder."""
    first = started(clock)
    await sql.begin_operation(first)
    get_operation = sql.get_operation

    async def abandoned_meanwhile(*scope: str) -> OperationRecord | None:
        monkeypatch.setattr(sql, "get_operation", get_operation)
        await sql.abandon_operation(first)
        return None

    monkeypatch.setattr(sql, "get_operation", abandoned_meanwhile)
    retry = started(clock, fingerprint="corrected")
    assert await sql.begin_operation(retry) is None
    assert await sql.get_operation("default", "operator", "op-1") == retry


def access_key(clock: FixedClock, key_id: str) -> AccessKey:
    return AccessKey(
        key_id=key_id, kind="actor", actor_id="alice", application_id="default", permissions=("jobs.run",),
        label="", created_at=clock.now(), revoked_at=None,
    )  # fmt: skip


def issued(clock: FixedClock, key_id: str, command: OperationCommand = "issue_key") -> OperationRecord:
    return started(clock, ref="key-1").model_copy(
        update={
            "command": command,
            "state": "done",
            "result": {"key_id": key_id},
            "completed_at": clock.now(),
        }
    )


async def test_an_access_key_and_its_record_commit_together(sql: SqlStore, clock: FixedClock) -> None:
    """B4: no unknown outcome. A key whose reference is held is not written, and neither is its record."""
    assert await sql.insert_access_key(access_key(clock, "key_a"), "hash-a", issued(clock, "key_a")) is True
    assert await sql.insert_access_key(access_key(clock, "key_b"), "hash-b", issued(clock, "key_b")) is False
    assert [key.key_id for key in await sql.list_access_keys()] == ["key_a"]
    held = await sql.get_operation("default", "operator", "key-1")
    assert held is not None and held.result == {"key_id": "key_a"}


async def test_a_rotation_under_a_held_reference_writes_nothing(sql: SqlStore, clock: FixedClock) -> None:
    await sql.insert_access_key(access_key(clock, "key_old"), "hash-old")
    await sql.begin_operation(started(clock, ref="key-1"))  # the reference is taken
    record = issued(clock, "key_new", "rotate_access_key")
    assert (
        await sql.rotate_access_key("key_old", access_key(clock, "key_new"), "hash-new", clock.now(), record)
        is False
    )
    old = await sql.get_access_key("key_old")
    assert old is not None and old.revoked_at is None  # the revoke rolled back with the rest
    assert await sql.get_access_key("key_new") is None


async def test_two_concurrent_issues_under_one_reference_store_one_key(
    sql: SqlStore, clock: FixedClock
) -> None:
    written = await asyncio.gather(
        *(
            sql.insert_access_key(access_key(clock, k), "hash-" + k, issued(clock, k))
            for k in ("key_a", "key_b")
        )
    )
    assert sorted(written) == [False, True]
    assert len(await sql.list_access_keys()) == 1
