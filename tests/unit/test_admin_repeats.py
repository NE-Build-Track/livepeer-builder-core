"""ISS-11 (D57): a repeated admin command never applies twice. The operation ledger answers a repeat.

Through the services, with the real SqlStore on SQLite and FakeAllocationManager, which replays
Batteries' idempotency keys. ``LPB_OPERATION_STALE_S`` (120 s by default) decides when a run that never
finished is resumed rather than reported in progress.
"""

import asyncio
from datetime import datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from livepeer_builder import EngineSettings
from livepeer_builder.contracts import ActorContext, FundRequest, Money, OperationRecord, ProvisionRequest
from livepeer_builder.errors import (
    AccessDenied,
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


def provision_request(actor_id: str = "alice", amount: str = "5") -> ProvisionRequest:
    return ProvisionRequest(
        application_id="default", actor_id=actor_id, amount=usd(amount), grant_id="grant_1"
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
    clock.advance(119)  # younger than LPB_OPERATION_STALE_S (120 s)
    with pytest.raises(OperationInProgress) as running:
        await engine.payments.fund(admin, "default", "alice", request)
    assert running.value.operation is not None and running.value.operation.state == "started"
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
    assert sorted(type(answer).__name__ for answer in answers) == ["OperationInProgress", "ProvisionedActor"]
    assert manager.calls.count("fund_allocation") == 2  # the timed-out call and one resume
    assert manager.allocations[actor.allocation_id].granted == Decimal(10)


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


async def test_the_stale_threshold_is_configurable_and_120_s_by_default(
    make_engine: EngineFactory, admin: ActorContext, manager: FakeAllocationManager, clock: FixedClock
) -> None:
    assert EngineSettings().operation_stale_s == 120  # Q3
    assert EngineSettings.from_env({"LPB_OPERATION_STALE_S": "30"}).operation_stale_s == 30
    engine = await make_engine(operation_stale_s=30)
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
