"""#158: a resume that is handed another key than its first run stored drops the copy it replaces.

A command that mints a key stores its secret and notes the reference in its record, so a resume reuses
that copy. A resume can be handed another key: an allotment resumed after its actor was revoked allots
again, and a command resumed under another ``LPB_ENGINE_ID`` sends another provider call. It stored the new
key's secret and noted that one in place of the first, and the first copy stayed in the secret store with
no record naming it.

The resume now notes the copy it replaces in the same write that notes the new one, and deletes it after
that write: the record never names a secret that is gone, and a repeat finishes a delete that failed. It
never deletes the reference the actor's row holds, or the one it has just stored.

A delete that failed leaves the record naming two copies. A resume that is handed yet another key first
deletes the replaced copy that is still noted, and only then stores and notes: so every stored secret of
the command is named by its record or by the actor's row, however often the key changes.
"""

from typing import Any

import pytest
from sqlalchemy import select

from livepeer_builder import BuilderEngine, EngineSupply
from livepeer_builder.contracts import ActorContext, SecretRef
from livepeer_builder.errors import InvalidRequest, OperationInProgress, ProviderUnavailable
from livepeer_builder.store import SqlStore
from livepeer_builder.store import schema as t
from livepeer_builder.testing import FakeAllocationManager, FixedClock, MemorySecretStore
from tests.conftest import EngineFactory
from tests.unit.test_admin_repeats import OnePathVault, allot_request

ANOTHER_ENGINE = "another-engine"  # LPB_ENGINE_ID after it was changed: every provider call is a new one


class Vault(MemorySecretStore):
    """A supplied store that records each put and each delete, and fails a delete when told to: the next
    one (``sealed``), or every one of a reference in ``stuck``."""

    def __init__(self) -> None:
        super().__init__()
        self.put_secrets: list[str] = []
        self.deleted: list[str] = []
        self.sealed = False
        self.stuck: set[str] = set()

    async def put(self, name: str, secret: str) -> SecretRef:
        self.put_secrets.append(secret)
        return await super().put(name, secret)

    async def delete(self, ref: SecretRef) -> None:
        if self.sealed or ref.ref in self.stuck:
            self.sealed = False
            raise ConnectionError("the vault is sealed")
        self.deleted.append(ref.ref)
        await super().delete(ref)


def loses_the_actor_write(store: SqlStore, monkeypatch: pytest.MonkeyPatch, write: str) -> None:
    """The next write of the actor (``allot_actor`` or ``set_actor_key``) fails: the run that makes it
    has stored its key's secret and noted it, and dies before the actor points at it."""
    original = getattr(store, write)

    async def lost(*_: object, **__: object) -> None:
        monkeypatch.setattr(store, write, original)
        raise ConnectionError("the database went away")

    monkeypatch.setattr(store, write, lost)


async def built_in_refs(store: SqlStore) -> set[str]:
    async with store.engine.connect() as conn:
        return set((await conn.execute(select(t.secrets.c.ref))).scalars())


async def noted(store: SqlStore, operation_ref: str) -> dict[str, Any]:
    """What the record of ``operation_ref`` notes."""
    held = t.operations.c.operation_ref == operation_ref
    async with store.engine.connect() as conn:
        return dict((await conn.execute(select(t.operations.c.context).where(held))).scalar_one())


def secrets_named(record: dict[str, Any]) -> dict[str, str]:
    """Every field of a record that names a stored secret, by its name without ``_secret_ref``."""
    return {name.removesuffix("_secret_ref"): ref for name, ref in record.items() if name.endswith("_ref")}


def live_keys(manager: FakeAllocationManager) -> list[str]:
    return [key_id for key_id, (_, _, revoked) in manager.keys.items() if not revoked]


