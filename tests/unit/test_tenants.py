"""Tenants at the clearinghouse (#165, D83): two tenants on one engine, each with its own admin.

A tenant is one enterprise app's account. Its id is the id of its grant at the payment provider, so no
route or body takes a separate grant id. A tenant admin divides its tenant's grant and cannot enlarge it;
only a clearinghouse admin onboards a tenant, tops up a grant or pauses a tenant.

Since 19 November 2026 (D87 point 3, as amended) a tenant's admin and auditor call its own routes, which
name no tenant: the tenant is the credential's. A clearinghouse admin calls the same routes at paths that
name the tenant, and a tenant credential is 403 on those, another tenant's or its own, before any lookup
or provider call (tests/unit/test_tenant_from_credential.py sweeps every route).

The first test is the guide's starter test
(docs/design-docs/m4-guides/iss-31-payment-configurations.md, "Test plan").
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import update

from livepeer_builder import BuilderEngine, EngineSupply
from livepeer_builder.contracts import ActorContext, AllotRequest, JobRequest, Money, TopUpRequest
from livepeer_builder.errors import AccessDenied, InvalidRequest, ProviderError
from livepeer_builder.http import create_app
from livepeer_builder.store import SqlStore
from livepeer_builder.store import schema as t
from livepeer_builder.testing import FakeAllocationManager, FixedClock, ScriptedTransport
from tests.conftest import EngineFactory, allot
from tests.unit.test_assembly import HeaderAuthenticator

OWN = "/v1/clearinghouse"  # a tenant's own routes: no tenant in the path
TENANTS = OWN + "/tenants"
USD_100 = {"amount": "100", "currency": "USD"}


def usd(amount: str) -> dict[str, str]:
    return {"amount": amount, "currency": "USD"}


@dataclass
class Tenant:
    """One onboarded tenant: its id, which is its grant's id, and its admin's key. Its admin and auditor
    call its own routes, which name no tenant (``path``); a clearinghouse admin calls the same routes at
    the path that names the tenant (``named``)."""

    id: str
    admin: dict[str, str]
    key_id: str

    def path(self, rest: str = "") -> str:
        return OWN + rest

    def allocation(self, actor_id: str, rest: str = "") -> str:
        return self.path(f"/actors/{actor_id}/allocation{rest}")

    def named(self, rest: str = "") -> str:
        return f"{TENANTS}/{self.id}{rest}"

    def named_allocation(self, actor_id: str, rest: str = "") -> str:
        return self.named(f"/actors/{actor_id}/allocation{rest}")


@dataclass
class House:
    """A clearinghouse: one engine behind its REST app, its admin, and two tenants."""

    client: httpx.AsyncClient
    engine: BuilderEngine
    admin: dict[str, str]  # the clearinghouse admin's key
    one: Tenant
    two: Tenant


async def onboard(client: httpx.AsyncClient, admin: dict[str, str], name: str, total: str = "100") -> Tenant:
    body = {"name": name, "amount": usd(total), "operation_ref": f"onboard-{name}"}
    answer = await client.post(TENANTS, json=body, headers=admin)
    assert answer.status_code == 201, answer.text
    key = answer.json()["admin_key"]
    return Tenant(
        answer.json()["tenant_id"], {"Authorization": f"Bearer {key['token']}"}, key["key"]["key_id"]
    )


@pytest.fixture
async def house(make_engine: EngineFactory) -> AsyncIterator[House]:
    engine = await make_engine()
    assert engine.keys is not None
    admin = {"Authorization": f"Bearer {(await engine.keys.bootstrap_admin()).token}"}
    app = create_app(engine, manage_lifecycle=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://engine") as client:
        one, two = await onboard(client, admin, "one"), await onboard(client, admin, "two")
        yield House(client, engine, admin, one, two)


async def allotted(house: House, tenant: Tenant, actor_id: str, amount: str = "5") -> dict[str, Any]:
    body = {"amount": usd(amount), "operation_ref": f"allot-{tenant.id}-{actor_id}"}
    answer = await house.client.post(tenant.allocation(actor_id), json=body, headers=tenant.admin)
    assert answer.status_code == 201, answer.text
    return dict(answer.json())


# -- the starter test -----------------------------------------------------------------------------------


async def test_a_tenant_admin_is_403_on_a_route_that_names_another_tenant_with_no_provider_call(
    house: House, manager: FakeAllocationManager
) -> None:
    """The guide's starter test. Since 19 November 2026 a path that names a tenant is a clearinghouse
    admin's: a tenant admin is refused there for its kind, the other tenant's path and its own alike, and
    its own routes name no tenant, so they reach no other."""
    await allotted(house, house.two, "bob")
    calls = list(manager.calls)
    body = {"amount": usd("1"), "operation_ref": "starter"}
    refused = await house.client.post(
        house.two.named_allocation("mallory"), json=body, headers=house.one.admin
    )
    assert refused.status_code == 403, refused.text
    assert "clearinghouse admin's" in refused.json()["error"]
    for tenant in (house.two, house.one):  # the other tenant's path, and its own
        top_up = await house.client.post(
            tenant.named_allocation("bob", "/top-up"), json=body, headers=house.one.admin
        )
        assert top_up.status_code == 403
    assert manager.calls == calls  # Batteries heard of none of them
    assert await house.engine.store.get_actor(house.two.id, "mallory") is None
    assert await house.engine.store.get_operation(house.one.id, "tenant-admin", "starter") is None
    # its own path for bob is its own tenant's, where there is no bob
    mine = await house.client.post(house.one.allocation("bob", "/top-up"), json=body, headers=house.one.admin)
    assert mine.status_code == 404 and manager.calls == calls


# -- onboarding -----------------------------------------------------------------------------------------


async def test_onboarding_creates_one_grant_and_returns_the_tenant_id_and_one_tenant_admin_key_once(
    make_engine: EngineFactory, manager: FakeAllocationManager, store: SqlStore
) -> None:
    engine = await make_engine()
    assert engine.keys is not None
    admin = {"Authorization": f"Bearer {(await engine.keys.bootstrap_admin()).token}"}
    app = create_app(engine, manage_lifecycle=False)
    body = {"name": "ChatLPT", "amount": USD_100, "admin_actor_id": "chatlpt", "operation_ref": "onboard-1"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://engine") as client:
        first = await client.post(TENANTS, json=body, headers=admin)
        assert first.status_code == 201 and "Idempotent-Replayed" not in first.headers
        tenant_id, key = first.json()["tenant_id"], first.json()["admin_key"]
        # one grant, and the tenant's id is its id
        created = [grant for grant in manager.grants if grant.id != "default"]
        assert [grant.id for grant in created] == [tenant_id] and manager.calls == ["create_grant"]
        assert (created[0].name, created[0].total) == ("ChatLPT", "100")
        # one key: a tenant admin's, bound to the tenant, and it works
        assert key["token"].startswith("lpk_") and key["replayed"] is False
        assert (key["key"]["kind"], key["key"]["tenant_id"], key["key"]["actor_id"]) == (
            "tenant_admin",
            tenant_id,
            "chatlpt",
        )
        as_tenant = {"Authorization": f"Bearer {key['token']}"}
        assert (await client.get(OWN + "/grant", headers=as_tenant)).json()["tenant_id"] == tenant_id

        again = await client.post(TENANTS, json=body, headers=admin)  # the answer was lost: a repeat
        assert again.status_code == 201 and again.headers["Idempotent-Replayed"] == "true"
        assert again.json() == first.json() | {"admin_key": key | {"token": None, "replayed": True}}
        assert manager.calls.count("create_grant") == 1  # no second grant
    keys = [key for key in await store.list_access_keys(tenant_id) if key.kind == "tenant_admin"]
    assert len(keys) == 1  # and no second key
    record = await store.get_operation("default", "operator", "onboard-1")
    assert record is not None and record.result["admin_key"]["token"] is None  # the ledger never holds it
    assert record.context == {"tenant_id": tenant_id, "admin_key_id": keys[0].key_id}


async def test_an_onboarding_cut_short_after_its_key_was_stored_issues_no_second_key_and_shows_no_token(
    make_engine: EngineFactory,
    admin: ActorContext,
    manager: FakeAllocationManager,
    store: SqlStore,
    clock: FixedClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The key is stored, then the engine dies before the command is recorded as done. The token went
    with that run: the engine keeps its hash only. A resume finds the grant and the key by the ids the
    record noted, makes neither again, and answers without a token, as a replayed key issue does."""
    engine = await make_engine()
    desk = engine.payments.tenants
    assert desk is not None
    complete = store.complete_operation

    async def died(*_: object, **__: object) -> bool:
        monkeypatch.setattr(store, "complete_operation", complete)
        raise ConnectionError("the database went away")

    monkeypatch.setattr(store, "complete_operation", died)
    with pytest.raises(ConnectionError):
        await desk.onboard(admin, "ops", Money(amount=Decimal(5), currency="USD"), operation_ref="o-1")
    clock.advance(120)
    resumed = await desk.onboard(admin, "ops", Money(amount=Decimal(5), currency="USD"), operation_ref="o-1")
    assert resumed.admin_key is not None
    assert (resumed.admin_key.token, resumed.admin_key.replayed) == (None, True)
    assert manager.calls.count("create_grant") == 1
    keys = await store.list_access_keys(resumed.tenant_id)
    assert [key.key_id for key in keys] == [resumed.admin_key.key.key_id]


