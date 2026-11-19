"""Allocation handoff: secrets stay at the work engine, and stale commands publish no key."""

import httpx
import pytest
from sqlalchemy import select

from livepeer_builder import EngineSupply
from livepeer_builder.contracts import ActorContext
from livepeer_builder.errors import AccessDenied, NotFound, NotProvisioned
from livepeer_builder.http.app import shared_app
from livepeer_builder.http.clearinghouse import mount_clearinghouse_routes
from livepeer_builder.services import payments
from livepeer_builder.store import SqlStore
from livepeer_builder.store import schema as t
from livepeer_builder.testing import MemorySecretStore
from tests.conftest import EngineFactory
from tests.unit.test_payments import allot_request


async def test_delivery_never_persists_secret_and_replay_returns_none(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, store: SqlStore
) -> None:
    vault = MemorySecretStore()
    engine = await make_engine(supply=EngineSupply(secret_store=vault))
    result = await engine.payments.allot_for_delivery(admin, allot_request())
    assert result.allocation_api_key and result.allocation_api_key.startswith("lpg_")
    assert result.allocation_api_key not in repr(result)
    assert result.secret_ref.ref.startswith("delivered:")
    with pytest.raises(NotFound, match="secret"):
        await vault.get(result.secret_ref)
    async with store.engine.connect() as conn:
        for table in t.metadata.sorted_tables:
            assert result.allocation_api_key not in repr((await conn.execute(select(table))).all())
    with pytest.raises(NotProvisioned, match="work engine"):
        await engine.payments.credential(alice)
    again = await engine.payments.allot_for_delivery(admin, allot_request())
    assert again.allocation_api_key is None
    rotated = await engine.payments.rotate_for_delivery(admin, "default", "alice", "rotate")
    assert rotated.allocation_api_key and rotated.allocation_api_key != result.allocation_api_key
    assert (
        await engine.payments.rotate_for_delivery(admin, "default", "alice", "rotate")
    ).allocation_api_key is None
    with pytest.raises(AccessDenied):
        await engine.payments.allot_for_delivery(alice, allot_request("bob"))


async def test_delayed_delivery_cannot_replace_reallotted_key(
    make_engine: EngineFactory, admin: ActorContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = await make_engine()
    original = payments.delivered
    replacement = []

    async def after_reallot(store, allocation, secrets):
        monkeypatch.setattr(payments, "delivered", original)
        await engine.payments.revoke(admin, "default", "alice")
        request = allot_request().model_copy(update={"operation_ref": "allot-again"})
        replacement.append(await engine.payments.allot_for_delivery(admin, request))
        return await original(store, allocation, secrets)

    monkeypatch.setattr(payments, "delivered", after_reallot)
    old = await engine.payments.allot_for_delivery(admin, allot_request())
    assert old.allocation_api_key is None
    new = replacement[0]
    assert new.allocation_api_key
    current = await engine.payments.get_actor(admin, "default", "alice")
    assert current.api_key_id == new.api_key_id
    assert current.allocation_id != old.allocation_id


async def test_clearinghouse_routes_deliver_and_expose_no_signer_proxy(make_engine: EngineFactory) -> None:
    engine = await make_engine()
    assert engine.keys is not None
    key = await engine.keys.bootstrap_admin()
    app = shared_app(engine, manage_lifecycle=False)
    mount_clearinghouse_routes(app, engine)
    headers = {"Authorization": f"Bearer {key.token}"}
    path = "/v1/clearinghouse/tenants/default/actors/alice/allocation"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://engine") as client:
        body = {"amount": {"amount": "5", "currency": "USD"}, "operation_ref": "first"}
        response = await client.post(path, json=body, headers=headers)
        assert response.status_code == 201, response.text
        assert response.json()["allocation_api_key"].startswith("lpg_")
        assert response.headers["cache-control"] == "no-store"
        assert "allocation_api_key" not in (await client.get(path, headers=headers)).json()
        assert (await client.post(path, json=body, headers=headers)).json()["allocation_api_key"] is None
        for route in ["/sign-orchestrator-info", "/generate-live-payment", "/v1/clearinghouse/payments"]:
            assert (await client.post(route, headers=headers)).status_code == 404


async def test_rotation_overtaken_by_revoke_returns_no_credential(
    make_engine: EngineFactory, admin: ActorContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = await make_engine()
    await engine.payments.allot_for_delivery(admin, allot_request())
    original = payments.delivered

    async def after_revoke(store, allocation, secrets):
        await engine.payments.revoke(admin, "default", "alice")
        return await original(store, allocation, secrets)

    monkeypatch.setattr(payments, "delivered", after_revoke)
    rotated = await engine.payments.rotate_for_delivery(admin, "default", "alice", "rotate-revoke")
    assert rotated.allocation_api_key is None
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

    async def failed(store, allocation, secrets):
        held.append((secrets, allocation.secret_ref))
        raise RuntimeError("response interrupted")

    monkeypatch.setattr(payments, "delivered", failed)
    with pytest.raises(RuntimeError, match="interrupted"):
        await engine.payments.allot_for_delivery(admin, allot_request())
    secrets, ref = held[0]
    assert secrets.token(ref) is None
