"""The EngineStore contract, identical on SQLite and PostgreSQL (ISS-06)."""

from collections.abc import AsyncIterator
from datetime import UTC, timedelta
from decimal import Decimal

import pytest

from livepeer_builder.contracts import (
    AccessKey,
    Attempt,
    AttemptCost,
    Failure,
    Fees,
    JobCost,
    MeasuredUsage,
    ProvisionedActor,
    ResultRef,
    SecretRef,
    SeenActor,
    SignedSession,
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
        make_attempt(job.id, clock).model_copy(update={"outcome": "in_flight", "ended_at": None})
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
    await sql.save_actor(actor.model_copy(update={"label": "Alice Smith"}))
    await sql.save_actor(actor.model_copy(update={"status": "paused", "label": "Alice Smith"}))
    loaded = await sql.get_actor("default", "alice")
    assert loaded is not None and loaded.status == "paused" and loaded.secret_ref.ref == "db:x"
    assert loaded.label == "Alice Smith"  # the readable name beside an opaque actor_id (D46)
    assert [a.label for a in await sql.list_actors("default")] == ["Alice Smith"]

    key = AccessKey(
        key_id="k", kind="actor", actor_id="alice", application_id="default", permissions=("jobs.run",),
        label="", created_at=clock.now(), revoked_at=None,
    )  # fmt: skip
    await sql.insert_access_key(key, "hash")
    assert (await sql.access_key_by_hash("hash")) == key
    assert await sql.revoke_access_key("k", clock.now()) is True
    assert await sql.revoke_access_key("k", clock.now()) is False

    assert await sql.get_access_key("k") == key.model_copy(update={"revoked_at": clock.now()})
    assert await sql.get_access_key("missing") is None

    await sql.put_secret("db:x", "ciphertext", clock.now())
    assert await sql.get_secret("db:x") == "ciphertext"
    await sql.delete_secret("db:x")
    assert await sql.get_secret("db:x") is None


async def test_rotating_a_key_revokes_it_and_inserts_its_replacement_together(
    sql: SqlStore, clock: FixedClock
) -> None:
    def key(key_id: str) -> AccessKey:
        return AccessKey(
            key_id=key_id, kind="actor", actor_id="alice", application_id="default", permissions=("jobs.run",),
            label="", created_at=clock.now(), revoked_at=None,
        )  # fmt: skip

    await sql.insert_access_key(key("old"), "old-hash")
    assert await sql.rotate_access_key("old", key("new"), "new-hash", clock.now()) is True
    old = await sql.get_access_key("old")
    assert old is not None and old.revoked_at == clock.now()
    assert await sql.access_key_by_hash("new-hash") == key("new")

    # a revoked or unknown key is not rotated, and its replacement is not inserted
    assert await sql.rotate_access_key("old", key("again"), "again-hash", clock.now()) is False
    assert await sql.rotate_access_key("missing", key("other"), "other-hash", clock.now()) is False
    assert await sql.get_access_key("again") is None and await sql.get_access_key("other") is None


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
    await sql.save_projection(projection, evidence_version=0)
    await sql.save_projection(projection.model_copy(update={"status": "corrected"}), evidence_version=0)
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
        ),
        evidence_version=0,
    )
    await sql.save_projection(
        JobCost(
            job_id=latest.id,
            status="observed",
            fees=delivered,
            attempts=(AttemptCost(attempt_number=1, cost_class="delivered", fees=delivered),),
            first_observed_at=clock.now(),
            updated_at=clock.now(),
        ),
        evidence_version=0,
    )

    selected = {job.id for job in await sql.jobs_for_projection(())}
    assert first.id in selected and unprojected.id in selected and latest.id not in selected
    async with sql.cost_snapshot() as reads:
        newest = await reads.recent_with_projections("default", None, 1)
        every = await reads.recent_with_projections(
            "default", None, 5
        )  # each recent job, projection if stored
    assert [(job.id, cost is not None) for job, cost in newest] == [(latest.id, True)]
    assert [(job.id, cost is not None) for job, cost in every] == [
        (latest.id, True),
        (unprojected.id, False),
        (first.id, True),
    ]


