"""Allocation handoff: secrets stay at the work engine, and stale commands publish no key.

Custody (D85 point 1, D87 point 1, #186 review): who holds an actor's credential is recorded beside its
reference, never read from the reference's text. A delivered receipt never reaches the configured store,
and a store-held secret is deleted on the store that issued it. A delivering answer carries the key or
says plainly that it does not (``replayed``).
"""

import ast
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import select, update

from livepeer_builder import EngineSupply
from livepeer_builder.contracts import (
    OPERATION_REPLAYED,
    OPERATION_RESUMED,
    ActorAllocation,
    ActorContext,
    AllotRequest,
    DeliveredAllocation,
    SecretRef,
    SeenActor,
)
from livepeer_builder.docs_gen import render_openapi
from livepeer_builder.errors import AccessDenied, NotFound, NotProvisioned, OperationConflict
from livepeer_builder.http.app import create_app, shared_app
from livepeer_builder.http.clearinghouse import mount_clearinghouse_routes
from livepeer_builder.runtime import BuilderEngine
from livepeer_builder.services import payments
from livepeer_builder.store import SqlStore
from livepeer_builder.store import schema as t
from livepeer_builder.testing import FakeAllocationManager, FixedClock, MemorySecretStore
from tests.conftest import EngineFactory
from tests.unit.test_payments import allot_request

ALLOCATION = "/v1/clearinghouse/tenants/default/actors/alice/allocation"
STALE_S = 120  # LPB_OPERATION_STALE_S: a run that never finished is resumed after it


def signalled(answer: DeliveredAllocation) -> DeliveredAllocation:
    """Every delivering answer, in every test here: no key exactly when it says ``replayed``."""
    assert (answer.allocation_api_key is None) is answer.replayed, answer
    return answer


async def allot_delivered(
    engine: BuilderEngine, admin: ActorContext, request: AllotRequest
) -> DeliveredAllocation:
    return signalled(await engine.payments.allot_for_delivery(admin, request))


async def rotate_delivered(engine: BuilderEngine, admin: ActorContext, ref: str) -> DeliveredAllocation:
    return signalled(await engine.payments.rotate_for_delivery(admin, "default", "alice", ref))


def key_of(answer: DeliveredAllocation) -> str:
    assert answer.allocation_api_key is not None, "this answer carries no key"
    return answer.allocation_api_key.get_secret_value()


class StrictVault(MemorySecretStore):
    """A supplied store that refuses to delete a handle it never issued, as a real vault may."""

    def __init__(self) -> None:
        super().__init__()
        self.issued: set[str] = set()
        self.deleted: list[str] = []
        self.gets = 0
        self.puts = 0

    async def put(self, name: str, secret: str) -> SecretRef:
        self.puts += 1
        ref = await super().put(name, secret)
        self.issued.add(ref.ref)
        return ref

    async def get(self, ref: SecretRef) -> str:
        self.gets += 1
        return await super().get(ref)

    async def delete(self, ref: SecretRef) -> None:
        if ref.ref not in self.issued:
            raise ValueError("not a handle this vault issued")
        self.deleted.append(ref.ref)
        await super().delete(ref)


class DeliveredLookingVault(MemorySecretStore):
    """A supplied store whose opaque handles happen to begin with ``delivered:``: allowed by SecretRef."""

    async def put(self, name: str, secret: str) -> SecretRef:
        ref = SecretRef(ref=f"delivered:{name}")
        self.values[ref.ref] = secret
        return ref


def once_then[**P, R](
    original: Callable[P, Awaitable[R]], restore: Callable[[], None], *, apply: bool
) -> Callable[P, Awaitable[R]]:
    """A write that is lost once: before it applies, or (``apply``) after, with its outcome unknown."""

    async def lost(*args: P.args, **kwargs: P.kwargs) -> R:
        restore()
        if apply:
            await original(*args, **kwargs)
        raise ConnectionError("the database went away")

    return lost


def lose_once(monkeypatch: pytest.MonkeyPatch, target: object, name: str, *, apply: bool = False) -> None:
    original = getattr(target, name)
    monkeypatch.setattr(
        target, name, once_then(original, lambda: monkeypatch.setattr(target, name, original), apply=apply)
    )


# -- the handoff ------------------------------------------------------------------------------------------


async def test_delivery_never_persists_secret_and_replay_returns_none(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, store: SqlStore
) -> None:
    vault = MemorySecretStore()
    engine = await make_engine(supply=EngineSupply(secret_store=vault))
    result = await allot_delivered(engine, admin, allot_request())
    token = key_of(result)
    assert token.startswith("lpg_") and not result.replayed and not result.resumed
    assert result.credential_custody == "work_engine"
    with pytest.raises(NotFound, match="secret"):
        await vault.get(result.secret_ref)
    async with store.engine.connect() as conn:
        for table in t.metadata.sorted_tables:
            assert token not in repr((await conn.execute(select(table))).all())
    with pytest.raises(NotProvisioned, match="work engine"):
        await engine.payments.credential(alice)
    again = await allot_delivered(engine, admin, allot_request())
    assert again.allocation_api_key is None and again.replayed and not again.resumed  # a replay, not a resume
    rotated = await rotate_delivered(engine, admin, "rotate")
    assert key_of(rotated) != token
    assert (await rotate_delivered(engine, admin, "rotate")).replayed
    with pytest.raises(AccessDenied):
        await engine.payments.allot_for_delivery(alice, allot_request("bob"))


