"""The EngineStore contract, identical on SQLite and PostgreSQL (ISS-06)."""

from collections.abc import AsyncIterator
from datetime import UTC
from decimal import Decimal

import pytest

from livepeer_builder.contracts import (
    AccessKey,
    AttemptCost,
    AttemptOutcome,
    CostStatus,
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


async def test_jobs_attempts_and_sessions_round_trip(sql: SqlStore, clock: FixedClock) -> None:
    job = make_job(clock, state="running")
    await sql.create_job(job, "fp")
    await sql.start_attempt(
        make_attempt(job.id, clock).model_copy(update={"outcome": AttemptOutcome.IN_FLIGHT, "ended_at": None})
    )
    await sql.finish_attempt(
        make_attempt(job.id, clock, manifest_ids=("b", "a", "b"), allocation_id="alloc-1")
    )
    result = ResultRef(storage="inline", content_type="application/json", size_bytes=3, sha256="0" * 64)
    await sql.finish_job(job.id, "succeeded", None, result, clock.now())
    await sql.set_usage(
        job.id, MeasuredUsage(quantities={"tokens": 3}, source="app_reported", reported_at=clock.now())
    )
    loaded = await sql.get_job(job.id)
    assert loaded is not None
    assert loaded.state == "succeeded" and loaded.result == result
    assert [(session.manifest_id, session.match) for session in loaded.attempts[0].sessions] == [
        ("b", "unmatched"),
        ("a", "unmatched"),
        ("b", "unmatched"),
    ]
    assert loaded.attempts[0].allocation_id == "alloc-1"
    assert loaded.usage is not None and loaded.usage.quantities == {"tokens": 3}
    assert loaded.created_at.tzinfo is UTC or loaded.created_at.utcoffset() is not None


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
    def row(row_id: str, session: str, fee: str, manifest: str | None) -> UsageRow:
        return UsageRow(
            id=row_id,
            event_id=None,
            status="applied",
            manifest_id=manifest,
            allocation_id="a1",
            payment_session_id=session,
            request_id=None,
            pipeline=None,
            computed_fee_eth=Decimal(fee),
            computed_fee_usd=None,
            currency=None,
            created_at=clock.now(),
        )

    new = await sql.apply_usage(
        [row("1", "s1", "0.000000000000000001", "m1"), row("2", "orphan", "2", "forged")],
        SyncCheckpoint(resume_cursor="c1"),
    )
    assert [r.id for r in new] == ["1", "2"]
    assert await sql.apply_usage([row("1", "s1", "1", "m1")], SyncCheckpoint(resume_cursor="c2")) == []
    checkpoint = await sql.cost_checkpoint()
    assert checkpoint is not None and checkpoint.resume_cursor == "c2"
    (exact,) = await sql.usage_for_payment_sessions(["s1"])
    assert exact.computed_fee_eth == Decimal("0.000000000000000001")  # exact decimals, never floats

    job = make_job(clock)
    await sql.create_job(job, None)
    await sql.start_attempt(make_attempt(job.id, clock))
    await sql.finish_attempt(make_attempt(job.id, clock, manifest_ids=("m1",), allocation_id="a1"))
    assert await sql.match_sessions(()) == {job.id}  # the row arrived before the attempt finished
    matched = await sql.get_job(job.id)
    assert matched is not None
    assert matched.attempts[0].sessions[0].payment_session_id == "s1"
    assert matched.attempts[0].sessions[0].match == "matched"
    assert [r.id for r in await sql.unattributed_usage("a1")] == ["2"]

    attempt_cost = AttemptCost(
        attempt_number=1,
        cost_class="delivered",
        fees=Fees(fee_eth=Decimal("1.5"), fee_usd=None, row_count=1),
    )
    projection = JobCost(
        job_id=job.id, status="observed", fees=Fees(fee_eth=Decimal("1.5"), fee_usd=None, row_count=1), attempts=(attempt_cost,),
        first_observed_at=clock.now(), updated_at=clock.now(),
    )  # fmt: skip
    await sql.save_projection(projection)
    await sql.save_projection(projection.model_copy(update={"status": CostStatus.CORRECTED}))
    loaded = await sql.get_projection(job.id)
    assert loaded is not None and loaded.status == "corrected" and loaded.fees.fee_eth == Decimal("1.5")
    assert loaded.attempts == (attempt_cost,)


async def test_empty_attempt_lists_are_rebuilt_and_recent_reads_are_newest_first(
    sql: SqlStore, clock: FixedClock
) -> None:
    first = make_job(clock)
    await sql.create_job(first, None)
    clock.advance(1)
    unprojected = make_job(clock)
    await sql.create_job(unprojected, None)
    clock.advance(1)
    latest = make_job(clock)
    await sql.create_job(latest, None)
    delivered = Fees(fee_eth=Decimal("2"), fee_usd=None, row_count=1)
    await sql.save_projection(
        JobCost(
            job_id=first.id,
            status="observed",
            fees=Fees(fee_eth=Decimal("1"), fee_usd=None, row_count=1),
            attempts=(),
            first_observed_at=clock.now(),
            updated_at=clock.now(),
        )
    )
    await sql.save_projection(
        JobCost(
            job_id=latest.id,
            status="observed",
            fees=delivered,
            attempts=(AttemptCost(attempt_number=1, cost_class="delivered", fees=delivered),),
            first_observed_at=clock.now(),
            updated_at=clock.now(),
        )
    )

    selected = {job.id for job in await sql.jobs_for_projection(())}
    assert first.id in selected and unprojected.id in selected and latest.id not in selected
    assert [cost.job_id for cost in await sql.recent_projections("default", None, 1)] == [latest.id]
    assert [cost.job_id for cost in await sql.recent_projections("default", None, 5)] == [
        latest.id,
        first.id,
    ]


async def test_session_rows_are_append_only(sql: SqlStore, clock: FixedClock) -> None:
    """A stream writes its sessions at open and at end; a match made in between survives (D39)."""
    usage = UsageRow(
        id="u1", event_id=None, status="applied", manifest_id="m1", allocation_id="a1", payment_session_id="s1",
        request_id=None, pipeline=None, computed_fee_eth=Decimal("1"), computed_fee_usd=None, currency=None,
        created_at=clock.now(),
    )  # fmt: skip
    await sql.apply_usage([usage], SyncCheckpoint(resume_cursor="c1"))
    job = make_job(clock, state="running")
    await sql.create_job(job, None)
    await sql.start_attempt(make_attempt(job.id, clock))
    await sql.finish_attempt(make_attempt(job.id, clock, manifest_ids=("m1",), allocation_id="a1"))
    assert await sql.match_sessions(()) == {job.id}  # cost sync matched it while the stream ran
    await sql.finish_attempt(make_attempt(job.id, clock, manifest_ids=("m1", "m2"), allocation_id="a1"))
    loaded = await sql.get_job(job.id)
    assert loaded is not None
    assert [(s.manifest_id, s.payment_session_id, s.match) for s in loaded.attempts[0].sessions] == [
        ("m1", "s1", "matched"),
        ("m2", None, "unmatched"),
    ]
