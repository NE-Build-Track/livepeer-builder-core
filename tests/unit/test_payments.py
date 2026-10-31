"""ISS-04: allocation modes, explicit provisioning, idempotent money moves, key custody and rotation."""

from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from livepeer_builder.contracts import ActorContext, FundRequest, Money, ProvisionRequest, SeenActor
from livepeer_builder.errors import AccessDenied, InvalidRequest, NotFound, NotProvisioned
from livepeer_builder.services.payments import idempotency_key
from livepeer_builder.store import SqlStore
from livepeer_builder.store import schema as t
from livepeer_builder.testing import FakeAllocationManager, FixedClock
from tests.conftest import EngineFactory


def usd(amount: str) -> Money:
    return Money(amount=Decimal(amount), currency="USD")


def provision_request(
    actor_id: str = "alice",
    amount: str = "5",
    grant_id: str = "grant_1",
    *,
    application_id: str = "default",
    label: str = "",
) -> ProvisionRequest:
    return ProvisionRequest(
        operation_ref=f"provision:{application_id}:{actor_id}",
        application_id=application_id,
        actor_id=actor_id,
        amount=usd(amount),
        grant_id=grant_id,
        label=label,
    )


async def test_provisioning_is_explicit_and_admin_only(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, manager: FakeAllocationManager
) -> None:
    engine = await make_engine()
    with pytest.raises(AccessDenied):
        await engine.payments.provision(alice, provision_request())
    with pytest.raises(NotProvisioned):
        await engine.payments.credential(alice)  # nothing was provisioned automatically
    actor = await engine.payments.provision(admin, provision_request())
    assert actor.status == "active" and actor.currency == "USD"
    assert manager.calls == ["create_key_for_grant"]  # one atomic call: allocation + key + funding
    assert manager.last_grant_id == "grant_1"


async def test_provision_uses_the_grant_on_the_request(
    make_engine: EngineFactory, admin: ActorContext, manager: FakeAllocationManager
) -> None:
    engine = await make_engine()
    await engine.payments.provision(
        admin, provision_request().model_copy(update={"grant_id": "grant_chosen"})
    )
    assert manager.last_grant_id == "grant_chosen"


async def test_repeat_provisioning_returns_the_original(
    make_engine: EngineFactory, admin: ActorContext, manager: FakeAllocationManager
) -> None:
    engine = await make_engine()
    first = await engine.payments.provision(admin, provision_request())
    second = await engine.payments.provision(admin, provision_request())
    assert first == second
    assert manager.calls == ["create_key_for_grant"]
    assert len(manager.allocations) == 1


