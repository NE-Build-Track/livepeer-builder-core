"""ISS-11 (D57): a repeated admin command never applies twice. The operation ledger answers a repeat.

Through the services, with the real SqlStore on SQLite and FakeAllocationManager, which replays
Batteries' idempotency keys. ``LPB_OPERATION_STALE_S`` (120 s by default) decides when a run that never
finished is resumed rather than reported in progress.
"""

import asyncio
from datetime import datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from livepeer_builder import EngineSettings
from livepeer_builder.contracts import (
    AccessKey,
    ActorContext,
    FundRequest,
    GrantRecord,
    Money,
    OperationRecord,
    ProvisionedActor,
    ProvisionRequest,
)
from livepeer_builder.errors import (
    AccessDenied,
    AlreadyProvisioned,
    InvalidRequest,
    NotFound,
    OperationConflict,
    OperationInProgress,
    ProviderError,
    ProviderUnavailable,
)
from livepeer_builder.services.ledger import definite_refusal
from livepeer_builder.store import SqlStore
from livepeer_builder.store import schema as t
from livepeer_builder.testing import FakeAllocationManager, FixedClock
from tests.conftest import EngineFactory


def usd(amount: str) -> Money:
    return Money(amount=Decimal(amount), currency="USD")


def provision_request(actor_id: str = "alice", amount: str = "5", *, ref: str = "") -> ProvisionRequest:
    return ProvisionRequest(
        application_id="default",
        actor_id=actor_id,
        amount=usd(amount),
        grant_id="grant_1",
        operation_ref=ref or f"provision:{actor_id}",
    )


async def test_funding_repeated_with_one_ref_is_answered_without_calling_the_provider_again(
    make_engine: EngineFactory, admin: ActorContext, manager: FakeAllocationManager
) -> None:
    engine = await make_engine()
    await engine.payments.provision(admin, provision_request(amount="0"))
    request = FundRequest(amount=usd("10"), operation_ref="topup-1")
    first = await engine.payments.fund(admin, "default", "alice", request)
    again = await engine.payments.fund(admin, "default", "alice", request)  # a lost response
    assert again == first
    assert manager.calls.count("fund_allocation") == 1


async def test_funding_with_one_ref_and_another_amount_is_a_conflict_before_the_provider_call(
    make_engine: EngineFactory, admin: ActorContext, manager: FakeAllocationManager
) -> None:
    engine = await make_engine()
    await engine.payments.provision(admin, provision_request(amount="0"))
    await engine.payments.fund(
        admin, "default", "alice", FundRequest(amount=usd("10"), operation_ref="topup-1")
    )
    with pytest.raises(OperationConflict) as conflict:
        await engine.payments.fund(
            admin, "default", "alice", FundRequest(amount=usd("11"), operation_ref="topup-1")
        )
    assert manager.calls.count("fund_allocation") == 1
    assert conflict.value.job is None
    assert conflict.value.operation is not None and conflict.value.operation.command == "fund"


async def test_a_repeat_while_the_first_run_may_still_be_going_is_in_progress(
    make_engine: EngineFactory, admin: ActorContext, manager: FakeAllocationManager, clock: FixedClock
) -> None:
    """The first call's outcome is unknown (the provider timed out), so its record stays started."""
    engine = await make_engine()
    await engine.payments.provision(admin, provision_request(amount="0"))
    request = FundRequest(amount=usd("10"), operation_ref="topup-1")
    manager.fail_once("fund_allocation", ProviderUnavailable("Batteries unreachable: timed out"))
    with pytest.raises(ProviderUnavailable):
        await engine.payments.fund(admin, "default", "alice", request)
    clock.advance(30)
    with pytest.raises(OperationInProgress) as early:
        await engine.payments.fund(admin, "default", "alice", request)
    assert early.value.retry_after_s == 90  # the seconds until a repeat may resume it
    clock.advance(89)  # 119 s: younger than LPB_OPERATION_STALE_S (120 s)
    with pytest.raises(OperationInProgress) as running:
        await engine.payments.fund(admin, "default", "alice", request)
    assert running.value.operation is not None and running.value.operation.state == "started"
    assert running.value.retry_after_s == 1
    assert manager.calls.count("fund_allocation") == 1