async def test_onboarding_with_engine_access_keys_off_creates_the_grant_and_no_key(
    make_engine: EngineFactory, admin: ActorContext, manager: FakeAllocationManager
) -> None:
    """``LPB_AUTH=jwt``: the tenant's admin is another kind of credential (ISS-33), so there is no key."""
    off = EngineSupply(api_keys=None, authenticators=(HeaderAuthenticator(),))
    engine = await make_engine(supply=off)
    desk = engine.payments.tenants
    assert desk is not None
    onboarded = await desk.onboard(
        admin, "ops", Money(amount=Decimal(0), currency="USD"), operation_ref="o-1"
    )
    assert onboarded.admin_key is None and manager.grants[-1].id == onboarded.tenant_id
    assert (await desk.grant(admin, onboarded.tenant_id)).total.amount == 0  # onboarded, to top up later


# -- one id, and what a tenant admin does in its own tenant ---------------------------------------------


async def test_the_tenants_id_is_the_grants_id_and_no_route_takes_a_separate_grant_id(
    house: House, manager: FakeAllocationManager
) -> None:
    alice = await allotted(house, house.one, "alice")
    assert manager.last_grant_id == house.one.id == alice["grant_id"] == alice["tenant_id"]
    grant = (await house.client.get(house.one.path("/grant"), headers=house.one.admin)).json()
    assert grant == {
        "tenant_id": house.one.id, "name": "one", "status": "active",
        "total": usd("100"), "unallocated": usd("95"), "spent": usd("0"), "allocated_unspent": usd("5"),
    }  # fmt: skip
    # no request of any route has a grant id: not in a path, a query string or a body
    schema = create_app(house.engine, manage_lifecycle=False).openapi()
    for path, operations in schema["paths"].items():
        assert "grant_id" not in path
        for operation in operations.values():
            assert "grant_id" not in [parameter["name"] for parameter in operation.get("parameters", [])]
            body = operation.get("requestBody", {}).get("content", {}).get("application/json", {})
            name = body.get("schema", {}).get("$ref", "").rsplit("/", 1)[-1]
            assert "grant_id" not in schema["components"]["schemas"].get(name, {}).get("properties", {}), path
    stale = {"amount": usd("1"), "operation_ref": "stale", "grant_id": house.two.id}
    named = await house.client.post(house.one.allocation("bob"), json=stale, headers=house.one.admin)
    assert named.status_code == 422  # a body that names another tenant's grant is not read past it