async def test_running_jobs_are_never_projected_and_reads_find_the_uncovered_jobs(
    sql: SqlStore, clock: FixedClock
) -> None:  # #33: a projection of a running job goes stale the moment it pays
    running = make_job(clock, state="running")
    await sql.create_job(running, None)
    covered = make_job(clock)
    await sql.create_job(covered, None)
    clock.advance(1)
    stale = make_job(clock)  # projected, then ended later: its projection predates its end
    await sql.create_job(stale, None)
    none = Fees(fee_eth=Decimal("0"), fee_usd=None, row_count=0)
    for job_id, at in ((covered.id, clock.now()), (stale.id, clock.now() - timedelta(seconds=5))):
        await sql.save_projection(
            JobCost(
                job_id=job_id, status="none", fees=none, attempts=(), first_observed_at=None, updated_at=at
            ),
            evidence_version=0,
        )
    assert running.id not in {job.id for job in await sql.jobs_for_projection([running.id])}
    window = (clock.now() - timedelta(hours=1), clock.now() + timedelta(hours=1))
    async with sql.cost_snapshot() as reads:
        stored, uncovered = await reads.projections_and_unprojected("default", None, *window)
        _, none_for_bob = await reads.projections_and_unprojected("default", "bob", *window)
    assert {job.id for job in uncovered} == {running.id, stale.id}
    assert {cost.job_id for cost in stored} == {covered.id, stale.id} and none_for_bob == []


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


async def test_a_cost_snapshot_does_not_see_a_write_committed_while_it_is_open(
    sql: SqlStore, clock: FixedClock
) -> None:  # a cost read must not mix states while cost sync writes (#43 review)
    def usage(row_id: str) -> UsageRow:
        return UsageRow(
            id=row_id, event_id=None, status="applied", manifest_id=None, allocation_id="a1",
            payment_session_id=None, request_id=None, pipeline=None, computed_fee_eth=Decimal("1"),
            computed_fee_usd=None, currency=None, created_at=clock.now(),
        )  # fmt: skip

    await sql.apply_usage([usage("u1")], SyncCheckpoint(resume_cursor="c1"))
    async with sql.cost_snapshot() as reads:
        assert [row.id for row in await reads.unattributed_usage("a1")] == ["u1"]
        await sql.apply_usage([usage("u2")], SyncCheckpoint(resume_cursor="c2"))  # a sync commits
        assert [row.id for row in await reads.unattributed_usage("a1")] == ["u1"]  # still the same state
    assert sorted(row.id for row in await sql.unattributed_usage("a1")) == ["u1", "u2"]


async def test_new_evidence_puts_a_projection_out_of_date_until_one_from_that_evidence_is_stored(
    sql: SqlStore, clock: FixedClock
) -> None:  # #43 review: sync commits rows and matches before it stores the projection, if it gets there
    def usage(row_id: str) -> UsageRow:
        return UsageRow(
            id=row_id, event_id=None, status="applied", manifest_id="m1", allocation_id="a1",
            payment_session_id="s1", request_id=None, pipeline=None, computed_fee_eth=Decimal("1"),
            computed_fee_usd=None, currency=None, created_at=clock.now(),
        )  # fmt: skip

    async def served() -> JobCost | None:
        async with sql.cost_snapshot() as reads:
            found = await reads.job_with_projection(job.id)
        assert found is not None
        return found[1]

    async def needs_projection() -> bool:
        return job.id in {selected.id for selected in await sql.jobs_for_projection([])}

    job = make_job(clock)  # ended
    await sql.create_job(job, None)
    await sql.start_attempt(make_attempt(job.id, clock))
    await sql.finish_attempt(make_attempt(job.id, clock, manifest_ids=("m1",), allocation_id="a1"))
    none = Fees(fee_eth=Decimal("0"), fee_usd=None, row_count=0)
    stored = JobCost(
        job_id=job.id, status="none", fees=none, attempts=(), first_observed_at=None, updated_at=clock.now()
    )

    async def version() -> int:
        found = await sql.job_for_projection(job.id)
        assert found is not None
        return found[1]

    await sql.save_projection(stored, evidence_version=await version())
    await sql.apply_usage([usage("u1")], SyncCheckpoint(resume_cursor="c1"))  # its session is not matched yet
    assert await served() is not None and not await needs_projection()
    before = await version()
    assert await sql.match_sessions([usage("u1")]) == {job.id}  # the match changed
    assert await served() is None and await needs_projection()
    await sql.save_projection(stored, evidence_version=before)  # a sync that classified before the match
    assert await served() is None and await needs_projection()  # cannot hide it
    current = await version()
    await sql.save_projection(stored, evidence_version=current)
    assert await served() is not None and not await needs_projection()
    await sql.save_projection(stored.model_copy(update={"status": "pending"}), evidence_version=before)
    served_now = await served()
    assert served_now is not None and served_now.status == "none"  # an older save never replaces a newer one
    await sql.apply_usage([usage("u2")], SyncCheckpoint(resume_cursor="c2"))  # a new row, matched session
    assert await served() is None and await needs_projection()
    assert await version() == current + 1
    assert await sql.get_projection(job.id) is not None  # sync still reads it (first observed, corrected)


