"""ISS-04: allocation modes, explicit provisioning, idempotent money moves, key custody and rotation."""

from decimal import Decimal

import pytest
from sqlalchemy import select

from livepeer_builder.contracts import ActorContext, FundRequest, Money, ProvisionRequest
from livepeer_builder.errors import AccessDenied, InvalidRequest, NotProvisioned
from livepeer_builder.store import SqlStore
from livepeer_builder.store import schema as t
from livepeer_builder.testing import FakeAllocationManager
from tests.conftest import EngineFactory


def usd(amount: str) -> Money:
    return Money(amount=Decimal(amount), currency="USD")


def provision_request(actor_id: str = "alice", amount: str = "5") -> ProvisionRequest:
    return ProvisionRequest(application_id="default", actor_id=actor_id, amount=usd(amount))


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


async def test_repeat_provisioning_returns_the_original(
    make_engine: EngineFactory, admin: ActorContext, manager: FakeAllocationManager
) -> None:
    engine = await make_engine()
    first = await engine.payments.provision(admin, provision_request())
    second = await engine.payments.provision(admin, provision_request())
    assert first == second
    assert manager.calls == ["create_key_for_grant"]
    assert len(manager.allocations) == 1


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
    assert await engine.payments.credential(alice) == secret


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


async def test_funding_the_configured_grant_applies_once(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, manager: FakeAllocationManager
) -> None:
    engine = await make_engine()
    request = FundRequest(amount=usd("10"), operation_ref="topup-1")
    with pytest.raises(AccessDenied):
        await engine.payments.fund_grant(alice, request)
    await engine.payments.fund_grant(admin, request)
    await engine.payments.fund_grant(admin, request)  # a retry
    assert manager.funded_grants == [("grant_1", usd("10"), "fund-grant:grant_1:topup-1")]
    with pytest.raises(InvalidRequest):
        await engine.payments.fund_grant(admin, FundRequest(amount=usd("0"), operation_ref="zero"))
    with pytest.raises(InvalidRequest):
        await engine.payments.fund_grant(
            admin, FundRequest(amount=Money(amount=Decimal(1), currency="ETH"), operation_ref="eth")
        )


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
    old_secret = await engine.payments.credential(alice)
    after = await engine.payments.rotate_key(admin, "default", "alice", "rotate-1")
    assert after.allocation_id == before.allocation_id and after.api_key_id != before.api_key_id
    assert manager.keys[before.api_key_id][2] is True  # old key revoked upstream
    assert await engine.payments.credential(alice) not in (None, old_secret)


async def test_per_application_mode_uses_the_configured_key(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext
) -> None:
    engine = await make_engine(allocation_mode="per_application", shared_payment_key="lpg_shared")
    assert await engine.payments.credential(alice) == "lpg_shared"
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