async def test_a_tenant_admin_allots_tops_up_pauses_and_revokes_allocations_in_its_own_tenant(
    house: House, manager: FakeAllocationManager
) -> None:
    client, one = house.client, house.one
    alice = await allotted(house, one, "alice", "5")
    assert (alice["tenant_id"], alice["status"]) == (one.id, "active")
    top_up = {"amount": usd("2"), "operation_ref": "top-up-alice"}
    assert (
        await client.post(one.allocation("alice", "/top-up"), json=top_up, headers=one.admin)
    ).status_code == 200
    assert manager.allocations[alice["allocation_id"]].granted == Decimal(7)
    paused = await client.post(
        one.allocation("alice", "/status"), json={"status": "paused"}, headers=one.admin
    )
    assert (paused.status_code, paused.json()["status"]) == (200, "paused")
    rotated = await client.post(
        one.allocation("alice", "/rotate-payment-key"),
        json={"operation_ref": "rotate-alice"},
        headers=one.admin,
    )
    assert rotated.status_code == 200 and rotated.json()["api_key_id"] != alice["api_key_id"]
    listed = await client.get(one.path("/actors"), headers=one.admin)
    assert [(actor["actor_id"], actor["allotted"]) for actor in listed.json()] == [("alice", True)]
    assert (await client.get(one.allocation("alice"), headers=one.admin)).json()["status"] == "paused"
    revoked = await client.post(one.allocation("alice", "/revoke"), headers=one.admin)
    assert (revoked.status_code, revoked.json()["status"]) == (200, "revoked")
    assert manager.unallocated(one.id) == Decimal(100)  # nothing was spent: it all returned to the grant
    assert manager.unallocated(house.two.id) == Decimal(100)  # the other tenant's grant never moved
    # and it issues an auditor key for its tenant, which reads the tenant and writes nothing
    issued = await client.post(
        one.path("/keys"), json={"actor_id": "dashboard", "operation_ref": "auditor-1"}, headers=one.admin
    )
    assert (issued.status_code, issued.json()["key"]["kind"], issued.json()["key"]["tenant_id"]) == (
        201,
        "auditor",
        one.id,
    )
    auditor = {"Authorization": f"Bearer {issued.json()['token']}"}
    assert (await client.get(one.path("/grant"), headers=auditor)).json()["tenant_id"] == one.id
    assert (await client.get(one.path("/actors"), headers=auditor)).status_code == 200
    assert (await client.get(house.two.named("/grant"), headers=auditor)).status_code == 403
    assert (await client.get(one.named("/grant"), headers=auditor)).status_code == 403
    assert (await client.post(one.allocation("bob"), json=top_up, headers=auditor)).status_code == 403