async def test_a_late_evidence_report_never_reopens_a_finished_attempt(
    sql: SqlStore, clock: FixedClock
) -> None:
    job = make_job(clock, state="running")
    await sql.create_job(job, None)
    attempt = Attempt(
        job_id=job.id, number=1, runner_url="r", orchestrator_url=None, allocation_id="a1", sessions=(),
        payment_sent=False, outcome="in_flight", status_code=None, started_at=clock.now(), ended_at=None,
    )  # fmt: skip
    await sql.start_attempt(attempt)
    signed = attempt.model_copy(update={"sessions": (SignedSession(manifest_id="m1"),), "payment_sent": True})
    await sql.record_payment_evidence(signed)  # while the call runs
    done = signed.model_copy(update={"outcome": "succeeded", "status_code": 200, "ended_at": clock.now()})
    await sql.finish_attempt(done)
    late = done.model_copy(
        update={"sessions": (SignedSession(manifest_id="m1"), SignedSession(manifest_id="m2"))}
    )
    await sql.record_payment_evidence(late.model_copy(update={"outcome": "in_flight", "ended_at": None}))
    stored = await sql.get_job(job.id)
    assert stored is not None
    [kept] = stored.attempts
    assert (kept.outcome, kept.status_code, kept.payment_sent) == ("succeeded", 200, True)  # not reopened
    assert [session.manifest_id for session in kept.sessions] == ["m1", "m2"]  # appended once each


async def test_a_late_evidence_report_on_an_ended_job_puts_its_projection_out_of_date(
    sql: SqlStore, clock: FixedClock
) -> None:  # a projection stored before the report would serve none for a paid job
    job = make_job(clock)  # ended
    await sql.create_job(job, None)
    attempt = make_attempt(job.id, clock)
    await sql.start_attempt(attempt)
    none = Fees(fee_eth=Decimal("0"), fee_usd=None, row_count=0)
    await sql.save_projection(
        JobCost(
            job_id=job.id,
            status="none",
            fees=none,
            attempts=(),
            first_observed_at=None,
            updated_at=clock.now(),
        ),
        evidence_version=0,
    )
    late = attempt.model_copy(update={"sessions": (SignedSession(manifest_id="m1"),), "payment_sent": True})
    await sql.record_payment_evidence(late)
    async with sql.cost_snapshot() as reads:
        found = await reads.job_with_projection(job.id)
    assert found is not None and found[1] is None  # computed live: pending, not the stored none
    assert job.id in {selected.id for selected in await sql.jobs_for_projection([])}


async def test_sightings_keep_the_first_time_and_follow_the_name(sql: SqlStore, clock: FixedClock) -> None:
    """A person the identity provider signed in, before any allocation (D48)."""
    first = clock.now()
    seen = SeenActor(
        application_id="default", actor_id="4f6c2a1e", name="alice", first_seen_at=first, last_seen_at=first
    )
    await sql.save_sighting(seen)
    later = first + timedelta(hours=2)
    await sql.save_sighting(
        seen.model_copy(update={"name": "alice.smith", "first_seen_at": later, "last_seen_at": later})
    )
    await sql.save_sighting(
        SeenActor(application_id="app-2", actor_id="b", name="bob", first_seen_at=first, last_seen_at=first)
    )
    nameless = later + timedelta(hours=1)  # the name claim switched off: the name is kept, the time moves
    await sql.save_sighting(
        seen.model_copy(update={"name": "", "first_seen_at": nameless, "last_seen_at": nameless})
    )
    later = nameless
    [kept] = await sql.list_sightings("default")
    assert (kept.name, kept.first_seen_at, kept.last_seen_at) == ("alice.smith", first, later)
    assert await sql.get_sighting("default", "4f6c2a1e") == kept
    assert await sql.get_sighting("default", "nobody") is None
    assert [s.actor_id for s in await sql.list_sightings()] == ["b", "4f6c2a1e"]  # by application, then actor
