"""#115 C4: one admin command never undoes another on the same actor.

``fund``, ``set_status``, ``revoke`` and ``rotate_key`` each read the actor, called the provider, then
saved the whole row from that read. A command that finished in between was overwritten: a rotation's new
key by a pause, a pause by a rotation, a revoke by anything. Each command now writes only its own columns,
under a condition in the SQL.

Every race here is played in a fixed order: the second command runs to its end while the first is at the
provider, which is after the first read the actor and before it writes.
"""

from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any

import pytest

from livepeer_builder import BuilderEngine, EngineSupply
from livepeer_builder.contracts import ActorContext, FundRequest, Money, ProvisionedActor, ProvisionRequest
from livepeer_builder.errors import (
    AlreadyProvisioned,
    InvalidRequest,
    NotFound,
    NotProvisioned,
    OperationInProgress,
    ProviderUnavailable,
)
from livepeer_builder.protocols import ActorCommandRepo
from livepeer_builder.store import SqlStore
from livepeer_builder.testing import FakeAllocationManager, FixedClock, MemorySecretStore
from tests.conftest import EngineFactory

Command = Callable[[], Awaitable[object]]


def usd(amount: str) -> Money:
    return Money(amount=Decimal(amount), currency="USD")


def provision_request(amount: str = "5") -> ProvisionRequest:
    return ProvisionRequest(
        application_id="default",
        actor_id="alice",
        amount=usd(amount),
        grant_id="grant_1",
        operation_ref="provision:alice",
    )


def during(manager: FakeAllocationManager, call: str, other: Command) -> None:
    """Run ``other`` to its end while the next ``call`` is at the provider: the command making that call
    has read the actor and has not written it yet."""
    original = getattr(manager, call)

    async def interleaved(*args: Any, **kwargs: Any) -> Any:
        setattr(manager, call, original)  # once
        answer = await original(*args, **kwargs)
        await other()
        return answer

    setattr(manager, call, interleaved)


class Desk:
    """The four commands on one actor, as an admin gives them."""

    def __init__(self, engine: BuilderEngine, admin: ActorContext) -> None:
        self.payments, self.admin = engine.payments, admin

    async def pause(self) -> ProvisionedActor:
        return await self.payments.set_status(self.admin, "default", "alice", "paused")

    async def activate(self) -> ProvisionedActor:
        return await self.payments.set_status(self.admin, "default", "alice", "active")

    async def fund(self) -> ProvisionedActor:
        request = FundRequest(amount=usd("1"), operation_ref="fund-1")
        return await self.payments.fund(self.admin, "default", "alice", request)

    async def revoke(self) -> ProvisionedActor:
        return await self.payments.revoke(self.admin, "default", "alice")

    async def rotate(self) -> ProvisionedActor:
        return await self.payments.rotate_key(self.admin, "default", "alice", "rotate-1")


PROVIDER_CALL = {
    "pause": "set_allocation_status",
    "activate": "set_allocation_status",
    "fund": "fund_allocation",
    "revoke": "revoke_allocation",
    "rotate": "create_key",
}


@pytest.fixture
def vault() -> MemorySecretStore:
    return MemorySecretStore()


@pytest.fixture
async def desk(make_engine: EngineFactory, admin: ActorContext, vault: MemorySecretStore) -> Desk:
    engine = await make_engine(supply=EngineSupply(secret_store=vault))
    return Desk(engine, admin)


