"""The EngineStore contract, identical on SQLite and PostgreSQL (ISS-06)."""

from collections.abc import AsyncIterator
from datetime import UTC
from decimal import Decimal

import pytest

from livepeer_builder.contracts import (
    AccessKey,
    Failure,
    Fees,
    JobCost,
    MeasuredUsage,
    ProvisionedActor,
    ResultRef,
    SecretRef,
    SyncCheckpoint,
    UsageRow,
)
from livepeer_builder.errors import OperationExists
from livepeer_builder.store import SqlStore
from livepeer_builder.testing import FixedClock
from tests.store.conftest import make_attempt, make_job


@pytest.fixture
async def sql(database_url: str) -> AsyncIterator[SqlStore]:
    store = await SqlStore.connect(database_url)
    yield store
    await store.close()


async def test_jobs_attempts_and_auth_ids_round_trip(sql: SqlStore, clock: FixedClock) -> None:
    job = make_job(clock, state="running")
    await sql.create_job(job, "fp")
    await sql.start_attempt(
        make_attempt(job.id, clock).model_copy(update={"outcome": "in_flight", "ended_at": None})
    )
    await sql.finish_attempt(make_attempt(job.id, clock, auth_ids=("b-auth", "a-auth", "b-auth")))
    result = ResultRef(storage="inline", content_type="application/json", size_bytes=3, sha256="0" * 64)
    await sql.finish_job(job.id, "succeeded", None, result, clock.now())
    await sql.set_usage(
        job.id, MeasuredUsage(quantities={"tokens": 3}, source="app_reported", reported_at=clock.now())
    )
    loaded = await sql.get_job(job.id)
    assert loaded is not None
    assert loaded.state == "succeeded" and loaded.result == result
    assert loaded.attempts[0].auth_ids == ("a-auth", "b-auth")
    assert loaded.usage is not None and loaded.usage.quantities == {"tokens": 3}
    assert loaded.created_at.tzinfo is UTC or loaded.created_at.utcoffset() is not None
    assert await sql.jobs_for_auth_ids(["a-auth"]) == {job.id}


async def test_operation_ref_is_unique_per_actor(sql: SqlStore, clock: FixedClock) -> None:
    first = make_job(clock, ref="order-1")
    await sql.create_job(first, "fp")
    with pytest.raises(OperationExists) as exists:
        await sql.create_job(make_job(clock, ref="order-1"), "fp")
    assert exists.value.job.id == first.id
    await sql.create_job(make_job(clock, actor="bob", ref="order-1"), "fp")  # other actor: allowed
    await sql.create_job(make_job(clock), None)
    await sql.create_job(make_job(clock), None)  # many jobs without a ref
    found = await sql.job_by_operation_ref("default", "alice", "order-1")
    assert found is not None and found[1] == "fp"


async def test_failure_and_listing(sql: SqlStore, clock: FixedClock) -> None:
    job = make_job(clock, state="running")
    await sql.create_job(job, None)
    await sql.finish_job(job.id, "failed", Failure(kind="refused", message="busy"), None, clock.now())
    listed = await sql.list_jobs("default", "alice")
    assert [j.failure.kind for j in listed if j.failure] == ["refused"]
    assert await sql.list_jobs("default", "bob") == []


async def test_actors_keys_and_secrets(sql: SqlStore, clock: FixedClock) -> None:
    actor = ProvisionedActor(
        application_id="default", actor_id="alice", grant_id="g", allocation_id="a1", api_key_id="k1",
        secret_ref=SecretRef(ref="db:x"), currency="USD", status="active", created_at=clock.now(), updated_at=clock.now(),
    )  # fmt: skip
    await sql.save_actor(actor)
    await sql.save_actor(actor.model_copy(update={"status": "paused"}))
    loaded = await sql.get_actor("default", "alice")
    assert loaded is not None and loaded.status == "paused" and loaded.secret_ref.ref == "db:x"

    key = AccessKey(
        key_id="k", kind="actor", actor_id="alice", application_id="default", permissions=("jobs.run",),
        label="", created_at=clock.now(), revoked_at=None,
    )  # fmt: skip
    await sql.insert_access_key(key, "hash")
    assert (await sql.access_key_by_hash("hash")) == key
    assert await sql.revoke_access_key("k", clock.now()) is True
    assert await sql.revoke_access_key("k", clock.now()) is False

    await sql.put_secret("db:x", "ciphertext", clock.now())
    assert await sql.get_secret("db:x") == "ciphertext"
    await sql.delete_secret("db:x")
    assert await sql.get_secret("db:x") is None


async def test_usage_rows_cursor_projection_and_attribution(sql: SqlStore, clock: FixedClock) -> None:
    def row(row_id: str, session: str, fee: str) -> UsageRow:
        return UsageRow(
            id=row_id, event_id=None, status="applied", manifest_id=None, allocation_id="a1", payment_session_id=session,
            request_id=None, pipeline=None, computed_fee_eth=Decimal(fee), computed_fee_usd=None, currency=None,
            created_at=clock.now(),
        )  # fmt: skip

    new = await sql.apply_usage(
        [row("1", "s1", "0.000000000000000001"), row("2", "orphan", "2")], SyncCheckpoint(resume_cursor="c1")
    )
    assert [r.id for r in new] == ["1", "2"]
    assert await sql.apply_usage([row("1", "s1", "1")], SyncCheckpoint(resume_cursor="c2")) == []
    checkpoint = await sql.cost_checkpoint()
    assert checkpoint is not None and checkpoint.resume_cursor == "c2"
    (exact,) = await sql.usage_for_auth_ids(["s1"])
    assert exact.computed_fee_eth == Decimal("0.000000000000000001")  # exact decimals, never floats

    job = make_job(clock)
    await sql.create_job(job, None)
    await sql.start_attempt(make_attempt(job.id, clock))
    await sql.finish_attempt(make_attempt(job.id, clock, auth_ids=("s1",)))
    assert [r.id for r in await sql.unattributed_usage("a1")] == ["2"]

    projection = JobCost(
        job_id=job.id, status="observed", fees=Fees(fee_eth=Decimal("1.5"), fee_usd=None, row_count=1), attempts=(),
        first_observed_at=clock.now(), updated_at=clock.now(),
    )  # fmt: skip
    await sql.save_projection(projection)
    await sql.save_projection(projection.model_copy(update={"status": "corrected"}))
    loaded = await sql.get_projection(job.id)
    assert loaded is not None and loaded.status == "corrected" and loaded.fees.fee_eth == Decimal("1.5")