async def test_provisioning_repeated_after_a_lost_answer_creates_one_allocation(
    make_engine: EngineFactory,
    admin: ActorContext,
    store: SqlStore,
    manager: FakeAllocationManager,
    clock: FixedClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 21 October console note (pin ad6bba9): a repeated provision created a second allocation. The
    first call dies after the provider created the allocation, before the actor is saved; the repeat
    derives the same provider key from the actor, so the provider replays the first allocation. The lost
    answer leaves the operation started, so the repeat resumes it once it is stale (D57)."""
    engine = await make_engine()
    save_actor = store.save_actor

    async def lost(actor: object) -> None:
        monkeypatch.setattr(store, "save_actor", save_actor)  # only the first save is lost
        raise ConnectionError("the database went away")

    monkeypatch.setattr(store, "save_actor", lost)
    with pytest.raises(ConnectionError):
        await engine.payments.provision(admin, provision_request())
    clock.advance(120)
    actor = await engine.payments.provision(admin, provision_request())
    assert manager.calls == ["create_key_for_grant", "create_key_for_grant"]
    assert list(manager.allocations) == [actor.allocation_id]  # one allocation, the first one


async def test_zero_amount_sets_up_identity_without_money(
    make_engine: EngineFactory, admin: ActorContext
) -> None:
    engine = await make_engine()
    actor = await engine.payments.provision(admin, provision_request(amount="0"))
    assert actor.status == "exhausted"


async def test_payment_key_is_stored_encrypted_and_only_referenced(
    make_engine: EngineFactory,
    admin: ActorContext,
    alice: ActorContext,
    store: SqlStore,
    manager: FakeAllocationManager,
) -> None:
    engine = await make_engine()
    actor = await engine.payments.provision(admin, provision_request())
    secret = manager.keys[actor.api_key_id][1]
    assert actor.secret_ref.ref.startswith("db:")
    async with store.engine.connect() as conn:
        actors_dump = str((await conn.execute(select(t.actors))).all())
        secrets_dump = str((await conn.execute(select(t.secrets))).all())
    assert secret not in actors_dump and secret not in secrets_dump  # never plaintext at rest
    assert (await engine.payments.credential(alice)).token == secret


async def test_funding_needs_an_operation_ref_and_applies_once(
    make_engine: EngineFactory, admin: ActorContext, manager: FakeAllocationManager
) -> None:
    engine = await make_engine()
    actor = await engine.payments.provision(admin, provision_request(amount="0"))
    request = FundRequest(amount=usd("10"), operation_ref="topup-1")
    funded = await engine.payments.fund(admin, "default", "alice", request)
    await engine.payments.fund(admin, "default", "alice", request)  # a retry
    assert funded.status == "active"
    assert manager.allocations[actor.allocation_id].granted == Decimal(10)
    with pytest.raises(InvalidRequest):
        await engine.payments.fund(
            admin,
            "default",
            "alice",
            FundRequest(amount=Money(amount=Decimal(1), currency="ETH"), operation_ref="x"),
        )


async def test_operator_lists_and_creates_grants(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, manager: FakeAllocationManager
) -> None:
    engine = await make_engine()
    desk = engine.payments.grants
    assert desk is not None  # a grants manager was supplied, so the grant desk exists
    assert await desk.list_grants(admin) == []
    with pytest.raises(AccessDenied):
        await desk.list_grants(alice)
    with pytest.raises(InvalidRequest, match="name"):
        await desk.create_grant(admin, "  ", usd("1"), operation_ref="grant-1")
    with pytest.raises(InvalidRequest, match="positive"):
        await desk.create_grant(admin, "ops", usd("0"), operation_ref="grant-2")
    with pytest.raises(InvalidRequest, match="positive"):
        await desk.create_grant(
            admin, "ops", Money(amount=Decimal("1"), currency="ETH"), operation_ref="grant-3"
        )
    created = await desk.create_grant(admin, " ops ", usd("5"), operation_ref="grant-4")
    assert created == manager.grants[0]
    assert created.name == "ops" and created.total == "5"
    assert await desk.list_grants(admin) == [created]


async def test_an_auditor_never_sees_the_operators_grants(
    make_engine: EngineFactory, admin: ActorContext, manager: FakeAllocationManager
) -> None:
    """Grants are the operator's money for every application and carry none, so an auditor bound to
    one application cannot read them (John, #53 review). Admins still list them."""
    engine = await make_engine()
    desk = engine.payments.grants
    assert desk is not None
    await desk.create_grant(admin, "another application's pool", usd("5"), operation_ref="grant-5")
    auditor = ActorContext(actor_id="dashboard", credential_kind="auditor")
    with pytest.raises(AccessDenied):
        await desk.list_grants(auditor)
    assert len(await desk.list_grants(admin)) == 1


def test_a_provisioning_label_is_capped_where_it_is_stored() -> None:
    """``ProvisionedActor.label`` holds 200 characters, so the request refuses more before any provider
    call (John, #53 review)."""
    with pytest.raises(ValidationError):
        ProvisionRequest(
            operation_ref="provision:default:alice",
            application_id="default",
            actor_id="alice",
            amount=usd("1"),
            label="x" * 201,
        )
    assert ProvisionRequest(
        operation_ref="provision:default:alice",
        application_id="default",
        actor_id="alice",
        amount=usd("1"),
        label="x" * 200,
    )


async def test_funding_a_named_grant_applies_once(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, manager: FakeAllocationManager
) -> None:
    engine = await make_engine()
    request = FundRequest(amount=usd("10"), operation_ref="topup-1", grant_id="grant_chosen")
    with pytest.raises(AccessDenied):
        await engine.payments.fund_grant(alice, request)
    await engine.payments.fund_grant(admin, request)
    await engine.payments.fund_grant(admin, request)  # a retry
    key = idempotency_key(engine.settings.engine_id, "fund-grant:grant_chosen:topup-1", "fund")
    assert manager.funded_grants == [("grant_chosen", usd("10"), key)]
    assert 1 <= len(key) <= 256 and all(character.isalnum() or character in "_-" for character in key)
    with pytest.raises(InvalidRequest):
        await engine.payments.fund_grant(
            admin, FundRequest(amount=usd("0"), operation_ref="zero", grant_id="g")
        )
    with pytest.raises(InvalidRequest):
        await engine.payments.fund_grant(
            admin,
            FundRequest(amount=Money(amount=Decimal(1), currency="ETH"), operation_ref="eth", grant_id="g"),
        )


async def test_omitted_grant_uses_the_callers_allocation(
    make_engine: EngineFactory, admin: ActorContext, manager: FakeAllocationManager
) -> None:
    engine = await make_engine()
    await engine.payments.provision(admin, provision_request(actor_id="operator", grant_id="grant_ops"))
    await engine.payments.provision(admin, provision_request(grant_id=""))
    assert manager.last_grant_id == "grant_ops"
    await engine.payments.fund_grant(admin, FundRequest(amount=usd("3"), operation_ref="top"))
    key = idempotency_key(engine.settings.engine_id, "fund-grant:grant_ops:top", "fund")
    assert manager.funded_grants == [("grant_ops", usd("3"), key)]


async def test_a_grant_is_required_when_the_caller_has_no_allocation(
    make_engine: EngineFactory, admin: ActorContext
) -> None:
    engine = await make_engine()
    missing = provision_request(grant_id="")
    with pytest.raises(InvalidRequest, match="choose a grant"):
        await engine.payments.provision(admin, missing)
    with pytest.raises(InvalidRequest, match="choose a grant"):
        await engine.payments.fund_grant(admin, FundRequest(amount=usd("1"), operation_ref="g"))


async def test_revoke_forgets_the_key(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext
) -> None:
    engine = await make_engine()
    await engine.payments.provision(admin, provision_request())
    revoked = await engine.payments.revoke(admin, "default", "alice")
    assert revoked.status == "revoked"
    with pytest.raises(NotProvisioned):
        await engine.payments.credential(alice)


async def test_rotation_replaces_the_key_and_revokes_the_old_one(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, manager: FakeAllocationManager
) -> None:
    engine = await make_engine()
    before = await engine.payments.provision(admin, provision_request())
    old_secret = (await engine.payments.credential(alice)).token
    after = await engine.payments.rotate_key(admin, "default", "alice", "rotate-1")
    assert after.allocation_id == before.allocation_id and after.api_key_id != before.api_key_id
    assert manager.keys[before.api_key_id][2] is True  # old key revoked upstream
    assert (await engine.payments.credential(alice)).token not in (None, old_secret)


async def test_repeating_a_rotation_with_the_same_ref_keeps_the_new_key_live(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, manager: FakeAllocationManager
) -> None:
    """B1: the provider replays the same new key, so the repeat must not revoke it as "the old key"."""
    engine = await make_engine()
    before = await engine.payments.provision(admin, provision_request())
    first = await engine.payments.rotate_key(admin, "default", "alice", "rotate-1")
    again = await engine.payments.rotate_key(admin, "default", "alice", "rotate-1")  # a lost response
    assert again.api_key_id == first.api_key_id
    assert manager.keys[first.api_key_id][2] is False  # the live key is not revoked
    assert manager.keys[before.api_key_id][2] is True
    assert len(manager.keys) == 2  # provisioning's key and one rotation: one key minted
    assert (await engine.payments.credential(alice)).token == manager.keys[first.api_key_id][1]


async def test_per_application_mode_uses_the_configured_key(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext
) -> None:
    engine = await make_engine(allocation_mode="per_application", shared_payment_key="lpg_shared")
    shared = await engine.payments.credential(alice)
    assert shared.token == "lpg_shared" and shared.allocation_id is None
    with pytest.raises(InvalidRequest, match="per_actor"):
        await engine.payments.provision(admin, provision_request())


async def test_allowance_is_read_from_the_provider(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, bob: ActorContext
) -> None:
    engine = await make_engine()
    await engine.payments.provision(admin, provision_request())
    allowance = await engine.payments.allowance(alice)
    assert allowance.granted == usd("5") and allowance.available == usd("5")
    with pytest.raises(AccessDenied):
        await engine.payments.allowance(bob, "alice")


# -- the actor collection (#52, D46) ------------------------------------------------------------------------


async def test_provisioning_stores_the_label_and_admins_list_the_actors(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext
) -> None:
    engine = await make_engine()
    opaque = "4f6c2a1e-9b3d-4c8e-a1f0-7d2e5b9c3a10"  # an identity provider's sub
    await engine.payments.provision(admin, provision_request(actor_id=opaque, label="Alice Smith"))
    await engine.payments.provision(admin, provision_request(actor_id="bob", application_id="app-2"))
    listed = await engine.payments.list_actors(admin)
    assert [(a.application_id, a.actor_id, a.label) for a in listed] == [
        ("app-2", "bob", ""), ("default", opaque, "Alice Smith"),
    ]  # fmt: skip
    assert [a.actor_id for a in await engine.payments.list_actors(admin, application_id="app-2")] == ["bob"]
    assert (await engine.payments.get_actor(admin, "default", opaque)).label == "Alice Smith"
    with pytest.raises(NotFound):
        await engine.payments.get_actor(admin, "default", "nobody")
    with pytest.raises(AccessDenied):
        await engine.payments.list_actors(alice)  # admin reads need an admin or auditor credential


async def test_the_actor_list_filters_by_status_and_limits(
    make_engine: EngineFactory, admin: ActorContext
) -> None:
    engine = await make_engine()
    for name in ("a", "b", "c"):
        await engine.payments.provision(admin, provision_request(actor_id=name))
    await engine.payments.set_status(admin, "default", "b", "paused")
    assert [a.actor_id for a in await engine.payments.list_actors(admin, status="paused")] == ["b"]
    assert [a.actor_id for a in await engine.payments.list_actors(admin, limit=2)] == ["a", "b"]


async def test_people_who_signed_in_are_listed_before_provisioning_and_by_name_after(
    make_engine: EngineFactory, admin: ActorContext, store: SqlStore, clock: FixedClock
) -> None:
    """Registration on first sign-in (D48): the operator provisions people it can name."""
    engine = await make_engine()
    alice_sub, bob_sub = "3c446b40-7123-45f1", "14a5c7ce-7de4-44a0"
    for sub, name in ((alice_sub, "alice"), (bob_sub, "bob")):
        await store.save_sighting(
            SeenActor(application_id="default", actor_id=sub, name=name, first_seen_at=clock.now(), last_seen_at=clock.now())
        )  # fmt: skip
    await engine.payments.provision(admin, provision_request(actor_id=bob_sub, label="Bob B."))
    await engine.payments.provision(
        admin, provision_request(actor_id="carol")
    )  # provisioned, never signed in
    listed = {a.actor_id: a for a in await engine.payments.list_actors(admin)}
    alice, bob, carol = listed[alice_sub], listed[bob_sub], listed["carol"]
    assert (alice.provisioned, alice.allocation, alice.name, alice.label) == (False, None, "alice", "alice")
    assert alice.first_seen_at == clock.now()
    assert (bob.provisioned, bob.name, bob.label) == (True, "bob", "Bob B.")  # the operator's label wins
    assert bob.allocation is not None and bob.allocation.status == "active"
    assert (carol.provisioned, carol.name, carol.first_seen_at) == (True, None, None)
    unprovisioned = await engine.payments.list_actors(admin, provisioned=False)
    assert [a.actor_id for a in unprovisioned] == [alice_sub]
    assert alice_sub not in [a.actor_id for a in await engine.payments.list_actors(admin, status="active")]


async def test_provisioning_a_person_takes_their_name_as_the_label(
    make_engine: EngineFactory, admin: ActorContext, store: SqlStore, clock: FixedClock
) -> None:
    engine = await make_engine()
    seen = SeenActor(
        application_id="default",
        actor_id="3c446b40",
        name="alice",
        first_seen_at=clock.now(),
        last_seen_at=clock.now(),
    )
    await store.save_sighting(seen)
    provisioned = await engine.payments.provision(admin, provision_request(actor_id="3c446b40"))
    assert provisioned.label == "alice"  # unless the operator gives one


async def test_an_auditor_lists_only_its_applications_actors(
    make_engine: EngineFactory, admin: ActorContext
) -> None:
    engine = await make_engine()
    await engine.payments.provision(admin, provision_request(actor_id="alice"))
    await engine.payments.provision(admin, provision_request(actor_id="bob", application_id="app-2"))
    auditor = ActorContext(actor_id="dashboard", credential_kind="auditor")  # application "default"
    assert [a.actor_id for a in await engine.payments.list_actors(auditor)] == ["alice"]
    with pytest.raises(AccessDenied, match="another application"):
        await engine.payments.list_actors(auditor, application_id="app-2")
    with pytest.raises(AccessDenied, match="another application"):
        await engine.payments.get_actor(auditor, "app-2", "bob")
    with pytest.raises(AccessDenied):
        await engine.payments.provision(auditor, provision_request(actor_id="carol"))