@pytest.mark.parametrize("command", ["pause", "fund"])
async def test_a_status_command_does_not_undo_a_key_rotation_that_finished_meanwhile(
    desk: Desk, alice: ActorContext, manager: FakeAllocationManager, vault: MemorySecretStore, command: str
) -> None:
    """Before, the command saved the row it read: the old key, which the rotation had just revoked, and
    the reference of a secret the rotation had just deleted. The actor could not pay again."""
    before = await desk.payments.provision(desk.admin, provision_request())
    during(manager, PROVIDER_CALL[command], desk.rotate)
    answer = await getattr(desk, command)()

    stored = await desk.payments.get_actor(desk.admin, "default", "alice")
    assert stored.api_key_id != before.api_key_id and manager.keys[stored.api_key_id][2] is False
    assert stored.status == ("paused" if command == "pause" else "active")
    assert answer == stored  # the answer is the row as stored, the rotation's key included
    assert (await desk.payments.credential(alice)).token == manager.keys[stored.api_key_id][1]
    assert list(vault.values) == [stored.secret_ref.ref]


async def test_a_revoke_keeps_a_rotations_columns_and_forgets_its_key_too(
    desk: Desk, manager: FakeAllocationManager, vault: MemorySecretStore
) -> None:
    before = await desk.payments.provision(desk.admin, provision_request())
    during(manager, "revoke_allocation", desk.rotate)
    revoked = await desk.revoke()

    stored = await desk.payments.get_actor(desk.admin, "default", "alice")
    assert revoked == stored and stored.status == "revoked"
    assert stored.api_key_id != before.api_key_id  # the rotation's write was not overwritten
    assert vault.values == {}  # a revoke forgets the key: the one the rotation stored meanwhile as well


async def test_a_key_rotation_does_not_undo_a_pause_that_finished_meanwhile(
    desk: Desk, manager: FakeAllocationManager
) -> None:
    """Before, the rotation saved the row it read, status ``active`` included: the pause was lost here
    while the provider kept the allocation paused."""
    before = await desk.payments.provision(desk.admin, provision_request())
    during(manager, "create_key", desk.pause)
    rotated = await desk.rotate()

    stored = await desk.payments.get_actor(desk.admin, "default", "alice")
    assert stored.status == "paused" and stored.api_key_id != before.api_key_id
    assert rotated == stored


@pytest.mark.parametrize("command", ["pause", "activate", "fund", "rotate"])
async def test_no_command_moves_an_actor_out_of_revoked(
    desk: Desk, alice: ActorContext, manager: FakeAllocationManager, vault: MemorySecretStore, command: str
) -> None:
    """A revoke finishes while another command is at the provider. Before, that command then saved its
    stale row: the actor was ``active`` or ``paused`` again here, on an allocation the provider had
    revoked, with a secret reference the revoke had deleted."""
    await desk.payments.provision(desk.admin, provision_request("0" if command == "fund" else "5"))
    during(manager, PROVIDER_CALL[command], desk.revoke)
    if command == "fund":  # the funds were sent: the answer is the actor as it is stored
        assert (await desk.fund()).status == "revoked"
    else:
        with pytest.raises(NotFound, match="is not provisioned"):
            await getattr(desk, command)()

    stored = await desk.payments.get_actor(desk.admin, "default", "alice")
    assert stored.status == "revoked"
    with pytest.raises(NotProvisioned):
        await desk.payments.credential(alice)
    assert vault.values == {}  # nothing is kept for a revoked actor, a key rotated meanwhile included


async def test_two_revokes_at_once_answer_alike(desk: Desk, manager: FakeAllocationManager) -> None:
    """The slower revoke finds the actor already revoked: it writes nothing and answers as a repeat does."""
    await desk.payments.provision(desk.admin, provision_request())
    answers: list[ProvisionedActor] = []

    async def faster() -> None:
        answers.append(await desk.revoke())

    during(manager, "revoke_allocation", faster)
    answers.append(await desk.revoke())
    assert answers[0] == answers[1] and answers[0].status == "revoked"
    assert await desk.payments.get_actor(desk.admin, "default", "alice") == answers[0]


async def test_a_fund_moves_exhausted_to_active(desk: Desk) -> None:
    provisioned = await desk.payments.provision(desk.admin, provision_request("0"))
    assert provisioned.status == "exhausted"
    funded = await desk.fund()
    assert funded.status == "active"
    assert (await desk.payments.get_actor(desk.admin, "default", "alice")).status == "active"