async def test_a_tenant_admin_cannot_onboard_a_tenant_top_up_its_grant_or_issue_a_tenant_admin_key(
    house: House, manager: FakeAllocationManager, store: SqlStore
) -> None:
    client, one = house.client, house.one
    calls, keys = list(manager.calls), await store.list_access_keys(None)
    refusals = [
        ("POST", TENANTS, {"name": "mine", "amount": USD_100, "operation_ref": "r1"}),
        ("GET", TENANTS, None),
        ("POST", one.named("/grant/top-up"), {"amount": USD_100, "operation_ref": "r2"}),
        ("POST", one.named("/status"), {"status": "paused", "operation_ref": "r3"}),
        ("POST", one.path("/keys"), {"kind": "tenant_admin", "actor_id": "second", "operation_ref": "r4"}),
        ("POST", "/v1/keys", {"kind": "tenant_admin", "actor_id": "x", "tenant_id": one.id, "operation_ref": "r5"}),
        ("POST", "/v1/keys", {"kind": "clearinghouse_admin", "actor_id": "x", "tenant_id": one.id, "operation_ref": "r6"}),
        ("POST", "/v1/costs/sync", None),  # every tenant's usage: a clearinghouse admin's
    ]  # fmt: skip
    for method, path, body in refusals:
        answer = await client.request(method, path, json=body, headers=one.admin)
        assert answer.status_code == 403, (method, path, answer.text)
    assert manager.calls == calls and manager.unallocated(one.id) == Decimal(100)
    assert await store.list_access_keys(None) == keys
    # a clearinghouse admin does each of them
    top_up = {"amount": USD_100, "operation_ref": "grow-one"}
    assert (
        await client.post(one.named("/grant/top-up"), json=top_up, headers=house.admin)
    ).status_code == 204
    assert manager.unallocated(one.id) == Decimal(200)
    second = {"kind": "tenant_admin", "actor_id": "second", "operation_ref": "second-admin"}
    issued = await client.post(one.named("/keys"), json=second, headers=house.admin)
    assert (issued.status_code, issued.json()["key"]["kind"]) == (201, "tenant_admin")
    assert issued.json()["key"]["tenant_id"] == one.id
    assert [tenant["tenant_id"] for tenant in (await client.get(TENANTS, headers=house.admin)).json()] == [
        "default",
        one.id,
        house.two.id,
    ]


# -- the grant is divided at the provider ---------------------------------------------------------------


async def test_an_allocation_larger_than_the_grants_unallocated_balance_is_refused(
    house: House, manager: FakeAllocationManager, store: SqlStore
) -> None:
    """Batteries refuses, with its reason, and the engine passes that on as a definite refusal: it reads
    no balance and decides nothing itself, so it cannot race the provider's ledger."""
    client, one = house.client, house.one
    await allotted(house, one, "alice", "60")
    calls = list(manager.calls)
    too_much = {"amount": usd("40.01"), "operation_ref": "allot-bob"}
    refused = await client.post(one.allocation("bob"), json=too_much, headers=one.admin)
    assert refused.status_code == 409, refused.text
    assert "insufficient unallocated grant balance" in refused.json()["error"]
    assert manager.calls == [*calls, "create_key_for_grant"]  # one call, the provider's refusal: no read
    assert await store.get_actor(one.id, "bob") is None and manager.unallocated(one.id) == Decimal(40)
    # a definite refusal frees the reference: the same command with what is left goes through
    fits = await client.post(one.allocation("bob"), json=too_much | {"amount": usd("40")}, headers=one.admin)
    assert fits.status_code == 201 and manager.unallocated(one.id) == Decimal(0)
    more = {"amount": usd("0.01"), "operation_ref": "more-alice"}
    top_up = await client.post(one.allocation("alice", "/top-up"), json=more, headers=one.admin)
    assert top_up.status_code == 409 and "insufficient unallocated grant balance" in top_up.json()["error"]
    # the other tenant's grant is no help, and what a revoke returns is
    assert manager.unallocated(house.two.id) == Decimal(100)
    assert (await client.post(one.allocation("bob", "/revoke"), headers=one.admin)).status_code == 200
    assert (
        await client.post(one.allocation("alice", "/top-up"), json=more, headers=one.admin)
    ).status_code == 200


# -- a paused tenant ------------------------------------------------------------------------------------