# -- an allotment ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("handed_another_key", ["after a revoke", "under another LPB_ENGINE_ID"])
async def test_a_resumed_allotment_that_is_handed_another_key_drops_the_copy_its_first_run_stored(
    make_engine: EngineFactory,
    admin: ActorContext,
    alice: ActorContext,
    manager: FakeAllocationManager,
    clock: FixedClock,
    store: SqlStore,
    monkeypatch: pytest.MonkeyPatch,
    handed_another_key: str,
) -> None:
    """B stored its key's secret and died before saving the actor. Then the actor was allotted under A
    and revoked, so B's resume allots again; or the engine's id was changed, so B's resume sends another
    provider call. Either way it is handed a key its first run never saw, stores that key's secret and
    notes it. The first copy stayed stored, named by nothing: now the only secret kept is the actor's."""
    engine = await make_engine()
    loses_the_actor_write(store, monkeypatch, "allot_actor")
    with pytest.raises(ConnectionError):
        await engine.payments.allot(admin, allot_request(ref="p-b"))
    [first_copy] = await built_in_refs(store)
    if handed_another_key == "after a revoke":
        await engine.payments.allot(admin, allot_request(ref="p-a"))
        await engine.payments.revoke(admin, "default", "alice")
    else:
        engine = await make_engine(engine_id=ANOTHER_ENGINE)
    clock.advance(120)
    again = await engine.payments.allot(admin, allot_request(ref="p-b"))

    assert again.secret_ref.ref != first_copy
    assert await built_in_refs(store) == {again.secret_ref.ref}
    assert (await engine.payments.credential(alice)).token == manager.keys[again.api_key_id][1]