@pytest.mark.parametrize("amount", ["5", "0"], ids=["active", "exhausted"])
async def test_a_fund_does_not_undo_a_pause_that_finished_meanwhile(
    desk: Desk, manager: FakeAllocationManager, amount: str
) -> None:
    """A fund moves ``exhausted`` to ``active`` and nothing else. Before, it wrote the status it had read
    (``active``), or ``active`` for the ``exhausted`` it had read, over the pause."""
    await desk.payments.provision(desk.admin, provision_request(amount))
    during(manager, "fund_allocation", desk.pause)
    funded = await desk.fund()

    stored = await desk.payments.get_actor(desk.admin, "default", "alice")
    assert stored.status == "paused" and funded == stored


async def test_a_fund_that_changes_no_status_writes_nothing(desk: Desk, clock: FixedClock) -> None:
    provisioned = await desk.payments.provision(desk.admin, provision_request())
    clock.advance(60)
    assert await desk.fund() == provisioned  # ``updated_at`` included
    assert await desk.payments.get_actor(desk.admin, "default", "alice") == provisioned


async def test_a_status_command_writes_the_status_and_the_time_only(desk: Desk, clock: FixedClock) -> None:
    provisioned = await desk.payments.provision(desk.admin, provision_request())
    clock.advance(60)
    paused = await desk.pause()
    assert paused == provisioned.model_copy(update={"status": "paused", "updated_at": clock.now()})
    assert await desk.payments.get_actor(desk.admin, "default", "alice") == paused


# -- two rotations of one actor's key ---------------------------------------------------------------------


def live_keys(manager: FakeAllocationManager) -> list[str]:
    """The keys the provider would still take: not revoked, on an allocation that is not revoked."""
    return [
        key_id
        for key_id, (allocation, _, revoked) in manager.keys.items()
        if not revoked and manager.allocations[allocation].status != "revoked"
    ]


async def rotate_2(desk: Desk) -> ProvisionedActor:
    return await desk.payments.rotate_key(desk.admin, "default", "alice", "rotate-2")


async def holds_only_the_key_of(
    desk: Desk, alice: ActorContext, manager: FakeAllocationManager, vault: MemorySecretStore
) -> ProvisionedActor:
    """The actor as stored, after checking that its key is the only live one and its secret the only one
    stored: no rotation left a key live, or a secret stored, that nothing references."""
    stored = await desk.payments.get_actor(desk.admin, "default", "alice")
    assert live_keys(manager) == [stored.api_key_id]
    assert list(vault.values) == [stored.secret_ref.ref]
    assert (await desk.payments.credential(alice)).token == manager.keys[stored.api_key_id][1]
    return stored


async def test_a_key_rotation_does_not_undo_a_rotation_that_finished_meanwhile(
    desk: Desk, alice: ActorContext, manager: FakeAllocationManager, vault: MemorySecretStore
) -> None:
    """Before, the key write asked only for the same allocation and an actor not revoked, so the slower
    rotation wrote its key over the finished one: that one had answered with a key the actor no longer
    held, and its key stayed live with its secret stored and nothing pointing at it."""
    await desk.payments.provision(desk.admin, provision_request())
    finished: list[ProvisionedActor] = []

    async def other() -> None:
        finished.append(await rotate_2(desk))

    during(manager, "create_key", other)  # rotate-1 has minted its key; rotate-2 runs to its end
    with pytest.raises(InvalidRequest, match="another key rotation"):
        await desk.rotate()
    assert await holds_only_the_key_of(desk, alice, manager, vault) == finished[0]