async def test_pausing_a_tenant_pauses_its_grant_and_the_engine_enforces_nothing_itself(
    house: House, manager: FakeAllocationManager
) -> None:
    """What a paused grant stops is Batteries' rule: at the pin it refuses every payment of the grant's
    allocations (403) and still takes allotments and top-ups. The engine passes the status on."""
    client, one = house.client, house.one
    pause = {"status": "paused", "operation_ref": "pause-1"}
    paused = await client.post(one.named("/status"), json=pause, headers=house.admin)
    assert (paused.status_code, paused.json()) == (200, {"tenant_id": one.id, "status": "paused"})
    assert (await client.get(one.path("/grant"), headers=one.admin)).json()["status"] == "paused"
    listed = {
        tenant["tenant_id"]: tenant["status"]
        for tenant in (await client.get(TENANTS, headers=house.admin)).json()
    }
    assert (listed[one.id], listed[house.two.id]) == ("paused", "active")
    await allotted(house, one, "alice")  # the provider still takes it, so the engine does
    resume = {"status": "active", "operation_ref": "resume-1"}
    assert (await client.post(one.named("/status"), json=resume, headers=house.admin)).json()[
        "status"
    ] == "active"
    # a late repeat of the pause is answered from the ledger: the tenant is not paused again
    late = await client.post(one.named("/status"), json=pause, headers=house.admin)
    assert late.status_code == 200 and late.headers["Idempotent-Replayed"] == "true"
    assert (await client.get(one.path("/grant"), headers=one.admin)).json()["status"] == "active"
    assert manager.calls.count("set_grant_status") == 2
    for status in ("closed", "draft", "revoked"):  # a grant is never closed
        body = {"status": status, "operation_ref": f"never-{status}"}
        assert (await client.post(one.named("/status"), json=body, headers=house.admin)).status_code == 422
    unknown = await client.post(
        f"{TENANTS}/no-such-tenant/status", json=pause | {"operation_ref": "p-2"}, headers=house.admin
    )
    assert unknown.status_code == 404  # no such grant at the provider


# -- a tenant admin's key ---------------------------------------------------------------------------------


async def test_a_tenant_admin_key_never_acts_on_another_tenants_keys(house: House, store: SqlStore) -> None:
    client, one, two = house.client, house.one, house.two
    before = await store.list_access_keys(None)
    # issuing for another tenant, in a path or in a body
    for path, body in (
        (two.named("/keys"), {"actor_id": "spy", "operation_ref": "k1"}),
        ("/v1/keys", {"kind": "auditor", "actor_id": "spy", "tenant_id": two.id, "operation_ref": "k2"}),
    ):
        assert (await client.post(path, json=body, headers=one.admin)).status_code == 403, path
    # a body that names no tenant was put in "default", another tenant's: now it is refused as it is read
    unnamed = {"kind": "actor", "actor_id": "spy", "operation_ref": "k3"}
    assert (await client.post("/v1/keys", json=unnamed, headers=one.admin)).status_code == 422
    # reading another tenant's keys: no path of its own names another tenant, and one that does is 403
    assert (await client.get(two.named("/keys"), headers=one.admin)).status_code == 403
    assert {key["tenant_id"] for key in (await client.get(one.path("/keys"), headers=one.admin)).json()} == {
        one.id
    }
    assert (await client.get(f"/v1/keys?tenant_id={two.id}", headers=one.admin)).status_code == 403
    assert [key["tenant_id"] for key in (await client.get("/v1/keys", headers=one.admin)).json()] == [one.id]
    # another tenant's key, named by its id: missing, through its own routes and the engine's
    rotate = {"operation_ref": "k4"}
    assert (await client.get(one.path(f"/keys/{two.key_id}"), headers=one.admin)).status_code == 404
    assert (await client.get(f"/v1/keys/{two.key_id}", headers=one.admin)).status_code == 404
    for path in (one.path(f"/keys/{two.key_id}"), f"/v1/keys/{two.key_id}"):
        assert (await client.post(path + "/rotate", json=rotate, headers=one.admin)).status_code == 400, path
        assert (await client.post(path + "/revoke", headers=one.admin)).status_code == 400, path
    assert (await client.post(two.named(f"/keys/{two.key_id}/revoke"), headers=one.admin)).status_code == 403
    assert await store.list_access_keys(None) == before
    assert (await client.get(two.path("/grant"), headers=two.admin)).status_code == 200  # still two's key