async def test_a_repeat_after_the_first_run_went_stale_resumes_it_and_applies_once(
    make_engine: EngineFactory, admin: ActorContext, manager: FakeAllocationManager, clock: FixedClock
) -> None:
    engine = await make_engine()
    actor = await engine.payments.provision(admin, provision_request(amount="0"))
    request = FundRequest(amount=usd("10"), operation_ref="topup-1")
    manager.fail_once("fund_allocation", ProviderUnavailable("Batteries unreachable: timed out"))
    with pytest.raises(ProviderUnavailable):
        await engine.payments.fund(admin, "default", "alice", request)
    clock.advance(120)  # LPB_OPERATION_STALE_S: the first run is presumed dead
    funded = await engine.payments.fund(admin, "default", "alice", request)
    assert funded.status == "active"
    assert manager.allocations[actor.allocation_id].granted == Decimal(10)
    assert await engine.payments.fund(admin, "default", "alice", request) == funded  # done: a replay
    assert manager.calls.count("fund_allocation") == 2  # the timed-out call and the resume


async def test_two_repeats_racing_to_resume_a_stale_run_resume_it_once(
    make_engine: EngineFactory,
    admin: ActorContext,
    manager: FakeAllocationManager,
    clock: FixedClock,
    store: SqlStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both repeats read the stale record before either takes it over; the fencing token lets one win."""
    engine = await make_engine()
    actor = await engine.payments.provision(admin, provision_request(amount="0"))
    request = FundRequest(amount=usd("10"), operation_ref="topup-1")
    manager.fail_once("fund_allocation", ProviderUnavailable("Batteries unreachable: timed out"))
    with pytest.raises(ProviderUnavailable):
        await engine.payments.fund(admin, "default", "alice", request)
    clock.advance(600)
    both_read = asyncio.Barrier(2)
    resume = store.resume_operation

    async def after_both_read(record: OperationRecord, started_at: datetime) -> OperationRecord | None:
        await both_read.wait()
        return await resume(record, started_at)

    monkeypatch.setattr(store, "resume_operation", after_both_read)
    answers = await asyncio.gather(
        *(engine.payments.fund(admin, "default", "alice", request) for _ in range(2)), return_exceptions=True
    )
    assert manager.calls.count("fund_allocation") == 2  # the timed-out call and one resume
    assert manager.allocations[actor.allocation_id].granted == Decimal(10)
    # The loser answers from the record as it is now: the winner's result once done, else in progress
    # with a full stale window, since the winner restarted it now (Cursor, #76 review: it said 1 s).
    assert any(isinstance(answer, ProvisionedActor) for answer in answers)
    lost = [answer for answer in answers if isinstance(answer, OperationInProgress)]
    assert [answer.retry_after_s for answer in lost] in ([], [120])
    assert lost, "the race should leave the loser in progress here: the winner is still funding"


async def test_funding_a_grant_with_one_ref_and_another_grant_is_a_conflict(
    make_engine: EngineFactory, admin: ActorContext, manager: FakeAllocationManager
) -> None:
    engine = await make_engine()
    request = FundRequest(amount=usd("10"), operation_ref="grant-topup-1", grant_id="grant_a")
    await engine.payments.fund_grant(admin, request)
    with pytest.raises(OperationConflict):
        await engine.payments.fund_grant(admin, request.model_copy(update={"grant_id": "grant_b"}))
    await engine.payments.fund_grant(admin, request)  # a lost response: answered from the ledger
    assert [grant for grant, _, _ in manager.funded_grants] == ["grant_a"]
    assert manager.calls.count("fund_grant") == 1


async def test_a_definite_refusal_frees_the_ref_for_a_corrected_repeat(
    make_engine: EngineFactory, admin: ActorContext, manager: FakeAllocationManager, store: SqlStore
) -> None:
    """A 4xx from the provider applied nothing, so the caller may fix the request and repeat it."""
    engine = await make_engine()
    request = FundRequest(amount=usd("10"), operation_ref="grant-topup-1", grant_id="grant_typo")
    manager.fail_once("fund_grant", ProviderError(404, "grant not found"))
    with pytest.raises(ProviderError):
        await engine.payments.fund_grant(admin, request)
    assert await store.get_operation("default", "operator", "grant-topup-1") is None
    await engine.payments.fund_grant(admin, request.model_copy(update={"grant_id": "grant_a"}))
    assert [grant for grant, _, _ in manager.funded_grants] == ["grant_a"]


@pytest.mark.parametrize(
    ("error", "refused"),
    [
        (InvalidRequest("choose a grant"), True),
        (NotFound("actor 'alice' is not provisioned"), True),
        (AccessDenied("admin only"), True),
        (ProviderError(400, "bad amount"), True),
        (ProviderError(409, "idempotency key was already used with different parameters"), True),
        (ProviderError(408, "request timeout"), False),  # the provider may still apply it
        (ProviderError(502, "bad gateway"), False),
        (ProviderUnavailable("Batteries unreachable: timed out"), False),
        (ConnectionError("the database went away"), False),
    ],
)
def test_only_a_definite_refusal_frees_a_reference(error: Exception, refused: bool) -> None:
    assert definite_refusal(error) is refused


# -- rotating the payment key (B1 and the crash between its steps) --------------------------------------


async def test_a_rotation_that_died_before_revoking_the_old_key_is_finished_by_a_resume(
    make_engine: EngineFactory,
    admin: ActorContext,
    alice: ActorContext,
    manager: FakeAllocationManager,
    clock: FixedClock,
) -> None:
    """The new key is stored and the actor uses it, then the old key's revoke times out. The old key must
    not stay live: a resume reads it from the ledger's context, not from the actor as it is now."""
    engine = await make_engine()
    before = await engine.payments.provision(admin, provision_request())
    manager.fail_once("revoke_key", ProviderUnavailable("Batteries unreachable: timed out"))
    with pytest.raises(ProviderUnavailable):
        await engine.payments.rotate_key(admin, "default", "alice", "rotate-1")
    assert manager.keys[before.api_key_id][2] is False  # the old key is still live
    clock.advance(120)
    rotated = await engine.payments.rotate_key(admin, "default", "alice", "rotate-1")
    assert manager.keys[before.api_key_id][2] is True  # revoked by the resume
    assert manager.keys[rotated.api_key_id][2] is False
    assert len(manager.keys) == 2  # provisioning's key and one rotation
    assert (await engine.payments.credential(alice)).token == manager.keys[rotated.api_key_id][1]


async def test_a_rotation_refused_after_its_new_key_was_stored_stays_resumable(
    make_engine: EngineFactory,
    admin: ActorContext,
    manager: FakeAllocationManager,
    clock: FixedClock,
) -> None:
    """Cursor, #76 review: a 4xx is a definite refusal, but here a step was already applied (the new key is
    stored and live). Freeing the reference would lose the old key from the ledger's context, and a repeat
    would take the new key for the old one: two live payment keys. The record stays started instead."""
    engine = await make_engine()
    before = await engine.payments.provision(admin, provision_request())
    manager.fail_once("revoke_key", ProviderError(409, "key busy"))
    with pytest.raises(ProviderError):
        await engine.payments.rotate_key(admin, "default", "alice", "rotate-1")
    assert manager.keys[before.api_key_id][2] is False  # the old key is still live
    clock.advance(120)
    rotated = await engine.payments.rotate_key(admin, "default", "alice", "rotate-1")
    assert manager.keys[before.api_key_id][2] is True  # revoked by the resume
    assert manager.keys[rotated.api_key_id][2] is False
    assert len(manager.keys) == 2


async def test_a_command_refused_before_any_step_applied_still_frees_its_reference(
    make_engine: EngineFactory, admin: ActorContext, manager: FakeAllocationManager
) -> None:
    """The other side of the rule: nothing was applied, so the caller may fix the request and repeat it."""
    engine = await make_engine()
    await engine.payments.provision(admin, provision_request(amount="0"))
    request = FundRequest(amount=usd("10"), operation_ref="topup-1")
    manager.fail_once("fund_allocation", ProviderError(400, "bad amount"))
    with pytest.raises(ProviderError):
        await engine.payments.fund(admin, "default", "alice", request)
    await engine.payments.fund(admin, "default", "alice", request)  # not in progress: the record was freed
    assert manager.calls.count("fund_allocation") == 2


async def test_a_rotation_resumed_after_storing_the_secret_failed_revokes_the_old_key_once(
    make_engine: EngineFactory,
    admin: ActorContext,
    alice: ActorContext,
    manager: FakeAllocationManager,
    clock: FixedClock,
    store: SqlStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = await make_engine()
    before = await engine.payments.provision(admin, provision_request())
    put_secret = store.put_secret

    async def unavailable(*args: object) -> None:
        monkeypatch.setattr(store, "put_secret", put_secret)  # only this one write fails
        raise ConnectionError("the database went away")

    monkeypatch.setattr(store, "put_secret", unavailable)
    revoked: list[str] = []
    revoke_key = manager.revoke_key

    async def counted(api_key_id: str) -> None:
        revoked.append(api_key_id)
        await revoke_key(api_key_id)

    monkeypatch.setattr(manager, "revoke_key", counted)
    with pytest.raises(ConnectionError):
        await engine.payments.rotate_key(admin, "default", "alice", "rotate-1")
    clock.advance(120)
    rotated = await engine.payments.rotate_key(admin, "default", "alice", "rotate-1")
    assert revoked == [before.api_key_id]
    assert manager.keys[rotated.api_key_id][2] is False and len(manager.keys) == 2
    assert (await engine.payments.credential(alice)).token == manager.keys[rotated.api_key_id][1]


async def test_a_rotation_resumed_after_its_secret_was_stored_keeps_one_copy_of_the_key(
    make_engine: EngineFactory,
    admin: ActorContext,
    clock: FixedClock,
    store: SqlStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The secret is written, then the actor's save fails. The resume reuses that secret, recorded in the
    ledger's context, instead of writing a second copy of the live key that nothing points to."""
    engine = await make_engine()
    await engine.payments.provision(admin, provision_request())
    save_actor = store.save_actor

    async def lost(actor: object) -> None:
        monkeypatch.setattr(store, "save_actor", save_actor)
        raise ConnectionError("the database went away")

    monkeypatch.setattr(store, "save_actor", lost)
    with pytest.raises(ConnectionError):
        await engine.payments.rotate_key(admin, "default", "alice", "rotate-1")
    clock.advance(120)
    rotated = await engine.payments.rotate_key(admin, "default", "alice", "rotate-1")
    async with store.engine.connect() as conn:
        refs = set((await conn.execute(select(t.secrets.c.ref))).scalars())
    assert refs == {rotated.secret_ref.ref}  # the old key's secret is deleted, and no orphan copy


async def test_a_slow_run_taken_over_by_a_resume_answers_with_the_recorded_result(
    make_engine: EngineFactory,
    admin: ActorContext,
    manager: FakeAllocationManager,
    clock: FixedClock,
    store: SqlStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first run is not dead, only slow: it funded, then stalled past LPB_OPERATION_STALE_S. The resume
    replays the funding at the provider and records its answer; the slow run's complete is fenced off, so
    both callers get the one recorded answer and the money moved once."""
    engine = await make_engine()
    actor = await engine.payments.provision(admin, provision_request(amount="0"))
    request = FundRequest(amount=usd("10"), operation_ref="topup-1")
    stalled, release = asyncio.Event(), asyncio.Event()
    fund_allocation = manager.fund_allocation

    async def slow_once(*args: object) -> None:
        monkeypatch.setattr(manager, "fund_allocation", fund_allocation)
        await fund_allocation(*args)  # type: ignore[arg-type]
        stalled.set()
        await release.wait()

    monkeypatch.setattr(manager, "fund_allocation", slow_once)
    slow = asyncio.create_task(engine.payments.fund(admin, "default", "alice", request))
    await stalled.wait()
    clock.advance(120)
    resumed = await engine.payments.fund(admin, "default", "alice", request)
    clock.advance(1)
    release.set()
    assert await slow == resumed
    record = await store.get_operation("default", "operator", "topup-1")
    assert (
        record is not None and record.fencing_token == 2 and record.result == resumed.model_dump(mode="json")
    )
    assert manager.allocations[actor.allocation_id].granted == Decimal(10)


async def test_a_slow_rotation_taken_over_while_storing_its_secret_drops_its_copy(
    make_engine: EngineFactory,
    admin: ActorContext,
    alice: ActorContext,
    manager: FakeAllocationManager,
    clock: FixedClock,
    store: SqlStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = await make_engine()
    before = await engine.payments.provision(admin, provision_request())
    stalled, release = asyncio.Event(), asyncio.Event()
    put_secret = store.put_secret

    async def slow_once(*args: object) -> None:
        monkeypatch.setattr(store, "put_secret", put_secret)
        await put_secret(*args)  # type: ignore[arg-type]
        stalled.set()
        await release.wait()

    monkeypatch.setattr(store, "put_secret", slow_once)
    slow = asyncio.create_task(engine.payments.rotate_key(admin, "default", "alice", "rotate-1"))
    await stalled.wait()
    clock.advance(120)
    resumed = await engine.payments.rotate_key(admin, "default", "alice", "rotate-1")
    release.set()
    assert await slow == resumed
    async with store.engine.connect() as conn:
        refs = set((await conn.execute(select(t.secrets.c.ref))).scalars())
    assert refs == {resumed.secret_ref.ref}
    assert manager.keys[before.api_key_id][2] is True and manager.keys[resumed.api_key_id][2] is False
    assert (await engine.payments.credential(alice)).token == manager.keys[resumed.api_key_id][1]


async def test_a_slow_run_whose_resume_is_still_going_is_told_it_is_in_progress(
    make_engine: EngineFactory,
    admin: ActorContext,
    manager: FakeAllocationManager,
    clock: FixedClock,
    store: SqlStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = await make_engine()
    await engine.payments.provision(admin, provision_request(amount="0"))
    request = FundRequest(amount=usd("10"), operation_ref="topup-1")
    stalled, release = asyncio.Event(), asyncio.Event()
    fund_allocation = manager.fund_allocation

    async def slow(*args: object) -> None:
        await fund_allocation(*args)  # type: ignore[arg-type]
        stalled.set()
        await release.wait()

    monkeypatch.setattr(manager, "fund_allocation", slow)
    first = asyncio.create_task(engine.payments.fund(admin, "default", "alice", request))
    await stalled.wait()
    clock.advance(120)
    held = await store.get_operation("default", "operator", "topup-1")
    assert (
        held is not None and await store.resume_operation(held, clock.now()) is not None
    )  # a resume, running
    release.set()
    with pytest.raises(OperationInProgress):
        await first


def test_the_stale_window_must_outlast_a_commands_batteries_calls() -> None:
    """Cursor, #76 review: a resume is a lease, not a lock. Grant creation has no provider idempotency key,
    so a resume must not start while the presumed-dead run's calls may still be in flight: its two calls
    (create, then read back) each take up to LPB_BATTERIES_TIMEOUT_S. The window is four of them."""
    with pytest.raises(ValidationError, match="LPB_OPERATION_STALE_S"):
        EngineSettings(operation_stale_s=60, batteries_timeout_s=30)
    assert EngineSettings(operation_stale_s=120, batteries_timeout_s=30).operation_stale_s == 120


async def test_the_stale_threshold_is_configurable_and_120_s_by_default(
    make_engine: EngineFactory, admin: ActorContext, manager: FakeAllocationManager, clock: FixedClock
) -> None:
    assert EngineSettings().operation_stale_s == 120  # Q3
    shorter = {"LPB_OPERATION_STALE_S": "30", "LPB_BATTERIES_TIMEOUT_S": "5"}
    assert EngineSettings.from_env(shorter).operation_stale_s == 30
    engine = await make_engine(operation_stale_s=30, batteries_timeout_s=5)
    await engine.payments.provision(admin, provision_request(amount="0"))
    request = FundRequest(amount=usd("10"), operation_ref="topup-1")
    manager.fail_once("fund_allocation", ProviderUnavailable("Batteries unreachable: timed out"))
    with pytest.raises(ProviderUnavailable):
        await engine.payments.fund(admin, "default", "alice", request)
    clock.advance(30)
    assert (await engine.payments.fund(admin, "default", "alice", request)).status == "active"


async def test_one_ref_used_for_another_command_is_a_conflict(
    make_engine: EngineFactory, admin: ActorContext, manager: FakeAllocationManager
) -> None:
    engine = await make_engine()
    await engine.payments.provision(admin, provision_request(amount="0"))
    await engine.payments.fund(admin, "default", "alice", FundRequest(amount=usd("10"), operation_ref="op-1"))
    with pytest.raises(OperationConflict):
        await engine.payments.rotate_key(admin, "default", "alice", "op-1")
    assert "create_key" not in manager.calls


# -- provisioning (slice 4) ---------------------------------------------------------------------------------


def test_a_provisioning_request_needs_an_operation_ref() -> None:
    """Q4: required now, so a repeat after a lost response can never create a second allocation."""
    with pytest.raises(ValidationError, match="operation_ref"):
        ProvisionRequest(application_id="default", actor_id="alice", amount=usd("1"))  # type: ignore[call-arg]
    with pytest.raises(ValidationError, match="operation_ref"):
        ProvisionRequest(application_id="default", actor_id="alice", amount=usd("1"), operation_ref="")


async def test_provisioning_with_one_ref_and_another_body_is_a_conflict_before_any_provider_call(
    make_engine: EngineFactory, admin: ActorContext, manager: FakeAllocationManager
) -> None:
    engine = await make_engine()
    await engine.payments.provision(admin, provision_request(ref="p-1"))
    with pytest.raises(OperationConflict):
        await engine.payments.provision(admin, provision_request(actor_id="bob", ref="p-1"))
    assert manager.calls == ["create_key_for_grant"]
    with pytest.raises(OperationConflict):
        await engine.payments.provision(admin, provision_request(amount="6", ref="p-1"))  # same actor


async def test_two_concurrent_provisions_with_one_ref_create_one_allocation(
    make_engine: EngineFactory, admin: ActorContext, manager: FakeAllocationManager
) -> None:
    engine = await make_engine()
    answers = await asyncio.gather(
        *(engine.payments.provision(admin, provision_request()) for _ in range(2)), return_exceptions=True
    )
    assert sorted(type(answer).__name__ for answer in answers) == ["OperationInProgress", "ProvisionedActor"]
    assert manager.calls == ["create_key_for_grant"] and len(manager.allocations) == 1


async def test_a_provisioning_resumed_after_its_secret_was_stored_keeps_one_copy_of_the_key(
    make_engine: EngineFactory,
    admin: ActorContext,
    alice: ActorContext,
    manager: FakeAllocationManager,
    clock: FixedClock,
    store: SqlStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = await make_engine()
    save_actor = store.save_actor

    async def lost(actor: object) -> None:
        monkeypatch.setattr(store, "save_actor", save_actor)
        raise ConnectionError("the database went away")

    monkeypatch.setattr(store, "save_actor", lost)
    with pytest.raises(ConnectionError):
        await engine.payments.provision(admin, provision_request())
    clock.advance(120)
    actor = await engine.payments.provision(admin, provision_request())
    async with store.engine.connect() as conn:
        refs = set((await conn.execute(select(t.secrets.c.ref))).scalars())
    assert refs == {actor.secret_ref.ref}
    assert (await engine.payments.credential(alice)).token == manager.keys[actor.api_key_id][1]


# -- creating a grant (slice 4, Q5) -----------------------------------------------------------------------


async def test_creating_a_grant_twice_with_one_ref_creates_one_grant(
    make_engine: EngineFactory, admin: ActorContext, manager: FakeAllocationManager
) -> None:
    engine = await make_engine()
    desk = engine.payments.grants
    assert desk is not None
    first = await desk.create_grant(admin, "ops", usd("5"), operation_ref="grant-1")
    again = await desk.create_grant(admin, "ops", usd("5"), operation_ref="grant-1")  # a lost response
    assert again == first
    assert manager.calls.count("create_grant") == 1 and len(manager.grants) == 1


async def test_a_grant_create_resumed_after_the_provider_created_it_adopts_that_grant(
    make_engine: EngineFactory,
    admin: ActorContext,
    manager: FakeAllocationManager,
    clock: FixedClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Batteries has no idempotency key on grant creation, so the engine tags each grant it creates with
    ``metadata.lpb_operation`` and a resume lists the grants to find it."""
    engine = await make_engine()
    desk = engine.payments.grants
    assert desk is not None
    create_grant = manager.create_grant

    async def answer_lost(*args: object, **kwargs: object) -> GrantRecord:
        monkeypatch.setattr(manager, "create_grant", create_grant)
        await create_grant(*args, **kwargs)  # type: ignore[arg-type]
        raise ProviderUnavailable("Batteries unreachable: timed out")

    monkeypatch.setattr(manager, "create_grant", answer_lost)
    with pytest.raises(ProviderUnavailable):
        await desk.create_grant(admin, "ops", usd("5"), operation_ref="grant-1")
    clock.advance(120)
    adopted = await desk.create_grant(admin, "ops", usd("5"), operation_ref="grant-1")
    assert manager.grants == [adopted] and manager.calls.count("create_grant") == 1
    assert adopted.metadata["lpb_operation"]


async def test_two_grants_with_one_name_and_different_refs_are_both_created(
    make_engine: EngineFactory, admin: ActorContext, manager: FakeAllocationManager
) -> None:
    engine = await make_engine()
    desk = engine.payments.grants
    assert desk is not None
    first = await desk.create_grant(admin, "ops", usd("5"), operation_ref="grant-1")
    second = await desk.create_grant(admin, "ops", usd("5"), operation_ref="grant-2")
    assert first.id != second.id and len(manager.grants) == 2
    assert first.metadata["lpb_operation"] != second.metadata["lpb_operation"]


# -- engine access keys (slice 4, B4) ---------------------------------------------------------------------


async def test_issuing_a_key_twice_with_one_ref_issues_one_key_and_the_replay_has_no_token(
    make_engine: EngineFactory, admin: ActorContext
) -> None:
    """B4: a lost response and a repeat must not leave two live credentials. The token is never stored, so a
    replay cannot show it again: it returns the key without it, and an admin who lost it rotates the key."""
    engine = await make_engine()
    assert engine.keys is not None
    first = await engine.keys.issue(admin, kind="actor", actor_id="alice", operation_ref="key-1")
    again = await engine.keys.issue(admin, kind="actor", actor_id="alice", operation_ref="key-1")
    assert first.token is not None and first.token.startswith("lpk_") and not first.replayed
    assert (again.key, again.token, again.replayed) == (first.key, None, True)
    assert [key.key_id for key in await engine.keys.list_keys(admin)] == [first.key.key_id]


async def test_rotating_a_key_repeated_after_success_rotates_once_and_the_replay_has_no_token(
    make_engine: EngineFactory, admin: ActorContext
) -> None:
    """Before the ledger, the repeat after a lost response answered 400 "no active key": the first call
    had already revoked it."""
    engine = await make_engine()
    assert engine.keys is not None
    issued = await engine.keys.issue(admin, kind="actor", actor_id="alice", operation_ref="key-1")
    rotated = await engine.keys.rotate(admin, issued.key.key_id, operation_ref="rotate-key-1")
    again = await engine.keys.rotate(admin, issued.key.key_id, operation_ref="rotate-key-1")
    assert rotated.token is not None and not rotated.replayed
    assert (again.key, again.token, again.replayed) == (rotated.key, None, True)
    assert len(await engine.keys.list_keys(admin)) == 2  # the old key and one replacement


async def test_a_repeat_that_found_the_ref_free_but_lost_the_write_answers_from_the_winner(
    make_engine: EngineFactory, admin: ActorContext, store: SqlStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The race window: the repeat looked before the first request's key and record committed. The store
    refuses its key because the reference is held, and it answers from the winner's record. (Real
    concurrency is a store contract test: this in-memory store shares one connection.)"""
    engine = await make_engine()
    assert engine.keys is not None
    first = await engine.keys.issue(admin, kind="actor", actor_id="alice", operation_ref="key-1")
    get_operation = store.get_operation

    async def looked_too_early(*scope: str) -> OperationRecord | None:
        monkeypatch.setattr(store, "get_operation", get_operation)
        return None

    monkeypatch.setattr(store, "get_operation", looked_too_early)
    again = await engine.keys.issue(admin, kind="actor", actor_id="alice", operation_ref="key-1")
    assert (again.key, again.token) == (first.key, None)
    assert len(await engine.keys.list_keys(admin)) == 1


async def test_a_rotation_repeat_that_looked_too_early_answers_from_the_first_rotation(
    make_engine: EngineFactory, admin: ActorContext, store: SqlStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = await make_engine()
    assert engine.keys is not None
    issued = await engine.keys.issue(admin, kind="actor", actor_id="alice", operation_ref="key-1")
    rotated = await engine.keys.rotate(admin, issued.key.key_id, operation_ref="rotate-key-1")
    get_operation, get_access_key = store.get_operation, store.get_access_key

    async def looked_too_early(*scope: str) -> OperationRecord | None:
        monkeypatch.setattr(store, "get_operation", get_operation)
        return None

    async def still_active(key_id: str) -> AccessKey | None:
        old = await get_access_key(key_id)
        return None if old is None else old.model_copy(update={"revoked_at": None})

    monkeypatch.setattr(store, "get_operation", looked_too_early)
    monkeypatch.setattr(store, "get_access_key", still_active)
    again = await engine.keys.rotate(admin, issued.key.key_id, operation_ref="rotate-key-1")
    assert (again.key, again.token) == (rotated.key, None)
    with pytest.raises(InvalidRequest, match="no active key"):  # rotated meanwhile by another reference
        await engine.keys.rotate(admin, issued.key.key_id, operation_ref="rotate-key-2")
    assert len(await engine.keys.list_keys(admin)) == 2


async def test_no_token_or_payment_key_is_ever_stored_in_the_ledger(
    make_engine: EngineFactory, admin: ActorContext, manager: FakeAllocationManager, store: SqlStore
) -> None:
    """Core belief 8: the ledger holds the public result and what a resume needs, never a secret."""
    engine = await make_engine()
    assert engine.keys is not None
    issued = await engine.keys.issue(admin, kind="actor", actor_id="alice", operation_ref="key-1")
    rotated = await engine.keys.rotate(admin, issued.key.key_id, operation_ref="rotate-key-1")
    await engine.payments.provision(admin, provision_request())
    await engine.payments.rotate_key(admin, "default", "alice", "rotate-payment-1")
    async with store.engine.connect() as conn:
        rows = str((await conn.execute(select(t.operations))).all())
    secrets = [issued.token, rotated.token] + [secret for _, secret, _ in manager.keys.values()]
    assert len(secrets) == 4 and all(secret and secret not in rows for secret in secrets)
    assert rows.count("'done'") == 4


async def test_issuing_with_one_ref_and_another_body_is_a_conflict(
    make_engine: EngineFactory, admin: ActorContext
) -> None:
    engine = await make_engine()
    assert engine.keys is not None
    await engine.keys.issue(admin, kind="actor", actor_id="alice", operation_ref="key-1")
    with pytest.raises(OperationConflict):
        await engine.keys.issue(admin, kind="actor", actor_id="bob", operation_ref="key-1")
    with pytest.raises(OperationConflict):  # one reference, another command
        await engine.keys.rotate(admin, "key_anything", operation_ref="key-1")
    assert len(await engine.keys.list_keys(admin)) == 1


async def test_provisioning_an_actor_already_provisioned_under_a_new_ref_is_refused(
    make_engine: EngineFactory, admin: ActorContext, manager: FakeAllocationManager
) -> None:
    """Mike, 29 October: returning the actor as if provisioned silently dropped the amount, so an operator
    could believe it was funded. Refused with the actor (409 ``already_provisioned``); funding is ``fund``.
    Nothing was applied, so the new reference stays free."""
    engine = await make_engine()
    first = await engine.payments.provision(admin, provision_request(ref="p-1"))
    with pytest.raises(AlreadyProvisioned) as refused:
        await engine.payments.provision(admin, provision_request(ref="p-2"))
    assert refused.value.actor == first and manager.calls == ["create_key_for_grant"]
    bob = await engine.payments.provision(admin, provision_request("bob", ref="p-2"))
    assert bob.actor_id == "bob"


async def test_a_resumed_provisioning_that_never_reached_the_provider_does_not_claim_another_refs_actor(
    make_engine: EngineFactory, admin: ActorContext, manager: FakeAllocationManager, clock: FixedClock
) -> None:
    """Cursor, #76 review: reference B dies before its provider call; reference A then provisions the actor
    with another amount. B's resume must not answer with A's actor as its own result (B's amount would be
    silently dropped): it is refused, applied nothing, and frees B's reference."""
    engine = await make_engine()
    manager.fail_once("create_key_for_grant", ProviderUnavailable("Batteries unreachable: refused"))
    with pytest.raises(ProviderUnavailable):
        await engine.payments.provision(admin, provision_request(amount="5", ref="p-b"))
    a = await engine.payments.provision(admin, provision_request(amount="7", ref="p-a"))
    clock.advance(120)
    with pytest.raises(AlreadyProvisioned) as refused:
        await engine.payments.provision(admin, provision_request(amount="5", ref="p-b"))
    assert refused.value.actor == a
    with pytest.raises(AlreadyProvisioned):  # freed, so not in progress: refused again at once
        await engine.payments.provision(admin, provision_request(amount="5", ref="p-b"))


async def test_a_resumed_provisioning_that_reached_the_provider_answers_with_the_shared_allocation(
    make_engine: EngineFactory,
    admin: ActorContext,
    manager: FakeAllocationManager,
    clock: FixedClock,
    store: SqlStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B got its key and stored its secret, then died before saving the actor; A then provisioned the same
    actor. The provider key is actor-derived, so A got B's allocation replayed: B's command did apply, and
    its resume answers with the actor, dropping B's now unused copy of the secret."""
    engine = await make_engine()
    save_actor = store.save_actor

    async def lost(actor: object) -> None:
        monkeypatch.setattr(store, "save_actor", save_actor)
        raise ConnectionError("the database went away")

    monkeypatch.setattr(store, "save_actor", lost)
    with pytest.raises(ConnectionError):
        await engine.payments.provision(admin, provision_request(ref="p-b"))
    a = await engine.payments.provision(admin, provision_request(ref="p-a"))
    clock.advance(120)
    assert await engine.payments.provision(admin, provision_request(ref="p-b")) == a
    assert manager.calls == ["create_key_for_grant", "create_key_for_grant"]  # one allocation, replayed
    async with store.engine.connect() as conn:
        refs = set((await conn.execute(select(t.secrets.c.ref))).scalars())
    assert refs == {a.secret_ref.ref}


async def test_a_provisioning_resumed_after_its_actor_was_saved_returns_that_actor(
    make_engine: EngineFactory,
    admin: ActorContext,
    manager: FakeAllocationManager,
    clock: FixedClock,
    store: SqlStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The actor a resume finds may be its own earlier run's: that is the command's result, not a refusal."""
    engine = await make_engine()
    complete = store.complete_operation

    async def lost(*args: object) -> bool:
        monkeypatch.setattr(store, "complete_operation", complete)
        raise ConnectionError("the database went away")

    monkeypatch.setattr(store, "complete_operation", lost)
    with pytest.raises(ConnectionError):
        await engine.payments.provision(admin, provision_request())
    saved = await store.get_actor("default", "alice")
    clock.advance(120)
    assert await engine.payments.provision(admin, provision_request()) == saved
    assert manager.calls == ["create_key_for_grant"]