async def test_a_rotation_resumed_after_a_newer_one_finished_does_not_put_its_own_key_back(
    desk: Desk,
    alice: ActorContext,
    manager: FakeAllocationManager,
    vault: MemorySecretStore,
    clock: FixedClock,
) -> None:
    """rotate-1 stored its key and timed out revoking the old one; rotate-2 then replaced rotate-1's key,
    revoked it and deleted its secret. Before, the resume of rotate-1 found the actor without its key and
    wrote it again: a revoked key and the reference of a deleted secret, so the actor could not pay. Now
    it leaves the actor alone and still revokes the key it began from, which nothing else would."""
    before = await desk.payments.provision(desk.admin, provision_request())
    manager.fail_once("revoke_key", ProviderUnavailable("Batteries unreachable: timed out"))
    with pytest.raises(ProviderUnavailable):
        await desk.rotate()
    second = await rotate_2(desk)
    assert manager.keys[before.api_key_id][2] is False  # rotate-1's old key: its revoke never got through
    clock.advance(120)  # LPB_OPERATION_STALE_S: rotate-1's first run is presumed dead
    with pytest.raises(InvalidRequest, match="another key rotation"):
        await desk.rotate()
    assert await holds_only_the_key_of(desk, alice, manager, vault) == second


async def test_a_rotation_that_lost_keeps_its_reference_and_answers_the_same_again(
    desk: Desk, manager: FakeAllocationManager, clock: FixedClock
) -> None:
    """The reference is never freed: the provider replays the revoked key to any rotation under it. The
    record notes what happened, so a repeat is refused in the same words before any provider call."""
    await desk.payments.provision(desk.admin, provision_request())
    during(manager, "create_key", lambda: rotate_2(desk))
    with pytest.raises(InvalidRequest, match="another key rotation") as lost:
        await desk.rotate()
    with pytest.raises(OperationInProgress):  # the record is still held
        await desk.rotate()
    third = await desk.payments.rotate_key(desk.admin, "default", "alice", "rotate-3")
    calls = list(manager.calls)
    clock.advance(120)
    with pytest.raises(InvalidRequest) as again:
        await desk.rotate()
    assert str(again.value) == str(lost.value) and manager.calls == calls
    assert await desk.payments.get_actor(desk.admin, "default", "alice") == third


async def test_a_rotation_that_lost_and_could_not_revoke_its_own_key_finishes_on_a_repeat(
    desk: Desk,
    alice: ActorContext,
    manager: FakeAllocationManager,
    vault: MemorySecretStore,
    clock: FixedClock,
) -> None:
    """The losing rotation revokes its own key before it deletes that key's secret. When the revoke does
    not get through, the key is still live, so its secret is still stored and the record still open."""
    await desk.payments.provision(desk.admin, provision_request())

    async def other() -> None:
        await rotate_2(desk)
        manager.fail_once("revoke_key", ProviderUnavailable("Batteries unreachable: timed out"))

    during(manager, "create_key", other)
    with pytest.raises(ProviderUnavailable):
        await desk.rotate()
    assert len(live_keys(manager)) == 2 and len(vault.values) == 2  # the losing key, with its secret
    clock.advance(120)
    with pytest.raises(InvalidRequest, match="another key rotation"):
        await desk.rotate()
    await holds_only_the_key_of(desk, alice, manager, vault)


async def provisioned_again(desk: Desk) -> ProvisionedActor:
    """Revoked, then provisioned again under another reference: the actor holds a new allocation."""
    await desk.revoke()
    again = provision_request().model_copy(update={"operation_ref": "provision-again"})
    return await desk.payments.provision(desk.admin, again)