async def test_a_tenant_admin_changes_no_admin_key_and_manages_its_tenants_other_keys(
    house: House, store: SqlStore
) -> None:
    client, one = house.client, house.one
    second = await client.post(
        one.named("/keys"),
        json={"kind": "tenant_admin", "actor_id": "second", "operation_ref": "second"},
        headers=house.admin,
    )
    other_admin = second.json()["key"]["key_id"]
    rotate = {"operation_ref": "rotate-admin"}
    for key_id in (one.key_id, other_admin):  # its own key, and its tenant's other admin key
        for path in (one.path(f"/keys/{key_id}"), f"/v1/keys/{key_id}"):
            assert (await client.post(path + "/rotate", json=rotate, headers=one.admin)).status_code == 403
            assert (await client.post(path + "/revoke", headers=one.admin)).status_code == 403
    assert all(key.revoked_at is None for key in await store.list_access_keys(one.id))
    # an auditor key of its tenant: issued, read, rotated and revoked
    issued = await client.post(
        one.path("/keys"), json={"actor_id": "dashboard", "operation_ref": "aud"}, headers=one.admin
    )
    auditor = issued.json()["key"]["key_id"]
    kinds = {
        key["key_id"]: key["kind"] for key in (await client.get(one.path("/keys"), headers=one.admin)).json()
    }
    assert kinds == {one.key_id: "tenant_admin", other_admin: "tenant_admin", auditor: "auditor"}
    assert (await client.get(one.path(f"/keys/{auditor}"), headers=one.admin)).json()["kind"] == "auditor"
    rotated = await client.post(one.path(f"/keys/{auditor}/rotate"), json=rotate, headers=one.admin)
    assert rotated.status_code == 201 and rotated.json()["key"]["tenant_id"] == one.id
    revoke = one.path(f"/keys/{rotated.json()['key']['key_id']}/revoke")
    assert (await client.post(revoke, headers=one.admin)).status_code == 204
    # on an engine that also runs jobs, it issues its tenant's actor keys under /v1/keys
    actor = {"kind": "actor", "actor_id": "alice", "tenant_id": one.id, "operation_ref": "alice"}
    assert (await client.post("/v1/keys", json=actor, headers=one.admin)).status_code == 201
    # the clearinghouse admin revokes a tenant admin key
    assert (
        await client.post(one.named(f"/keys/{other_admin}/revoke"), headers=house.admin)
    ).status_code == 204
    # only the two kinds a tenant holds at a clearinghouse are issued under the tenant's routes
    for kind in ("actor", "service", "clearinghouse_admin"):
        body = {"kind": kind, "actor_id": "x", "operation_ref": f"kind-{kind}"}
        assert (await client.post(one.named("/keys"), json=body, headers=house.admin)).status_code == 422
        assert (await client.post(one.path("/keys"), json=body, headers=one.admin)).status_code == 422
    named = {"actor_id": "x", "tenant_id": house.two.id, "operation_ref": "body-tenant"}
    assert (await client.post(one.named("/keys"), json=named, headers=house.admin)).status_code == 422
    assert (await client.post(one.path("/keys"), json=named, headers=one.admin)).status_code == 422


async def test_a_key_rotation_repeated_under_another_tenants_path_is_400_and_shows_no_key(
    house: House, store: SqlStore
) -> None:
    """The repeat of a rotation is answered from its record. Under another tenant's path it reads as
    missing, as the first run there would have (#165 review): a clearinghouse admin may name any tenant,
    so only the key's own tenant stops it. A tenant credential names no tenant in a path since #185, so
    the paths that name one, the clearinghouse admin's, are where this can be asked."""
    client, one, two = house.client, house.one, house.two
    issued = await client.post(
        one.path("/keys"), json={"actor_id": "dashboard", "operation_ref": "aud"}, headers=one.admin
    )
    auditor = issued.json()["key"]["key_id"]
    rotate = {"operation_ref": "rotate-aud"}
    first = await client.post(one.named(f"/keys/{auditor}/rotate"), json=rotate, headers=house.admin)
    assert first.status_code == 201 and first.json()["token"]
    elsewhere = await client.post(two.named(f"/keys/{auditor}/rotate"), json=rotate, headers=house.admin)
    assert elsewhere.status_code == 400 and first.json()["key"]["key_id"] not in elsewhere.text
    again = await client.post(one.named(f"/keys/{auditor}/rotate"), json=rotate, headers=house.admin)
    assert again.headers["Idempotent-Replayed"] == "true"
    assert (again.json()["key"], again.json()["token"]) == (first.json()["key"], None)
    assert (
        len(await store.list_access_keys(one.id)) == 3
    )  # its admin key, the auditor key and its replacement


# -- the work set -------------------------------------------------------------------------------------------


async def test_on_the_work_set_a_tenant_admin_is_an_admin_of_its_own_tenant_only(
    house: House, transport: ScriptedTransport
) -> None:
    client, engine, one, two = house.client, house.engine, house.one, house.two
    ours = ActorContext(actor_id="alice", tenant_id=one.id, permissions=frozenset({"jobs.run", "jobs.read"}))
    theirs = ours.model_copy(update={"actor_id": "bob", "tenant_id": two.id})
    await allot(engine, ours, theirs)
    transport.steps = [ScriptedTransport.ok(), ScriptedTransport.ok(manifest_ids=("manifest-2",))]
    mine = (await engine.jobs.run(ours, JobRequest(app="example/echo"))).job.id
    other = (await engine.jobs.run(theirs, JobRequest(app="example/echo"))).job.id
    assert [job["id"] for job in (await client.get("/v1/jobs", headers=one.admin)).json()] == [str(mine)]
    assert (await client.get(f"/v1/jobs/{mine}", headers=one.admin)).status_code == 200
    assert (await client.get(f"/v1/jobs/{other}", headers=one.admin)).status_code == 404  # as missing (D44)
    assert (await client.get(f"/v1/jobs/{other}/cost", headers=one.admin)).status_code == 404
    reconciliation = "/v1/costs/reconciliation?actor_id=alice&tenant_id="
    assert (await client.get(reconciliation + one.id, headers=one.admin)).status_code == 200
    assert (await client.get(reconciliation + two.id, headers=one.admin)).status_code == 403
    # no tenant named: once read as "default", another tenant's; now refused as it is read (#163 review)
    assert (await client.get("/v1/costs/reconciliation?actor_id=alice", headers=one.admin)).status_code == 422
    assert (await client.get("/v1/allowance?actor_id=alice", headers=one.admin)).status_code == 200
    assert (await client.get("/v1/allowance?actor_id=bob", headers=one.admin)).status_code == 404
    assert (await client.post("/v1/costs/sync", headers=one.admin)).status_code == 403
    assert (await client.post("/v1/costs/sync", headers=house.admin)).status_code == 200