async def test_a_store_with_one_reference_for_an_actor_keeps_the_secret_it_has_just_stored(
    make_engine: EngineFactory,
    admin: ActorContext,
    alice: ActorContext,
    manager: FakeAllocationManager,
    clock: FixedClock,
    store: SqlStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A supplied store may keep an actor's secret at one path: the copy the resume replaces has the
    reference of the copy it stores. That reference names the new key's secret, and is never deleted."""
    vault = OnePathVault()
    engine = await make_engine(supply=EngineSupply(secret_store=vault))
    loses_the_actor_write(store, monkeypatch, "allot_actor")
    with pytest.raises(ConnectionError):
        await engine.payments.allot(admin, allot_request(ref="p-b"))
    await engine.payments.allot(admin, allot_request(ref="p-a"))
    await engine.payments.revoke(admin, "default", "alice")
    clock.advance(120)
    again = await engine.payments.allot(admin, allot_request(ref="p-b"))

    assert vault.values == {again.secret_ref.ref: manager.keys[again.api_key_id][1]}
    assert (await engine.payments.credential(alice)).token == manager.keys[again.api_key_id][1]


async def test_a_delete_that_fails_leaves_the_record_naming_both_copies_and_a_repeat_finishes_it(
    make_engine: EngineFactory,
    admin: ActorContext,
    alice: ActorContext,
    manager: FakeAllocationManager,
    clock: FixedClock,
    store: SqlStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The replaced copy is deleted after the record names the new one. A store that fails then answers
    the command with its own error: no actor is saved, and the record stays ``started`` with the new
    copy as its key's secret and the replaced one beside it. The repeat is handed the same key again,
    stores nothing more, deletes the replaced copy and saves the actor."""
    vault = Vault()
    engine = await make_engine(supply=EngineSupply(secret_store=vault))
    loses_the_actor_write(store, monkeypatch, "allot_actor")
    with pytest.raises(ConnectionError):
        await engine.payments.allot(admin, allot_request(ref="p-b"))
    [first_copy] = vault.values
    await engine.payments.allot(admin, allot_request(ref="p-a"))
    await engine.payments.revoke(admin, "default", "alice")
    clock.advance(120)
    vault.sealed = True
    with pytest.raises(ConnectionError, match="the vault is sealed"):
        await engine.payments.allot(admin, allot_request(ref="p-b"))

    record = await noted(store, "p-b")
    assert record["replaced_secret_ref"] == first_copy and record["new_secret_ref"] != first_copy
    assert set(vault.values) == {first_copy, record["new_secret_ref"]}
    assert (await engine.payments.get_actor(admin, "default", "alice")).status == "revoked"
    with pytest.raises(OperationInProgress):
        await engine.payments.allot(admin, allot_request(ref="p-b"))

    clock.advance(120)
    calls = len(manager.allocations)
    again = await engine.payments.allot(admin, allot_request(ref="p-b"))
    assert len(manager.allocations) == calls  # the provider replayed the key: nothing new was allotted
    assert again.secret_ref.ref == record["new_secret_ref"]
    assert set(vault.values) == {again.secret_ref.ref} and first_copy in vault.deleted
    assert (await engine.payments.credential(alice)).token == manager.keys[again.api_key_id][1]


async def cut_short_then_revoked(
    engine: BuilderEngine,
    admin: ActorContext,
    store: SqlStore,
    monkeypatch: pytest.MonkeyPatch,
    vault: MemorySecretStore,
) -> str:
    """``p-b`` stores copy A and dies before it saves the actor, which is then allotted under ``p-a`` and
    revoked: a resume of ``p-b`` will be handed another key. Answers A's reference."""
    loses_the_actor_write(store, monkeypatch, "allot_actor")
    with pytest.raises(ConnectionError):
        await engine.payments.allot(admin, allot_request(ref="p-b"))
    [first_copy] = vault.values
    await engine.payments.allot(admin, allot_request(ref="p-a"))
    await engine.payments.revoke(admin, "default", "alice")
    return first_copy


@pytest.mark.parametrize("handed_a_third_key", ["under another LPB_ENGINE_ID", "after another revoke"])
async def test_a_second_key_change_first_drops_the_replaced_copy_whose_delete_had_failed(
    make_engine: EngineFactory,
    admin: ActorContext,
    alice: ActorContext,
    manager: FakeAllocationManager,
    clock: FixedClock,
    store: SqlStore,
    monkeypatch: pytest.MonkeyPatch,
    handed_a_third_key: str,
) -> None:
    """The resume stored B over A, and the delete of A failed: the record names B as its key's secret
    and A as the copy it replaced. The next resume is handed a third key, because the engine's id was
    changed or because the actor was allotted and revoked once more. It noted B as replaced in A's
    place and deleted B, and A stayed stored with no field of the record naming it. It now deletes A
    before it writes the note that stops naming it."""
    vault = Vault()
    engine = await make_engine(supply=EngineSupply(secret_store=vault))
    copy_a = await cut_short_then_revoked(engine, admin, store, monkeypatch, vault)
    clock.advance(120)
    vault.sealed = True
    with pytest.raises(ConnectionError):
        await engine.payments.allot(admin, allot_request(ref="p-b"))
    copy_b = (await noted(store, "p-b"))["new_secret_ref"]
    assert secrets_named(await noted(store, "p-b")) == {"new": copy_b, "replaced": copy_a}
    assert set(vault.values) == {copy_a, copy_b}

    if handed_a_third_key == "under another LPB_ENGINE_ID":
        engine = await make_engine(supply=EngineSupply(secret_store=vault), engine_id=ANOTHER_ENGINE)
    else:
        await engine.payments.allot(admin, allot_request(ref="p-c"))
        await engine.payments.revoke(admin, "default", "alice")
    clock.advance(120)
    again = await engine.payments.allot(admin, allot_request(ref="p-b"))

    copy_c = again.secret_ref.ref
    assert copy_c not in (copy_a, copy_b)
    assert vault.values == {copy_c: manager.keys[again.api_key_id][1]}
    assert secrets_named(await noted(store, "p-b")) == {"new": copy_c, "replaced": copy_b}
    assert (await engine.payments.credential(alice)).token == manager.keys[again.api_key_id][1]


async def test_a_pending_delete_that_fails_again_stores_nothing_and_the_repeat_stores_the_key_once(
    make_engine: EngineFactory,
    admin: ActorContext,
    manager: FakeAllocationManager,
    clock: FixedClock,
    store: SqlStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The delete of a replaced copy that is still noted comes before the new key's secret is stored.
    When it fails, the command answers with the store's error and has stored and noted nothing: the
    record names the two copies it named, and both are there. The repeat is handed the same third key,
    deletes the first copy, stores the third key's secret once, and deletes the second copy."""
    vault = Vault()
    engine = await make_engine(supply=EngineSupply(secret_store=vault))
    copy_a = await cut_short_then_revoked(engine, admin, store, monkeypatch, vault)
    clock.advance(120)
    vault.stuck = {copy_a}
    with pytest.raises(ConnectionError):
        await engine.payments.allot(admin, allot_request(ref="p-b"))
    two_copies = await noted(store, "p-b")
    copy_b = two_copies["new_secret_ref"]

    other = await make_engine(supply=EngineSupply(secret_store=vault), engine_id=ANOTHER_ENGINE)
    clock.advance(120)
    puts = len(vault.put_secrets)
    with pytest.raises(ConnectionError, match="the vault is sealed"):
        await other.payments.allot(admin, allot_request(ref="p-b"))
    assert await noted(store, "p-b") == two_copies
    assert set(vault.values) == {copy_a, copy_b} and len(vault.put_secrets) == puts
    assert (await other.payments.get_actor(admin, "default", "alice")).status == "revoked"
    with pytest.raises(OperationInProgress):
        await other.payments.allot(admin, allot_request(ref="p-b"))

    vault.stuck = set()
    clock.advance(120)
    allocations = len(manager.allocations)
    again = await other.payments.allot(admin, allot_request(ref="p-b"))
    assert len(manager.allocations) == allocations  # the provider replayed the third key
    assert vault.put_secrets[puts:] == [manager.keys[again.api_key_id][1]]  # stored once
    assert vault.values == {again.secret_ref.ref: manager.keys[again.api_key_id][1]}
    assert secrets_named(await noted(store, "p-b")) == {"new": again.secret_ref.ref, "replaced": copy_b}
    assert vault.deleted.index(copy_a) < vault.deleted.index(copy_b)


async def test_every_stored_copy_stays_named_through_three_key_changes_whose_deletes_fail(
    make_engine: EngineFactory,
    admin: ActorContext,
    alice: ActorContext,
    manager: FakeAllocationManager,
    clock: FixedClock,
    store: SqlStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three resumes in a row are each handed another key, and each time the delete of the copy the
    resume replaced fails. After every one of them, each secret in the store is named by the record:
    as the key's secret, or as the copy it replaced. The last resume, whose deletes go through, leaves
    the actor's secret alone."""
    vault = Vault()
    await cut_short_then_revoked(
        await make_engine(supply=EngineSupply(secret_store=vault)), admin, store, monkeypatch, vault
    )
    for change in (1, 2, 3):
        engine = await make_engine(supply=EngineSupply(secret_store=vault), engine_id=f"engine-{change}")
        clock.advance(120)
        record = await noted(store, "p-b")
        vault.stuck = {record["new_secret_ref"]}  # the copy this resume replaces cannot be deleted
        with pytest.raises(ConnectionError):
            await engine.payments.allot(admin, allot_request(ref="p-b"))
        named = secrets_named(await noted(store, "p-b"))
        assert named["replaced"] == record["new_secret_ref"] and named["new"] != record["new_secret_ref"]
        assert set(vault.values) == set(named.values()), f"after key change {change}"

    vault.stuck = set()
    clock.advance(120)
    again = await engine.payments.allot(admin, allot_request(ref="p-b"))
    assert vault.values == {again.secret_ref.ref: manager.keys[again.api_key_id][1]}
    assert (await engine.payments.credential(alice)).token == manager.keys[again.api_key_id][1]


async def test_a_store_with_one_reference_for_an_actor_keeps_its_secret_through_two_key_changes(
    make_engine: EngineFactory,
    admin: ActorContext,
    alice: ActorContext,
    manager: FakeAllocationManager,
    clock: FixedClock,
    store: SqlStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With one path for an actor, the copy noted as replaced has the reference of the record's own
    secret, and stays noted for good. The next key change does not take it for a delete left to finish:
    the path holds the key the record names, and after the resume the key it was handed last."""
    vault = OnePathVault()
    engine = await make_engine(supply=EngineSupply(secret_store=vault))
    await cut_short_then_revoked(engine, admin, store, monkeypatch, vault)
    clock.advance(120)
    loses_the_actor_write(store, monkeypatch, "allot_actor")  # the resume dies after it noted its copy
    with pytest.raises(ConnectionError):
        await engine.payments.allot(admin, allot_request(ref="p-b"))
    [path] = vault.values
    assert secrets_named(await noted(store, "p-b")) == {"new": path, "replaced": path}

    other = await make_engine(supply=EngineSupply(secret_store=vault), engine_id=ANOTHER_ENGINE)
    clock.advance(120)
    again = await other.payments.allot(admin, allot_request(ref="p-b"))
    assert vault.values == {path: manager.keys[again.api_key_id][1]}
    assert (await other.payments.credential(alice)).token == manager.keys[again.api_key_id][1]


async def test_a_delivering_allotment_that_is_handed_another_key_asks_nothing_of_the_secret_store(
    make_engine: EngineFactory,
    admin: ActorContext,
    manager: FakeAllocationManager,
    clock: FixedClock,
    store: SqlStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The copy a delivering command replaces was a receipt in its first run's own buffer, which is
    gone with that run: the configured store never issued it, and is not asked to delete it."""
    vault = Vault()
    engine = await make_engine(supply=EngineSupply(secret_store=vault))
    loses_the_actor_write(store, monkeypatch, "allot_actor")
    with pytest.raises(ConnectionError):
        await engine.payments.allot_for_delivery(admin, allot_request(ref="p-b"))
    await engine.payments.allot_for_delivery(admin, allot_request(ref="p-a"))
    await engine.payments.revoke(admin, "default", "alice")
    clock.advance(120)
    again = await engine.payments.allot_for_delivery(admin, allot_request(ref="p-b"))

    assert again.allocation_api_key is not None
    assert again.allocation_api_key.get_secret_value() == manager.keys[again.api_key_id][1]
    assert (vault.values, vault.deleted) == ({}, [])


# -- a rotation --------------------------------------------------------------------------------------------


async def rotation_cut_short(
    engine: BuilderEngine,
    admin: ActorContext,
    store: SqlStore,
    manager: FakeAllocationManager,
    monkeypatch: pytest.MonkeyPatch,
    *,
    pointed: bool,
) -> None:
    """A rotation that stored its new key's secret and died: before the actor pointed at the new key,
    which leaves the old key the actor's, or after it and before the old key was revoked."""
    if pointed:
        manager.fail_once("revoke_key", ProviderUnavailable("Batteries unreachable: timed out"))
        with pytest.raises(ProviderUnavailable):
            await engine.payments.rotate_payment_key(admin, "default", "alice", "rotate-1")
    else:
        loses_the_actor_write(store, monkeypatch, "set_actor_key")
        with pytest.raises(ConnectionError):
            await engine.payments.rotate_payment_key(admin, "default", "alice", "rotate-1")


async def test_a_rotation_resumed_under_another_engine_id_drops_the_copy_it_replaces_and_never_the_actors(
    make_engine: EngineFactory,
    admin: ActorContext,
    alice: ActorContext,
    manager: FakeAllocationManager,
    clock: FixedClock,
    store: SqlStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first run stored its key's secret and died before the actor pointed at it: the old key is
    still the actor's. The resume, under another ``LPB_ENGINE_ID``, is handed a third key, stores its
    secret and points the actor at it. The first run's copy stayed stored beside it. It is deleted now,
    and the old key's secret is deleted only once the actor has left it, as before."""
    vault = Vault()
    engine = await make_engine(supply=EngineSupply(secret_store=vault))
    before = await engine.payments.allot(admin, allot_request())
    await rotation_cut_short(engine, admin, store, manager, monkeypatch, pointed=False)
    [first_copy] = set(vault.values) - {before.secret_ref.ref}
    assert (await engine.payments.get_actor(admin, "default", "alice")).secret_ref == before.secret_ref

    other = await make_engine(supply=EngineSupply(secret_store=vault), engine_id=ANOTHER_ENGINE)
    clock.advance(120)
    rotated = await other.payments.rotate_payment_key(admin, "default", "alice", "rotate-1")

    assert rotated.secret_ref.ref not in (first_copy, before.secret_ref.ref)
    assert set(vault.values) == {rotated.secret_ref.ref}
    assert vault.deleted.index(first_copy) < vault.deleted.index(before.secret_ref.ref)
    assert (await other.payments.credential(alice)).token == manager.keys[rotated.api_key_id][1]


async def test_a_rotation_resumed_under_another_engine_id_keeps_the_noted_copy_the_actor_holds(
    make_engine: EngineFactory,
    admin: ActorContext,
    alice: ActorContext,
    manager: FakeAllocationManager,
    clock: FixedClock,
    store: SqlStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first run pointed the actor at its key and died before it revoked the old one. The copy the
    record notes is then the actor's own secret. The resume, under another ``LPB_ENGINE_ID``, is handed
    a third key and replaces the note, but that copy is the one the actor pays with: it is kept, the
    third key is revoked and its secret dropped, and the resume is refused as an overtaken rotation."""
    vault = Vault()
    engine = await make_engine(supply=EngineSupply(secret_store=vault))
    before = await engine.payments.allot(admin, allot_request())
    await rotation_cut_short(engine, admin, store, manager, monkeypatch, pointed=True)
    held = await engine.payments.get_actor(admin, "default", "alice")
    assert held.api_key_id != before.api_key_id

    other = await make_engine(supply=EngineSupply(secret_store=vault), engine_id=ANOTHER_ENGINE)
    clock.advance(120)
    with pytest.raises(InvalidRequest, match="another key rotation"):
        await other.payments.rotate_payment_key(admin, "default", "alice", "rotate-1")

    assert await other.payments.get_actor(admin, "default", "alice") == held
    assert set(vault.values) == {held.secret_ref.ref} and held.secret_ref.ref not in vault.deleted
    assert live_keys(manager) == [held.api_key_id]
    assert (await other.payments.credential(alice)).token == manager.keys[held.api_key_id][1]


async def test_a_second_key_change_of_a_rotation_keeps_the_replaced_copy_the_actor_holds(
    make_engine: EngineFactory,
    admin: ActorContext,
    alice: ActorContext,
    manager: FakeAllocationManager,
    clock: FixedClock,
    store: SqlStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The copy the record notes as replaced is the actor's own secret: the first run had pointed the
    actor at it, and the resume under another ``LPB_ENGINE_ID`` was handed a third key and kept it. That
    resume then failed to revoke its third key, so the record is not closed. A resume under yet another
    id, handed a fourth key, finds that replaced copy still noted. It is the secret the actor pays
    with, and is not deleted as a delete left to finish; the third and fourth keys' secrets are."""
    vault = Vault()
    engine = await make_engine(supply=EngineSupply(secret_store=vault))
    before = await engine.payments.allot(admin, allot_request())
    await rotation_cut_short(engine, admin, store, manager, monkeypatch, pointed=True)
    held = await engine.payments.get_actor(admin, "default", "alice")

    second = await make_engine(supply=EngineSupply(secret_store=vault), engine_id=ANOTHER_ENGINE)
    clock.advance(120)
    manager.fail_once("revoke_key", ProviderUnavailable("Batteries unreachable: timed out"))
    with pytest.raises(ProviderUnavailable):
        await second.payments.rotate_payment_key(admin, "default", "alice", "rotate-1")
    named = secrets_named(await noted(store, "rotate-1"))
    assert named == {"previous": before.secret_ref.ref, "new": named["new"], "replaced": held.secret_ref.ref}
    assert set(vault.values) == set(named.values()) and len(vault.values) == 3

    third = await make_engine(supply=EngineSupply(secret_store=vault), engine_id="a-third-engine")
    clock.advance(120)
    with pytest.raises(InvalidRequest, match="another key rotation"):
        await third.payments.rotate_payment_key(admin, "default", "alice", "rotate-1")

    assert await third.payments.get_actor(admin, "default", "alice") == held
    assert set(vault.values) == {held.secret_ref.ref} and held.secret_ref.ref not in vault.deleted
    assert (await third.payments.credential(alice)).token == manager.keys[held.api_key_id][1]