async def test_the_delivered_key_is_masked_everywhere_but_the_http_answer(
    make_engine: EngineFactory, admin: ActorContext
) -> None:
    engine = await make_engine()
    result = await allot_delivered(engine, admin, allot_request())
    token = key_of(result)
    for shown in (repr(result), str(result), str(result.model_dump()), result.model_dump_json()):
        assert token not in shown
    assert "**********" in result.model_dump_json()


async def test_allotment_records_who_holds_the_credential(
    make_engine: EngineFactory, admin: ActorContext
) -> None:
    engine = await make_engine()
    await allot_delivered(engine, admin, allot_request())
    await engine.payments.allot(admin, allot_request("bob"))
    assert (await engine.payments.get_actor(admin, "default", "alice")).credential_custody == "work_engine"
    assert (await engine.payments.get_actor(admin, "default", "bob")).credential_custody == "secret_store"


async def test_overlapping_deliveries_each_get_only_their_own_key_and_touch_no_store(
    make_engine: EngineFactory,
    admin: ActorContext,
    manager: FakeAllocationManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No service copy, no swapped store: each call has its own buffer, and the configured store sees no
    secret at all. Bob's delivery runs whole while Alice's is at the provider, so the two overlap without
    two writers on the shared in-memory test database, which does not isolate concurrent transactions."""
    vault = StrictVault()
    engine = await make_engine(supply=EngineSupply(secret_store=vault))
    original = manager.create_key_for_grant
    overlapped: list[DeliveredAllocation] = []

    async def bob_meanwhile(*args: Any) -> Any:
        monkeypatch.setattr(manager, "create_key_for_grant", original)
        overlapped.append(await allot_delivered(engine, admin, allot_request("bob")))
        return await original(*args)

    monkeypatch.setattr(manager, "create_key_for_grant", bob_meanwhile)
    alice = await allot_delivered(engine, admin, allot_request())
    [bob] = overlapped
    assert key_of(alice) == manager.keys[alice.api_key_id][1]
    assert key_of(bob) == manager.keys[bob.api_key_id][1]
    assert key_of(alice) != key_of(bob)
    assert vault.puts == 0 and vault.issued == set()


def test_the_payments_service_is_never_copied_for_a_delivery() -> None:
    """The delivery store is passed to the steps, never swapped onto a copy of the service: a field added
    later that reaches the configured store would bypass such a swap without notice (#186 review)."""
    tree = ast.parse(Path(payments.__file__).read_text())
    imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    imported |= {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    defined = {
        node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    assert "copy" not in imported and "_delivery" not in defined, (
        "pass a PaymentKeys built for the delivery to _allot_with / _rotate_with instead"
    )


# -- custody, not the reference's text ---------------------------------------------------------------------


async def test_a_custom_stores_delivered_looking_handle_still_resolves(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, manager: FakeAllocationManager
) -> None:
    engine = await make_engine(supply=EngineSupply(secret_store=DeliveredLookingVault()))
    allotted = await engine.payments.allot(admin, allot_request())
    assert allotted.secret_ref.ref.startswith("delivered:") and allotted.credential_custody == "secret_store"
    assert (await engine.payments.credential(alice)).token == manager.keys[allotted.api_key_id][1]


async def test_a_work_engines_credential_is_never_asked_of_the_store(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext
) -> None:
    vault = StrictVault()
    engine = await make_engine(supply=EngineSupply(secret_store=vault))
    await allot_delivered(engine, admin, allot_request())
    with pytest.raises(NotProvisioned, match="work engine"):
        await engine.payments.credential(alice)
    assert vault.gets == 0


async def test_rotating_a_stored_key_for_delivery_deletes_it_from_the_store(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext
) -> None:
    """The previous secret is deleted on the store that issued it, not on the request's buffer (#186
    review)."""
    vault = StrictVault()
    engine = await make_engine(supply=EngineSupply(secret_store=vault))
    before = await engine.payments.allot(admin, allot_request())
    assert list(vault.values) == [before.secret_ref.ref]
    rotated = await rotate_delivered(engine, admin, "rotate")
    assert key_of(rotated) and rotated.credential_custody == "work_engine"
    assert vault.values == {} and vault.deleted == [before.secret_ref.ref]
    assert rotated.secret_ref.ref not in vault.issued
    with pytest.raises(NotProvisioned, match="work engine"):
        await engine.payments.credential(alice)


async def test_rotating_a_delivered_key_sends_nothing_to_the_store(
    make_engine: EngineFactory, admin: ActorContext
) -> None:
    vault = StrictVault()
    engine = await make_engine(supply=EngineSupply(secret_store=vault))
    await allot_delivered(engine, admin, allot_request())
    assert key_of(await rotate_delivered(engine, admin, "rotate"))
    assert vault.deleted == [] and vault.puts == 0


# -- revoke -------------------------------------------------------------------------------------------


async def test_revoking_a_delivered_allocation_never_asks_the_store_to_delete(
    make_engine: EngineFactory, admin: ActorContext, manager: FakeAllocationManager
) -> None:
    """A store that refuses a handle it never issued cannot keep a revoked allocation listed as active
    (#186 review)."""
    vault = StrictVault()
    engine = await make_engine(supply=EngineSupply(secret_store=vault))
    delivered = await allot_delivered(engine, admin, allot_request())
    revoked = await engine.payments.revoke(admin, "default", "alice")
    assert revoked.status == "revoked" and vault.deleted == []
    assert manager.allocations[delivered.allocation_id].status == "revoked"
    assert await engine.payments.revoke(admin, "default", "alice") == revoked


async def test_a_delivery_rotation_during_a_revoke_leaves_its_receipt_out_of_the_store(
    make_engine: EngineFactory,
    admin: ActorContext,
    manager: FakeAllocationManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The revoke read a stored key; a rotation for delivery lands before its write. The first delete goes
    to the store (the row it read), the follow-up does not (the row it wrote holds a receipt)."""
    vault = StrictVault()
    engine = await make_engine(supply=EngineSupply(secret_store=vault))
    before = await engine.payments.allot(admin, allot_request())
    original = manager.revoke_allocation

    async def rotated_first(allocation_id: str) -> None:
        monkeypatch.setattr(manager, "revoke_allocation", original)
        await rotate_delivered(engine, admin, "rotate-mid")
        await original(allocation_id)

    monkeypatch.setattr(manager, "revoke_allocation", rotated_first)
    revoked = await engine.payments.revoke(admin, "default", "alice")
    assert revoked.status == "revoked" and revoked.credential_custody == "work_engine"
    assert vault.deleted == [before.secret_ref.ref, before.secret_ref.ref]  # the rotation's, the revoke's
    assert vault.values == {}


async def test_a_stored_rotation_during_a_revoke_of_a_delivered_key_is_deleted(
    make_engine: EngineFactory,
    admin: ActorContext,
    manager: FakeAllocationManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reverse: the revoke read a receipt, so its first delete is skipped; an admin rotation stored a
    secret meanwhile, and the follow-up delete removes it from the store."""
    vault = StrictVault()
    engine = await make_engine(supply=EngineSupply(secret_store=vault))
    await allot_delivered(engine, admin, allot_request())
    original = manager.revoke_allocation
    stored: list[str] = []

    async def rotated_first(allocation_id: str) -> None:
        monkeypatch.setattr(manager, "revoke_allocation", original)
        stored.append(
            (await engine.payments.rotate_payment_key(admin, "default", "alice", "rotate-mid")).secret_ref.ref
        )
        await original(allocation_id)

    monkeypatch.setattr(manager, "revoke_allocation", rotated_first)
    revoked = await engine.payments.revoke(admin, "default", "alice")
    assert revoked.status == "revoked" and revoked.credential_custody == "secret_store"
    assert vault.deleted == stored and vault.values == {}


async def test_a_store_that_fails_to_delete_keeps_the_revoke_repeatable(
    make_engine: EngineFactory, admin: ActorContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stored secret is still deleted before the actor is marked revoked (D61): a store that is down
    leaves the actor active, and the repeat deletes the secret and revokes."""
    vault = StrictVault()
    engine = await make_engine(supply=EngineSupply(secret_store=vault))
    await engine.payments.allot(admin, allot_request())
    delete = vault.delete

    async def down(ref: SecretRef) -> None:
        monkeypatch.setattr(vault, "delete", delete)
        raise ConnectionError("the vault is down")

    monkeypatch.setattr(vault, "delete", down)
    with pytest.raises(ConnectionError):
        await engine.payments.revoke(admin, "default", "alice")
    assert (await engine.payments.get_actor(admin, "default", "alice")).status == "active"
    assert (await engine.payments.revoke(admin, "default", "alice")).status == "revoked"
    assert vault.values == {}


# -- resumed and overtaken deliveries -------------------------------------------------------------------


# Where a delivering command's first run dies, and whether the write it dies in had applied (D87 point 1,
# #186 review). Each resume must answer with the key Batteries gave the first run.
RESUMES = [
    pytest.param("allot", "store", "allot_actor", False, id="allot-before-the-actor-row"),
    pytest.param("allot", "store", "allot_actor", True, id="allot-the-actor-row-written-then-lost"),
    pytest.param("allot", "store", "complete_operation", False, id="allot-before-the-record-completes"),
    pytest.param("rotate", "store", "set_actor_key", False, id="rotate-before-the-new-key-is-written"),
    pytest.param("rotate", "store", "set_actor_key", True, id="rotate-the-new-key-written-then-lost"),
    pytest.param("rotate", "manager", "revoke_key", True, id="rotate-after-the-old-key-is-revoked"),
    pytest.param("rotate", "store", "complete_operation", False, id="rotate-before-the-record-completes"),
]


def provider_answers(manager: FakeAllocationManager, monkeypatch: pytest.MonkeyPatch, call: str) -> list[str]:
    """The secret of every key the provider answers ``call`` with, in order: what each run was given."""
    given: list[str] = []
    original = getattr(manager, call)

    async def recorded(*args: Any) -> Any:
        key = await original(*args)
        given.append(key.secret.get_secret_value())
        return key

    monkeypatch.setattr(manager, call, recorded)
    return given


@pytest.mark.parametrize(("command", "target", "write", "applied"), RESUMES)
async def test_a_resumed_delivery_hands_over_the_key_the_first_run_was_given(
    make_engine: EngineFactory,
    admin: ActorContext,
    alice: ActorContext,
    manager: FakeAllocationManager,
    clock: FixedClock,
    store: SqlStore,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    target: str,
    write: str,
    applied: bool,
) -> None:
    """The first run dies and its answer is lost; a repeat after the stale window resumes it. Batteries
    replays a matching call's success, the secret included, and never expires it, so the resume always has
    the key and hands it over, marked ``resumed``. Nothing is allotted, funded or minted twice."""
    engine = await make_engine()
    unallocated = manager.unallocated("default")
    before = await allot_delivered(engine, admin, allot_request()) if command == "rotate" else None
    call = "create_key" if command == "rotate" else "create_key_for_grant"
    given = provider_answers(manager, monkeypatch, call)

    async def run() -> DeliveredAllocation:
        if command == "rotate":
            return await engine.payments.rotate_for_delivery(admin, "default", "alice", "rotate")
        return await engine.payments.allot_for_delivery(admin, allot_request())

    lose_once(monkeypatch, store if target == "store" else manager, write, apply=applied)
    with pytest.raises(ConnectionError):
        await run()
    clock.advance(STALE_S)
    resumed = signalled(await run())

    assert given and key_of(resumed) == given[0] and given[0].startswith("lpg_")
    assert set(given) == {given[0]}  # every provider answer was the same key, replayed
    assert resumed.resumed and not resumed.replayed
    assert len(manager.keys) == (2 if command == "rotate" else 1) and len(manager.allocations) == 1
    assert manager.unallocated("default") == unallocated - 5  # funded once
    stored = await engine.payments.get_actor(admin, "default", "alice")
    assert (stored.api_key_id, stored.secret_ref) == (resumed.api_key_id, resumed.secret_ref)
    if before is not None:
        assert resumed.api_key_id != before.api_key_id and manager.keys[before.api_key_id][2] is True
    with pytest.raises(NotProvisioned, match="work engine"):
        await engine.payments.credential(alice)  # the work engine holds it, as on a first delivery
    assert not (await allot_delivered(engine, admin, allot_request("bob"))).resumed  # each command resets it


@pytest.mark.parametrize("label", ["", "Alice, on trial"], ids=["named-by-her-sign-in", "labelled"])
@pytest.mark.parametrize(
    ("write", "applied"),
    [
        pytest.param("allot_actor", False, id="before-the-actor-row"),
        pytest.param("allot_actor", True, id="the-actor-row-written-then-lost"),
        pytest.param("complete_operation", False, id="before-the-record-completes"),
    ],
)
async def test_a_resumed_delivery_sends_the_provider_the_request_its_first_run_sent(
    make_engine: EngineFactory,
    admin: ActorContext,
    manager: FakeAllocationManager,
    clock: FixedClock,
    store: SqlStore,
    monkeypatch: pytest.MonkeyPatch,
    write: str,
    applied: bool,
    label: str,
) -> None:
    """Batteries replays a saved success only to the request it was first given for; any other under that
    idempotency key is its 409. The first run names the allocation after the person's sign-in name when
    the request gives no label, so a resume sends that name again wherever the first run died. Refused,
    the allotment could never finish, and nobody would hold its live key (#166 review)."""
    engine = await make_engine()
    await store.save_sighting(
        SeenActor(
            tenant_id="default",
            actor_id="alice",
            name="Alice Example",
            first_seen_at=clock.now(),
            last_seen_at=clock.now(),
        )
    )
    request = allot_request(label=label)
    given = provider_answers(manager, monkeypatch, "create_key_for_grant")
    lose_once(monkeypatch, store, write, apply=applied)
    with pytest.raises(ConnectionError):
        await engine.payments.allot_for_delivery(admin, request)
    clock.advance(STALE_S)
    resumed = await allot_delivered(engine, admin, request)

    assert resumed.resumed and resumed.label == (label or "Alice Example")
    assert key_of(resumed) == given[0] and given[0].startswith("lpg_")
    assert given == [given[0]] * 2  # the provider was asked again, and replayed the first run's key
    assert len(manager.keys) == 1 and len(manager.allocations) == 1  # nothing minted or allotted twice
    again = await allot_delivered(engine, admin, request)
    assert again.allocation_api_key is None and again.replayed and not again.resumed
    assert manager.calls.count("create_key_for_grant") == 2  # the repeat is answered from the record


@pytest.mark.parametrize("noted", [True, False], ids=["label-noted", "recorded-before-the-label-was-noted"])
async def test_a_delivery_resumed_after_its_row_was_saved_and_the_person_renamed_sends_the_first_name(
    make_engine: EngineFactory,
    admin: ActorContext,
    manager: FakeAllocationManager,
    clock: FixedClock,
    store: SqlStore,
    monkeypatch: pytest.MonkeyPatch,
    noted: bool,
) -> None:
    """The first run saved the actor and its answer was lost; the person then signs in under another
    name. The resume asks the provider to replay the key, and names the allocation as the first run did:
    by the label its record noted, or, for a record written before the label was noted, by the label
    the actor was saved with."""
    engine = await make_engine()

    async def sign_in(name: str) -> None:
        seen = SeenActor(
            tenant_id="default", actor_id="alice", name=name, first_seen_at=clock.now(), last_seen_at=clock.now()
        )  # fmt: skip
        await store.save_sighting(seen)

    await sign_in("Alice Example")
    given = provider_answers(manager, monkeypatch, "create_key_for_grant")
    lose_once(monkeypatch, store, "allot_actor", apply=True)
    with pytest.raises(ConnectionError):
        await engine.payments.allot_for_delivery(admin, allot_request())
    if not noted:
        await note_as_an_earlier_release(store, REFS["allot"], "sent_label")
    await sign_in("Alice E. Example")
    clock.advance(STALE_S)
    resumed = await allot_delivered(engine, admin, allot_request())
    assert resumed.resumed and resumed.label == "Alice Example"
    assert key_of(resumed) == given[0] and given == [given[0]] * 2  # replayed, so the same name was sent
    assert len(manager.keys) == 1 and len(manager.allocations) == 1


async def test_two_allotments_of_one_actor_both_carry_the_key_the_provider_gave_both(
    make_engine: EngineFactory,
    admin: ActorContext,
    manager: FakeAllocationManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Another reference wrote the actor while this one was at the provider. Both got the same
    actor-derived key, so both answers carry it; neither is a silent null (#186 review)."""
    engine = await make_engine()
    original = manager.create_key_for_grant
    other: list[DeliveredAllocation] = []

    async def overtaken(*args: Any) -> Any:
        monkeypatch.setattr(manager, "create_key_for_grant", original)
        request = allot_request().model_copy(update={"operation_ref": "allot-other"})
        other.append(await allot_delivered(engine, admin, request))
        return await original(*args)

    monkeypatch.setattr(manager, "create_key_for_grant", overtaken)
    first = await allot_delivered(engine, admin, allot_request())
    assert key_of(first) == key_of(other[0]) == manager.keys[first.api_key_id][1]
    assert not first.resumed and not other[0].resumed  # overtaken, not resumed
    assert first.secret_ref == other[0].secret_ref  # the receipt the actor holds


# -- one kind of command resumes a record (Mike, 23 November 2026) -----------------------------------------
#
# The admin commands store the key; the delivering ones hand it to their caller. A run begun by one kind
# and finished by the other would hand its key to nobody, or leave it where no caller looks for it.

KINDS = {"admin": "secret_store", "delivering": "work_engine"}  # the kind of command, and who keeps its key
COMMANDS = {
    ("allot", "admin"): "allot",
    ("allot", "delivering"): "allot_for_delivery",
    ("rotate", "admin"): "rotate_payment_key",
    ("rotate", "delivering"): "rotate_for_delivery",
}
REFS = {"allot": "allot:default:alice", "rotate": "rotate"}
UNFINISHED = [  # where the first run dies, and whether the write it dies in had applied
    pytest.param("allot", "allot_actor", False, id="allot-before-the-actor-row"),
    pytest.param("allot", "allot_actor", True, id="allot-the-actor-row-written-then-lost"),
    pytest.param("rotate", "set_actor_key", False, id="rotate-before-the-new-key-is-written"),
    pytest.param("rotate", "set_actor_key", True, id="rotate-the-new-key-written-then-lost"),
]
BOTH_KINDS = pytest.mark.parametrize(
    ("began", "other"), [("delivering", "admin"), ("admin", "delivering")], ids=["delivering", "admin"]
)


async def run_as(
    engine: BuilderEngine, admin: ActorContext, command: str, kind: str
) -> ActorAllocation | DeliveredAllocation:
    """One allotment or rotation of Alice's key, by the admin command or by its delivering twin."""
    if command == "allot":
        if kind == "delivering":
            return await allot_delivered(engine, admin, allot_request())
        return await engine.payments.allot(admin, allot_request())
    if kind == "delivering":
        return await rotate_delivered(engine, admin, REFS["rotate"])
    return await engine.payments.rotate_payment_key(admin, "default", "alice", REFS["rotate"])


async def everything_written(store: SqlStore, vault: "StrictVault", command: str) -> tuple[object, ...]:
    """What a refused command must leave as it was: the ledger's record, the actor and the secret store."""
    record = await store.get_operation("default", "operator", REFS[command])
    actor = await store.get_actor("default", "alice")
    return record, actor, dict(vault.values), vault.puts, list(vault.deleted)


async def note_as_an_earlier_release(store: SqlStore, ref: str, *dropped: str) -> None:
    """Rewrite a record as a release that noted less wrote it: without ``dropped``."""
    async with store.engine.begin() as conn:
        rows = (await conn.execute(select(t.operations.c.operation_ref, t.operations.c.context))).all()
        [context] = [row[1] for row in rows if row[0] == ref]
        assert set(dropped) <= set(context), context
        old = {name: value for name, value in context.items() if name not in dropped}
        await conn.execute(
            update(t.operations).where(t.operations.c.operation_ref == ref).values(context=old)
        )


async def refused_then_resumed_by_its_own_kind(
    engine: BuilderEngine,
    admin: ActorContext,
    alice: ActorContext,
    manager: FakeAllocationManager,
    clock: FixedClock,
    store: SqlStore,
    vault: "StrictVault",
    given: list[str],
    command: str,
    began: str,
    other: str,
) -> None:
    """The other kind of command is refused, in the stale window and after it, with nothing sent to the
    provider and nothing written; the kind that began the record then resumes it and finishes."""
    calls, written = list(manager.calls), await everything_written(store, vault, command)
    for wait in (0, STALE_S):  # refused while the first run may still be going, and when it is stale
        clock.advance(wait)
        with pytest.raises(OperationConflict) as refused:
            await run_as(engine, admin, command, other)
        assert f"begun by {COMMANDS[command, began]}" in str(refused.value)
        assert f"repeat {COMMANDS[command, began]}" in str(refused.value)
        assert refused.value.operation is not None and refused.value.operation.state == "started"
        assert manager.calls == calls  # no provider call
        assert await everything_written(store, vault, command) == written  # and no write: still resumable

    resumed = await run_as(engine, admin, command, began)
    assert OPERATION_RESUMED.get() and resumed.credential_custody == KINDS[began]
    if isinstance(resumed, DeliveredAllocation):
        assert key_of(resumed) == given[0] and given[0].startswith("lpg_")  # the first run's key
        assert resumed.resumed and not resumed.replayed and vault.values == {}
        with pytest.raises(NotProvisioned, match="work engine"):
            await engine.payments.credential(alice)
    else:
        assert (await engine.payments.credential(alice)).token == given[0]
        assert list(vault.values) == [resumed.secret_ref.ref]
    assert set(given) == {given[0]}  # every provider answer was the same key, replayed
    assert len(manager.allocations) == 1 and len(manager.keys) == (2 if command == "rotate" else 1)
    stored = await engine.payments.get_actor(admin, "default", "alice")
    assert (stored.api_key_id, stored.secret_ref) == (resumed.api_key_id, resumed.secret_ref)


@BOTH_KINDS
@pytest.mark.parametrize(("command", "write", "applied"), UNFINISHED)
async def test_an_unfinished_command_is_resumed_only_by_the_kind_of_command_that_began_it(
    make_engine: EngineFactory,
    admin: ActorContext,
    alice: ActorContext,
    manager: FakeAllocationManager,
    clock: FixedClock,
    store: SqlStore,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    write: str,
    applied: bool,
    began: str,
    other: str,
) -> None:
    """A rotation begun by ``rotate_for_delivery`` and finished by ``rotate_payment_key`` revoked the old
    key and handed the new one to nobody; the reverse kept the key in the store and answered a delivering
    caller with none. The two allotment commands shared a record the same way. Now the record notes which
    kind began it, and the other kind is refused before any provider call and before any write."""
    vault = StrictVault()
    engine = await make_engine(supply=EngineSupply(secret_store=vault))
    if command == "rotate":
        await run_as(engine, admin, "allot", began)
    given = provider_answers(
        manager, monkeypatch, "create_key" if command == "rotate" else "create_key_for_grant"
    )
    lose_once(monkeypatch, store, write, apply=applied)
    with pytest.raises(ConnectionError):
        await run_as(engine, admin, command, began)
    await refused_then_resumed_by_its_own_kind(
        engine, admin, alice, manager, clock, store, vault, given, command, began, other
    )


@BOTH_KINDS
@pytest.mark.parametrize("command", ["allot", "rotate"])
async def test_a_record_that_notes_only_its_secrets_custody_is_the_kind_that_custody_implies(
    make_engine: EngineFactory,
    admin: ActorContext,
    alice: ActorContext,
    manager: FakeAllocationManager,
    clock: FixedClock,
    store: SqlStore,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    began: str,
    other: str,
) -> None:
    """A record written before the kind was noted: a secret the work engine holds was stored by a
    delivering command, and one the store holds by an admin command."""
    vault = StrictVault()
    engine = await make_engine(supply=EngineSupply(secret_store=vault))
    if command == "rotate":
        await run_as(engine, admin, "allot", began)
    given = provider_answers(
        manager, monkeypatch, "create_key" if command == "rotate" else "create_key_for_grant"
    )
    lose_once(monkeypatch, store, "set_actor_key" if command == "rotate" else "allot_actor")
    with pytest.raises(ConnectionError):
        await run_as(engine, admin, command, began)
    await note_as_an_earlier_release(store, REFS[command], "begun_for")
    record = await store.get_operation("default", "operator", REFS[command])
    assert record is not None and record.context["new_secret_custody"] == KINDS[began]
    await refused_then_resumed_by_its_own_kind(
        engine, admin, alice, manager, clock, store, vault, given, command, began, other
    )


@pytest.mark.parametrize("command", ["allot", "rotate"])
async def test_a_record_that_notes_neither_kind_nor_custody_is_the_admin_commands(
    make_engine: EngineFactory,
    admin: ActorContext,
    alice: ActorContext,
    manager: FakeAllocationManager,
    clock: FixedClock,
    store: SqlStore,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    """A record an earlier release wrote names no kind and no custody. Only the admin commands existed
    then, and every secret was the configured store's: the delivering command is refused, and the admin
    command resumes it as the store's."""
    vault = StrictVault()
    engine = await make_engine(supply=EngineSupply(secret_store=vault))
    custodies = ["new_secret_custody"]
    if command == "rotate":
        await run_as(engine, admin, "allot", "admin")
        custodies.append("previous_credential_custody")
    given = provider_answers(
        manager, monkeypatch, "create_key" if command == "rotate" else "create_key_for_grant"
    )
    lose_once(monkeypatch, store, "set_actor_key" if command == "rotate" else "allot_actor")
    with pytest.raises(ConnectionError):
        await run_as(engine, admin, command, "admin")
    await note_as_an_earlier_release(store, REFS[command], "begun_for", *custodies)
    await refused_then_resumed_by_its_own_kind(
        engine, admin, alice, manager, clock, store, vault, given, command, "admin", "delivering"
    )


@BOTH_KINDS
@pytest.mark.parametrize("command", ["allot", "rotate"])
async def test_a_finished_command_repeated_by_the_other_kind_is_answered_from_the_ledger(
    make_engine: EngineFactory,
    admin: ActorContext,
    manager: FakeAllocationManager,
    command: str,
    began: str,
    other: str,
) -> None:
    """A repeat of a finished command is not a resume: either kind is answered from the record, the
    delivering one with no key and ``replayed``."""
    vault = StrictVault()
    engine = await make_engine(supply=EngineSupply(secret_store=vault))
    if command == "rotate":
        await run_as(engine, admin, "allot", began)
    first = await run_as(engine, admin, command, began)
    calls, values, deleted = list(manager.calls), dict(vault.values), list(vault.deleted)
    again = await run_as(engine, admin, command, other)
    assert OPERATION_REPLAYED.get() and not OPERATION_RESUMED.get()
    assert manager.calls == calls and vault.values == values and vault.deleted == deleted
    assert again.credential_custody == KINDS[began]
    if isinstance(again, DeliveredAllocation):
        assert again.allocation_api_key is None and again.replayed and not again.resumed
        assert ActorAllocation(**again.model_dump(include=set(ActorAllocation.model_fields))) == first
    else:
        assert isinstance(first, DeliveredAllocation) and key_of(first)
        assert again == ActorAllocation(**first.model_dump(include=set(ActorAllocation.model_fields)))


@BOTH_KINDS
async def test_two_references_of_two_kinds_that_meet_at_the_provider_leave_the_key_with_the_rows_writer(
    make_engine: EngineFactory,
    admin: ActorContext,
    alice: ActorContext,
    manager: FakeAllocationManager,
    monkeypatch: pytest.MonkeyPatch,
    began: str,
    other: str,
) -> None:
    """Two references are two commands, so neither resumes the other: both get the actor-derived key from
    the provider, and the one that wrote the row keeps it. A delivering writer's caller is handed it; an
    admin writer's store holds it, and the delivering reference that did not write answers with no key
    and says so. The key is never handed to nobody."""
    vault = StrictVault()
    engine = await make_engine(supply=EngineSupply(secret_store=vault))
    original = manager.create_key_for_grant
    wrote: list[ActorAllocation | DeliveredAllocation] = []

    async def overtaken(*args: Any) -> Any:
        monkeypatch.setattr(manager, "create_key_for_grant", original)
        request = allot_request().model_copy(update={"operation_ref": "allot-other"})
        if other == "delivering":
            wrote.append(await allot_delivered(engine, admin, request))
        else:
            wrote.append(await engine.payments.allot(admin, request))
        return await original(*args)

    monkeypatch.setattr(manager, "create_key_for_grant", overtaken)
    late = await run_as(engine, admin, "allot", began)
    [writer] = wrote
    assert len(manager.keys) == 1 and len(manager.allocations) == 1
    secret = manager.keys[writer.api_key_id][1]
    assert late.credential_custody == writer.credential_custody == KINDS[other]
    if isinstance(
        writer, DeliveredAllocation
    ):  # the writer's caller holds it; the store never saw a copy left
        assert key_of(writer) == secret and vault.values == {}
        with pytest.raises(NotProvisioned, match="work engine"):
            await engine.payments.credential(alice)
    else:  # the store holds it, and the delivering reference says it carries none
        assert isinstance(late, DeliveredAllocation) and late.allocation_api_key is None and late.replayed
        assert (await engine.payments.credential(alice)).token == secret
        assert list(vault.values) == [writer.secret_ref.ref]


async def test_delayed_delivery_cannot_replace_reallotted_key(
    make_engine: EngineFactory, admin: ActorContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = await make_engine()
    original = payments.delivered
    replacement = []

    async def after_reallot(store, allocation, secrets):  # type: ignore[no-untyped-def]
        monkeypatch.setattr(payments, "delivered", original)
        await engine.payments.revoke(admin, "default", "alice")
        request = allot_request().model_copy(update={"operation_ref": "allot-again"})
        replacement.append(await allot_delivered(engine, admin, request))
        return await original(store, allocation, secrets)

    monkeypatch.setattr(payments, "delivered", after_reallot)
    old = await allot_delivered(engine, admin, allot_request())
    assert old.allocation_api_key is None and old.replayed
    new = replacement[0]
    assert key_of(new)
    current = await engine.payments.get_actor(admin, "default", "alice")
    assert current.api_key_id == new.api_key_id
    assert current.allocation_id != old.allocation_id


async def test_rotation_overtaken_by_revoke_returns_no_credential(
    make_engine: EngineFactory, admin: ActorContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = await make_engine()
    await allot_delivered(engine, admin, allot_request())
    original = payments.delivered

    async def after_revoke(store, allocation, secrets):  # type: ignore[no-untyped-def]
        await engine.payments.revoke(admin, "default", "alice")
        return await original(store, allocation, secrets)

    monkeypatch.setattr(payments, "delivered", after_revoke)
    rotated = await rotate_delivered(engine, admin, "rotate-revoke")
    assert rotated.allocation_api_key is None and rotated.replayed
    assert (await engine.payments.get_actor(admin, "default", "alice")).status == "revoked"


async def test_another_tenants_admin_cannot_receive_credentials(make_engine: EngineFactory) -> None:
    engine = await make_engine()
    other = ActorContext(tenant_id="other", actor_id="operator", credential_kind="tenant_admin")
    with pytest.raises(AccessDenied):
        await engine.payments.allot_for_delivery(other, allot_request())
    with pytest.raises(AccessDenied):
        await engine.payments.rotate_for_delivery(other, "default", "alice", "rotate")


async def test_failed_delivery_clears_request_buffer(
    make_engine: EngineFactory, admin: ActorContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = await make_engine()
    held = []

    async def failed(store, allocation, secrets):  # type: ignore[no-untyped-def]
        held.append((secrets, allocation.secret_ref))
        raise RuntimeError("response interrupted")

    monkeypatch.setattr(payments, "delivered", failed)
    with pytest.raises(RuntimeError, match="interrupted"):
        await engine.payments.allot_for_delivery(admin, allot_request())
    secrets, ref = held[0]
    assert secrets.token(ref) is None


# -- HTTP and the published spec ------------------------------------------------------------------------


async def test_clearinghouse_routes_deliver_and_expose_no_signer_proxy(make_engine: EngineFactory) -> None:
    engine = await make_engine()
    assert engine.keys is not None
    key = await engine.keys.bootstrap_admin()
    app = shared_app(engine, manage_lifecycle=False)
    mount_clearinghouse_routes(app, engine, deliver_allocation_keys=True)
    headers = {"Authorization": f"Bearer {key.token}"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://engine") as client:
        body = {"amount": {"amount": "5", "currency": "USD"}, "operation_ref": "first"}
        response = await client.post(ALLOCATION, json=body, headers=headers)
        assert response.status_code == 201, response.text
        answer = response.json()
        assert answer["allocation_api_key"].startswith("lpg_") and answer["replayed"] is False
        assert answer["resumed"] is False
        assert answer["credential_custody"] == "work_engine"
        assert response.headers["cache-control"] == "no-store"
        assert "allocation_api_key" not in (await client.get(ALLOCATION, headers=headers)).json()
        repeat = await client.post(ALLOCATION, json=body, headers=headers)
        assert repeat.json()["allocation_api_key"] is None and repeat.json()["replayed"] is True
        assert repeat.headers["idempotent-replayed"] == "true"
        rotate = {"operation_ref": "rotate-1"}
        rotated = await client.post(ALLOCATION + "/rotate-payment-key", json=rotate, headers=headers)
        assert rotated.status_code == 200, rotated.text
        assert rotated.json()["allocation_api_key"].startswith("lpg_") and rotated.json()["replayed"] is False
        assert rotated.json()["allocation_api_key"] != answer["allocation_api_key"]
        assert rotated.headers["cache-control"] == "no-store"
        for route in ["/sign-orchestrator-info", "/generate-live-payment", "/v1/clearinghouse/payments"]:
            assert (await client.post(route, headers=headers)).status_code == 404


async def test_a_resumed_delivery_is_marked_in_the_http_answer(
    make_engine: EngineFactory, clock: FixedClock, store: SqlStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = await make_engine()
    assert engine.keys is not None
    key = await engine.keys.bootstrap_admin()
    app = shared_app(engine, manage_lifecycle=False)
    mount_clearinghouse_routes(app, engine, deliver_allocation_keys=True)
    headers = {"Authorization": f"Bearer {key.token}"}
    body = {"amount": {"amount": "5", "currency": "USD"}, "operation_ref": "first"}
    lose_once(monkeypatch, store, "allot_actor")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://engine") as client:
        with pytest.raises(ConnectionError):
            await client.post(ALLOCATION, json=body, headers=headers)
        clock.advance(STALE_S)
        response = await client.post(ALLOCATION, json=body, headers=headers)
    assert response.status_code == 201, response.text
    answer = response.json()
    assert answer["resumed"] is True and answer["replayed"] is False
    assert answer["allocation_api_key"].startswith("lpg_")
    assert "idempotent-replayed" not in response.headers  # resumed, not answered from the record


async def test_the_other_kind_of_route_cannot_resume_a_delivery_and_answers_409(
    make_engine: EngineFactory,
    manager: FakeAllocationManager,
    clock: FixedClock,
    store: SqlStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two apps over one engine, one of each kind: the storing route is refused as a conflict, and the
    delivering route that began the allotment still resumes it and hands the key over."""
    engine = await make_engine()
    assert engine.keys is not None
    key = await engine.keys.bootstrap_admin()
    delivering = shared_app(engine, manage_lifecycle=False)
    mount_clearinghouse_routes(delivering, engine, deliver_allocation_keys=True)
    storing = create_app(engine, manage_lifecycle=False)
    headers = {"Authorization": f"Bearer {key.token}"}
    body = {"amount": {"amount": "5", "currency": "USD"}, "operation_ref": "first"}
    lose_once(monkeypatch, store, "allot_actor")
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=delivering), base_url="http://engine") as first,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=storing), base_url="http://engine") as other,
    ):
        with pytest.raises(ConnectionError):
            await first.post(ALLOCATION, json=body, headers=headers)
        clock.advance(STALE_S)
        refused = await other.post(ALLOCATION, json=body, headers=headers)
        assert refused.status_code == 409, refused.text
        assert refused.json()["code"] == "operation_conflict"
        assert refused.json()["operation"] == {
            "command": "allot",
            "operation_ref": "first",
            "state": "started",
        }
        assert "begun by allot_for_delivery" in refused.json()["error"]
        assert manager.calls == ["create_key_for_grant"]  # the refused route sent nothing
        resumed = await first.post(ALLOCATION, json=body, headers=headers)
    assert resumed.status_code == 201, resumed.text
    assert resumed.json()["resumed"] is True and resumed.json()["allocation_api_key"].startswith("lpg_")


async def test_the_shipped_app_keeps_the_key_in_its_own_store(make_engine: EngineFactory) -> None:
    """``lpb serve`` (``create_app``) is the combined engine: its own work half holds the key, so its
    allotment answers the allocation alone. Handing keys over is the clearinghouse role's, which #169
    mounts."""
    engine = await make_engine()
    assert engine.keys is not None
    key = await engine.keys.bootstrap_admin()
    app = create_app(engine, manage_lifecycle=False)
    headers = {"Authorization": f"Bearer {key.token}"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://engine") as client:
        body = {"amount": {"amount": "5", "currency": "USD"}, "operation_ref": "first"}
        answer = (await client.post(ALLOCATION, json=body, headers=headers)).json()
    assert "allocation_api_key" not in answer and answer["credential_custody"] == "secret_store"


async def test_the_published_spec_documents_the_shipped_app() -> None:
    """The committed spec is ``create_app``'s (#186 review), so it never promises a key the
    served routes do not return."""
    rendered = await render_openapi()
    committed = Path(__file__).parents[2] / "docs" / "generated" / "openapi.json"
    assert committed.read_text() == rendered, "run `uv run lpb gen-docs`"
    spec = json.loads(rendered)
    allot = spec["paths"]["/v1/clearinghouse/tenants/{tenant_id}/actors/{actor_id}/allocation"]["post"]
    schema = allot["responses"]["201"]["content"]["application/json"]["schema"]
    assert schema == {"$ref": "#/components/schemas/ActorAllocation"}
    assert "DeliveredAllocationBody" not in spec["components"]["schemas"]