async def test_a_rotation_resumed_after_a_revoke_and_a_new_provisioning_is_refused(
    desk: Desk,
    alice: ActorContext,
    manager: FakeAllocationManager,
    vault: MemorySecretStore,
    clock: FixedClock,
) -> None:
    """rotate-1 timed out at the provider, then the actor was revoked and provisioned again. Before, the
    resume minted a key on the new allocation and revoked "the old key" its record named, a key of the
    first allocation: it answered with the new key while the key the new allocation had before stayed
    live, with its secret stored. A rotation belongs to the allocation it began on."""
    first = await desk.payments.provision(desk.admin, provision_request())
    manager.fail_once("create_key", ProviderUnavailable("Batteries unreachable: timed out"))
    with pytest.raises(ProviderUnavailable):
        await desk.rotate()
    second = await provisioned_again(desk)
    calls = list(manager.calls)
    clock.advance(120)
    with pytest.raises(NotFound, match=f"began on allocation '{first.allocation_id}'") as refused:
        await desk.rotate()
    assert manager.calls == calls  # nothing was sent for the new allocation
    assert await holds_only_the_key_of(desk, alice, manager, vault) == second
    clock.advance(120)  # a refusal on a resumed run keeps its reference: the same answer again
    with pytest.raises(NotFound) as again:
        await desk.rotate()
    assert str(again.value) == str(refused.value)