# -- the embedded API ---------------------------------------------------------------------------------------


async def test_the_embedded_api_confines_a_tenant_admin_as_the_routes_do(
    make_engine: EngineFactory, admin: ActorContext, manager: FakeAllocationManager
) -> None:
    """In process there is no route: the services pass the tenant to the policy themselves."""
    manager.add_grant("other")
    engine = await make_engine()
    desk, keys = engine.payments.tenants, engine.keys
    assert desk is not None and keys is not None
    tenant_admin = ActorContext(actor_id="tenant-admin", tenant_id="default", credential_kind="tenant_admin")
    five = Money(amount=Decimal(5), currency="USD")
    await allot(engine, ActorContext(actor_id="bob", tenant_id="other"))
    calls = list(manager.calls)
    with pytest.raises(AccessDenied, match="another tenant"):
        await engine.payments.top_up(
            tenant_admin, "other", "bob", TopUpRequest(amount=five, operation_ref="t")
        )
    with pytest.raises(AccessDenied, match="another tenant"):
        await engine.payments.set_status(tenant_admin, "other", "bob", "paused")
    with pytest.raises(AccessDenied, match="another tenant"):
        await engine.payments.revoke(tenant_admin, "other", "bob")
    with pytest.raises(AccessDenied, match="another tenant"):
        await engine.payments.rotate_payment_key(tenant_admin, "other", "bob", "r")
    with pytest.raises(AccessDenied, match="another tenant"):
        await engine.payments.list_actors(tenant_admin, tenant_id="other")
    with pytest.raises(AccessDenied, match="another tenant"):
        await desk.grant(tenant_admin, "other")
    with pytest.raises(AccessDenied, match="clearinghouse admin"):
        await engine.payments.top_up_grant(
            tenant_admin, "default", TopUpRequest(amount=five, operation_ref="g")
        )
    with pytest.raises(AccessDenied, match="clearinghouse admin"):
        await desk.onboard(tenant_admin, "mine", five, operation_ref="o")
    with pytest.raises(AccessDenied, match="clearinghouse admin"):
        await desk.set_status(tenant_admin, "default", "paused", operation_ref="s")
    with pytest.raises(AccessDenied, match="tenant_admin key"):
        await keys.issue(tenant_admin, kind="tenant_admin", actor_id="x", operation_ref="k")
    assert manager.calls == calls
    assert [actor.actor_id for actor in await engine.payments.list_actors(tenant_admin)] == []  # its own
    assert (await desk.grant(tenant_admin, "default")).tenant_id == "default"


# -- records from before a tenant's id was its grant's id -----------------------------------------------


async def test_an_allocation_allotted_from_another_grant_before_165_stays_on_that_grant(
    make_engine: EngineFactory, admin: ActorContext, manager: FakeAllocationManager, store: SqlStore
) -> None:
    """A deployment from before #165: the tenant has a free name (``default``), and its actors were
    allotted from a grant with another id. Nothing rewrites those records. The allocation is the
    provider's, which keeps it on its own grant, so every command on it still works and none names a
    grant. A new allocation is never allotted from a grant the record does not name: that is refused."""
    manager.add_grant("grant_before", total=10)
    engine = await make_engine()
    five = Money(amount=Decimal(5), currency="USD")

    def allot_alice(tenant_id: str, ref: str) -> AllotRequest:
        return AllotRequest(tenant_id=tenant_id, actor_id="alice", amount=five, operation_ref=ref)

    alice = await engine.payments.allot(admin, allot_alice("grant_before", "before"))
    # the record as a release before #165 left it: the tenant's free name, and the grant beside it
    async with store.engine.begin() as conn:
        await conn.execute(update(t.actors).where(t.actors.c.actor_id == "alice").values(tenant_id="default"))
    assert (await store.get_actor("default", "alice")) == alice.model_copy(update={"tenant_id": "default"})
    topped = await engine.payments.top_up(
        admin, "default", "alice", TopUpRequest(amount=five, operation_ref="t")
    )
    assert topped.grant_id == "grant_before"
    assert manager.unallocated("grant_before") == Decimal(0)  # 5 and 5: from its own grant
    assert manager.unallocated("default") == Decimal(1000)  # never from the grant its tenant's id names
    assert (await engine.payments.set_status(admin, "default", "alice", "paused")).status == "paused"
    assert (
        await engine.payments.rotate_payment_key(admin, "default", "alice", "r")
    ).grant_id == "grant_before"
    assert (await engine.payments.revoke(admin, "default", "alice")).status == "revoked"
    assert manager.unallocated("grant_before") == Decimal(10)  # returned to its own grant
    calls = list(manager.calls)
    with pytest.raises(InvalidRequest, match="never moved to another grant"):
        await engine.payments.allot(admin, allot_alice("default", "again"))
    assert manager.calls == calls and manager.unallocated("default") == Decimal(1000)
    assert await store.get_operation("default", "operator", "again") is None  # refused, and free again
    # the grant is still reached by its own id, as the tenant it is
    await engine.payments.top_up_grant(admin, "grant_before", TopUpRequest(amount=five, operation_ref="g"))
    assert manager.unallocated("grant_before") == Decimal(15)