async def test_such_a_rotation_drops_the_secret_it_had_stored_for_the_revoked_allocation(
    desk: Desk,
    alice: ActorContext,
    store: SqlStore,
    manager: FakeAllocationManager,
    vault: MemorySecretStore,
    clock: FixedClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await desk.payments.provision(desk.admin, provision_request())

    async def lost(*_: object, **__: object) -> None:
        raise ConnectionError("the database went away")

    with monkeypatch.context() as losing:
        losing.setattr(store, "set_actor_key", lost)
        with pytest.raises(
            ConnectionError
        ):  # the new key's secret is stored and noted; the actor is not written
            await desk.rotate()
    second = await provisioned_again(desk)
    assert len(vault.values) == 2  # the new allocation's, and the one the rotation stored
    clock.advance(120)
    with pytest.raises(NotFound, match="began on allocation"):
        await desk.rotate()
    assert await holds_only_the_key_of(desk, alice, manager, vault) == second


# -- a provisioning that another command overtook --------------------------------------------------------


def provision_b() -> ProvisionRequest:
    """The same actor under another reference: the provider call is the same one (its idempotency key is
    derived from the actor), so both references get one allocation and one key."""
    return provision_request().model_copy(update={"operation_ref": "provision-b"})


async def overtaken_provisioning(desk: Desk, manager: FakeAllocationManager, *then: str) -> None:
    """provision-b has read the actor and is at the provider while the other reference provisions the
    actor and the commands ``then`` run to their end."""

    async def other() -> None:
        await desk.payments.provision(desk.admin, provision_request())
        for command in then:
            await getattr(desk, command)()

    during(manager, "create_key_for_grant", other)


@pytest.fixture(params=["new", "revoked"])
async def start(request: pytest.FixtureRequest, desk: Desk) -> str:
    """Provisioning inserts the row of a new actor and replaces the row of a revoked one."""
    if request.param == "revoked":
        first = provision_request().model_copy(update={"operation_ref": "provision-0"})
        await desk.payments.provision(desk.admin, first)
        await desk.revoke()
    return str(request.param)


async def test_a_provisioning_does_not_move_an_actor_out_of_revoked(
    desk: Desk, alice: ActorContext, manager: FakeAllocationManager, vault: MemorySecretStore, start: str
) -> None:
    """Before, provisioning saved the whole row it had built from its first read: ``active``, on an
    allocation the provider had revoked meanwhile. Now the actor stays revoked, and the provisioning,
    whose allocation was funded and then revoked, answers with the actor as stored, as a fund does."""
    await overtaken_provisioning(desk, manager, "revoke")
    answer = await desk.payments.provision(desk.admin, provision_b())

    stored = await desk.payments.get_actor(desk.admin, "default", "alice")
    assert stored.status == "revoked" and answer == stored
    assert manager.allocations[stored.allocation_id].status == "revoked"
    assert len(manager.allocations) == (2 if start == "revoked" else 1)  # nothing more was funded
    with pytest.raises(NotProvisioned):
        await desk.payments.credential(alice)
    assert vault.values == {}  # this run's copy of the secret is deleted too


async def test_a_provisioning_does_not_undo_a_pause_that_finished_meanwhile(
    desk: Desk, alice: ActorContext, manager: FakeAllocationManager, vault: MemorySecretStore, start: str
) -> None:
    """Before, the actor was stored ``active`` while the provider kept the allocation paused, and the
    secret the first provisioning stored was left with nothing pointing at it."""
    await overtaken_provisioning(desk, manager, "pause")
    answer = await desk.payments.provision(desk.admin, provision_b())

    stored = await holds_only_the_key_of(desk, alice, manager, vault)
    assert stored.status == "paused" and answer == stored  # the same allocation and key: its result too
    assert manager.allocations[stored.allocation_id].status == "paused"


async def test_a_provisioning_does_not_undo_a_key_rotation_that_finished_meanwhile(
    desk: Desk, alice: ActorContext, manager: FakeAllocationManager, vault: MemorySecretStore, start: str
) -> None:
    """Before, the actor was pointed back at the key the rotation had just revoked. The actor no longer
    holds the key this provisioning got, so it is refused as any provisioning of a provisioned actor."""
    await overtaken_provisioning(desk, manager, "rotate")
    with pytest.raises(AlreadyProvisioned) as refused:
        await desk.payments.provision(desk.admin, provision_b())

    stored = await holds_only_the_key_of(desk, alice, manager, vault)
    assert refused.value.actor == stored and stored.status == "active"


@pytest.mark.parametrize("revoked_again", [False, True])
async def test_a_provisioning_whose_allocation_was_revoked_and_replaced_meanwhile_is_refused(
    desk: Desk, manager: FakeAllocationManager, vault: MemorySecretStore, revoked_again: bool
) -> None:
    """The actor was provisioned, revoked and provisioned again while provision-b was at the provider: the
    row is another allocation's. Nothing is written or funded, and this run's copy of the secret of its
    revoked allocation's key is deleted. The actor is another reference's now, or revoked again."""

    async def again() -> None:
        third = provision_request().model_copy(update={"operation_ref": "provision-c"})
        await desk.payments.provision(desk.admin, third)

    desk.again = again  # type: ignore[attr-defined]
    await overtaken_provisioning(desk, manager, "revoke", "again", *(["revoke"] if revoked_again else []))
    refusal = pytest.raises(NotFound, match="revoked while this provisioning")
    with refusal if revoked_again else pytest.raises(AlreadyProvisioned):
        await desk.payments.provision(desk.admin, provision_b())

    stored = await desk.payments.get_actor(desk.admin, "default", "alice")
    assert stored.status == ("revoked" if revoked_again else "active")
    assert [allocation.status for allocation in manager.allocations.values()] == ["revoked", stored.status]
    assert list(vault.values) == ([] if revoked_again else [stored.secret_ref.ref])


# -- a supplied store without the conditional writes ------------------------------------------------------


class _WholeRowStore:
    """A supplied store from before #115: everything the SQL store does, without the two column writes."""

    def __init__(self, inner: SqlStore) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        if name in ("set_actor_status", "set_actor_key", "provision_actor"):
            raise AttributeError(name)
        return getattr(self._inner, name)


@pytest.fixture
async def whole_row_desk(
    make_engine: EngineFactory, store: SqlStore, admin: ActorContext, vault: MemorySecretStore
) -> Desk:
    supplied = _WholeRowStore(store)
    assert isinstance(store, ActorCommandRepo) and not isinstance(supplied, ActorCommandRepo)
    engine = await make_engine(supply=EngineSupply(store=supplied, secret_store=vault))  # type: ignore[arg-type]
    return Desk(engine, admin)


async def test_a_store_without_the_column_writes_still_runs_every_command(
    whole_row_desk: Desk,
    alice: ActorContext,
    manager: FakeAllocationManager,
    vault: MemorySecretStore,
    clock: FixedClock,
) -> None:
    """The conditional writes are optional for a store (``ActorCommandRepo``). Without them the engine
    reads the actor again, checks the same rules itself and saves the whole row with ``save_actor``."""
    desk = whole_row_desk
    provisioned = await desk.payments.provision(desk.admin, provision_request("0"))
    clock.advance(60)
    funded = await desk.fund()
    assert funded == provisioned.model_copy(update={"status": "active", "updated_at": clock.now()})
    assert (await desk.pause()).status == "paused"
    assert (await desk.activate()).status == "active"

    rotated = await desk.rotate()
    stored = await desk.payments.get_actor(desk.admin, "default", "alice")
    assert rotated == stored and stored.api_key_id != provisioned.api_key_id
    assert (await desk.payments.credential(alice)).token == manager.keys[stored.api_key_id][1]
    assert list(vault.values) == [stored.secret_ref.ref]

    assert (await desk.revoke()).status == "revoked"
    assert (await desk.revoke()).status == "revoked"
    assert vault.values == {}
    with pytest.raises(NotFound):
        await desk.pause()


@pytest.mark.parametrize("command", ["pause", "fund", "rotate"])
async def test_a_store_without_the_column_writes_gets_the_rules_from_the_engines_own_check(
    whole_row_desk: Desk, manager: FakeAllocationManager, vault: MemorySecretStore, command: str
) -> None:
    """The engine reads the actor again just before it saves, so a command that finished while this one
    was at the provider is seen. What stays open for such a store is the moment between that read and the
    save, which only a condition in the store's own write can close."""
    desk = whole_row_desk
    await desk.payments.provision(desk.admin, provision_request("0" if command == "fund" else "5"))
    during(manager, PROVIDER_CALL[command], desk.revoke)
    if command == "fund":
        assert (await desk.fund()).status == "revoked"
    else:
        with pytest.raises(NotFound, match="is not provisioned"):
            await getattr(desk, command)()
    assert (await desk.payments.get_actor(desk.admin, "default", "alice")).status == "revoked"
    assert vault.values == {}


async def test_a_store_without_the_column_writes_keeps_a_pause_through_a_fund(
    whole_row_desk: Desk, manager: FakeAllocationManager
) -> None:
    desk = whole_row_desk
    await desk.payments.provision(desk.admin, provision_request("0"))
    during(manager, "fund_allocation", desk.pause)
    assert (await desk.fund()).status == "paused"


async def test_a_store_without_the_column_writes_keeps_a_rotation_that_finished_meanwhile(
    whole_row_desk: Desk, alice: ActorContext, manager: FakeAllocationManager, vault: MemorySecretStore
) -> None:
    desk = whole_row_desk
    await desk.payments.provision(desk.admin, provision_request())
    finished: list[ProvisionedActor] = []

    async def other() -> None:
        finished.append(await rotate_2(desk))

    during(manager, "create_key", other)
    with pytest.raises(InvalidRequest, match="another key rotation"):
        await desk.rotate()
    assert await holds_only_the_key_of(desk, alice, manager, vault) == finished[0]


@pytest.mark.parametrize("then", ["revoke", "pause"])
async def test_a_store_without_the_column_writes_keeps_what_finished_during_a_provisioning(
    whole_row_desk: Desk, manager: FakeAllocationManager, vault: MemorySecretStore, then: str
) -> None:
    desk = whole_row_desk
    await overtaken_provisioning(desk, manager, then)
    answer = await desk.payments.provision(desk.admin, provision_b())

    stored = await desk.payments.get_actor(desk.admin, "default", "alice")
    assert stored.status == ("revoked" if then == "revoke" else "paused") and answer == stored
    assert list(vault.values) == ([] if then == "revoke" else [stored.secret_ref.ref])