async def test_the_tenant_desk_refuses_in_process_what_a_body_would_not_parse(
    make_engine: EngineFactory, admin: ActorContext, manager: FakeAllocationManager
) -> None:
    """An embedded caller passes no REST body, so the desk holds the same rules itself, before the
    ledger and the provider."""
    engine = await make_engine()
    desk = engine.payments.tenants
    assert desk is not None
    five = Money(amount=Decimal(5), currency="USD")
    with pytest.raises(InvalidRequest, match="actor id"):
        await desk.onboard(admin, "ops", five, operation_ref="o-1", admin_actor_id="")
    with pytest.raises(InvalidRequest, match="never closed"):
        await desk.set_status(admin, "default", "closed", operation_ref="s-1")  # type: ignore[arg-type]
    assert manager.calls == []
    assert await engine.store.get_operation("default", "operator", "o-1") is None


async def test_the_fake_provider_keeps_each_grants_balance_as_batteries_does() -> None:
    """What builders' tests see through ``livepeer_builder.testing``: an unknown grant and an amount the
    grant cannot cover are refused, a revoke returns what was not spent, and a paused grant still takes
    an allotment."""
    provider = FakeAllocationManager()
    provider.add_grant("t-1", total=10)
    five, six = Money(amount=Decimal(5), currency="USD"), Money(amount=Decimal(6), currency="USD")
    with pytest.raises(ProviderError, match="404"):
        await provider.create_key_for_grant("nobody", "n", five, "k0")
    first = await provider.create_key_for_grant("t-1", "n", five, "k1")
    with pytest.raises(ProviderError, match="409: insufficient unallocated grant balance"):
        await provider.create_key_for_grant("t-1", "n", six, "k2")
    with pytest.raises(ProviderError, match="insufficient unallocated grant balance"):
        await provider.top_up_allocation(first.allocation_id, six, "k3")
    assert (await provider.create_key_for_grant("t-1", "n", five, "k1")) == first  # a replay takes nothing
    assert (await provider.read_grant("t-1")).unallocated.amount == 5
    await provider.set_grant_status("t-1", "paused")
    second = await provider.create_key_for_grant("t-1", "n", five, "k4")
    assert (await provider.read_grant("t-1")).status == "paused" and provider.unallocated("t-1") == 0
    provider.allocations[second.allocation_id].spent = Decimal(2)
    await provider.revoke_allocation(second.allocation_id)
    await provider.revoke_allocation(second.allocation_id)  # a repeat returns nothing more
    assert provider.unallocated("t-1") == 3
    provider.grant_page_size = 1
    made = await provider.create_grant("made", five)
    assert [grant.id for grant in await provider.list_grants()] == ["t-1", made.id]
    assert provider.held == {"t-1", made.id}
    assert [tenant.unallocated.amount for tenant in await provider.grant_balances()] == [3, 5]


async def test_the_fake_provider_states_what_each_grants_allocations_still_hold() -> None:
    """As Batteries' figures give it: each allocation's amount less what it spent, and nothing for one
    that spent more, which a builder's test sets on the fake's allocation. A revoked one holds nothing."""
    provider = FakeAllocationManager()
    provider.add_grant("t-1", total=100)
    provider.add_grant("idle", total=7)

    def amount(value: int) -> Money:
        return Money(amount=Decimal(value), currency="USD")

    overdrawn = await provider.create_key_for_grant("t-1", "n", amount(30), "k1")
    await provider.create_key_for_grant("t-1", "n", amount(20), "k2")
    revoked = await provider.create_key_for_grant("t-1", "n", amount(10), "k3")
    provider.allocations[overdrawn.allocation_id].spent = Decimal(40)  # charged after it was authorized
    provider.allocations[revoked.allocation_id].spent = Decimal(4)
    await provider.revoke_allocation(revoked.allocation_id)
    provider.allocations[revoked.allocation_id].spent = Decimal(5)  # a late charge
    read = await provider.read_grant("t-1")
    assert (read.unallocated.amount, read.allocated_unspent.amount, read.spent.amount) == (46, 20, 45)
    assert [tenant.allocated_unspent for tenant in await provider.grant_balances()] == [amount(20), amount(0)]
