"""A tenant's own routes take the tenant from the credential (D87 point 3, as Mike amended it on 19 November
2026 after the reviewer's comment on #175).

A tenant admin or auditor credential identifies one tenant, so the routes it calls name none: its grant is
``GET /v1/clearinghouse/grant``, its usage ``GET /v1/clearinghouse/usage``, its actors, their allocations
and its access keys under ``/v1/clearinghouse/actors`` and ``/v1/clearinghouse/keys``. These are the
"short paths". The clearinghouse admin, who acts on any tenant, names the tenant in the path, under
``/v1/clearinghouse/tenants/{tenant_id}``. Both forms call the same service with the tenant they
resolved, so their answers are the same.

One rule on every listener: a short path takes the tenant kinds only, and a clearinghouse admin is
refused there, for it has no tenant of its own; a path that names a tenant takes a clearinghouse admin
only, and a tenant credential is refused there, its own tenant's path included. So no route lets one
tenant's credential name another tenant: on a short path an id of another tenant's actor or key reads as
missing, as the services already answer. The public listener serves the short paths, the private
listener the paths that name a tenant, and one listener both.

Two tenants on one engine with one listener (``default`` and ``other``), each with an actor ``alice`` of
its own, so an answer that mixed the two would show.
"""

import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute

from livepeer_builder import BuilderEngine
from livepeer_builder.contracts import ActorContext, AllotRequest, Money, UsageRow
from livepeer_builder.http import create_app
from livepeer_builder.http.app import shared_app
from livepeer_builder.http.clearinghouse import mount_named_tenant_routes, mount_tenant_routes
from livepeer_builder.testing import FakeAllocationManager, FakeUsageSource
from tests.conftest import EngineFactory
from tests.unit.test_admin_route_sweep import SAMPLE_BODIES, everything_stored

SHORT = "/v1/clearinghouse"
NAMED = "/v1/clearinghouse/tenants/{tenant_id}"
TENANTS = ("default", "other")
# Every short path, as Mike's decision lists them: the tenant routes with ``tenants/{tenant_id}`` taken out.
SHORT_PATHS = {
    ("GET", SHORT + "/grant"),
    ("GET", SHORT + "/usage"),
    ("GET", SHORT + "/actors"),
    ("GET", SHORT + "/actors/{actor_id}/allocation"),
    ("POST", SHORT + "/actors/{actor_id}/allocation"),
    ("POST", SHORT + "/actors/{actor_id}/allocation/top-up"),
    ("POST", SHORT + "/actors/{actor_id}/allocation/status"),
    ("POST", SHORT + "/actors/{actor_id}/allocation/revoke"),
    ("POST", SHORT + "/actors/{actor_id}/allocation/rotate-payment-key"),
    ("GET", SHORT + "/keys"),
    ("POST", SHORT + "/keys"),
    ("GET", SHORT + "/keys/{key_id}"),
    ("POST", SHORT + "/keys/{key_id}/rotate"),
    ("POST", SHORT + "/keys/{key_id}/revoke"),
}
WRITES = {"POST"}
READS = {"read_grant", "read_allocation"}  # what a read asks the payment provider
USD = {"amount": "1", "currency": "USD"}


def usd(amount: str) -> Money:
    return Money(amount=Decimal(amount), currency="USD")


def bearer(token: str | None) -> dict[str, str]:
    assert token is not None
    return {"Authorization": f"Bearer {token}"}


def routes_of(mount: Any, engine: BuilderEngine) -> dict[tuple[str, str], APIRoute]:
    """The routes one group mounts, by itself."""
    app = shared_app(engine, manage_lifecycle=False)
    mount(app, engine)
    return {
        (method, route.path): route
        for route in app.routes
        if isinstance(route, APIRoute) and route.path.startswith(SHORT)
        for method in route.methods
    }


def body_for(route: APIRoute) -> dict[str, Any] | None:
    params = route.dependant.body_params
    if not params:
        return None
    name = params[0].field_info.annotation.__name__  # type: ignore[union-attr]
    assert name in SAMPLE_BODIES, f"add a sample {name} to SAMPLE_BODIES for {route.path}"
    return SAMPLE_BODIES[name]


def filled(path: str, params: dict[str, str]) -> str:
    return re.sub(r"\{(\w+)\}", lambda match: params[match.group(1)], path)


@dataclass
class Tenant:
    id: str
    admin: dict[str, str]  # its tenant admin's key
    auditor: dict[str, str]
    admin_key_id: str
    alice: str  # the id of its actor alice's allocation


@dataclass
class Desk:
    engine: BuilderEngine
    app: FastAPI  # the one listener's
    client: httpx.AsyncClient
    clearinghouse_admin: dict[str, str]
    actor: dict[str, str]  # alice of ``default``, an actor key
    tenants: dict[str, Tenant]

    def other_than(self, tenant: Tenant) -> Tenant:
        return next(each for each in self.tenants.values() if each.id != tenant.id)


@pytest.fixture
async def desk(
    make_engine: EngineFactory, admin: ActorContext, manager: FakeAllocationManager, usage: FakeUsageSource
) -> AsyncIterator[Desk]:
    manager.add_grant("other")
    engine = await make_engine()
    assert engine.keys is not None
    tenants: dict[str, Tenant] = {}
    for tenant_id in TENANTS:
        alice = await engine.payments.allot(
            admin,
            AllotRequest(
                tenant_id=tenant_id,
                actor_id="alice",
                amount=usd("5"),
                operation_ref=f"allot:{tenant_id}:alice",
            ),
        )
        desk_key = await engine.keys.issue(
            admin,
            kind="tenant_admin",
            actor_id="desk",
            tenant_id=tenant_id,
            operation_ref=f"desk-{tenant_id}",
        )
        audit = await engine.keys.issue(
            admin, kind="auditor", actor_id="audit", tenant_id=tenant_id, operation_ref=f"audit-{tenant_id}"
        )
        tenants[tenant_id] = Tenant(
            tenant_id, bearer(desk_key.token), bearer(audit.token), desk_key.key.key_id, alice.allocation_id
        )
    bob = AllotRequest(tenant_id="other", actor_id="bob", amount=usd("5"), operation_ref="allot:other:bob")
    await engine.payments.allot(admin, bob)
    actor = await engine.keys.issue(admin, kind="actor", actor_id="alice", operation_ref="actor-alice")
    usage.rows = [_row(number, tenants[TENANTS[number % 2]].alice) for number in range(1, 7)]
    usage.grant_of = {tenant.alice: tenant.id for tenant in tenants.values()}
    app = create_app(engine, manage_lifecycle=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://engine") as client:
        yield Desk(
            engine,
            app,
            client,
            bearer((await engine.keys.bootstrap_admin()).token),
            bearer(actor.token),
            tenants,
        )


def _row(number: int, allocation_id: str) -> UsageRow:
    return UsageRow(
        id=f"row-{number}",
        event_id=None,
        status="applied",
        manifest_id=f"m-{number}",
        allocation_id=allocation_id,
        payment_session_id=None,
        request_id=None,
        pipeline=None,
        computed_fee_eth=None,
        computed_fee_usd=Decimal("0.5"),
        currency="usd",
        created_at=datetime(2026, 11, 19, 12, number, tzinfo=UTC),
    )


# -- the set ----------------------------------------------------------------------------------------------


async def test_the_short_paths_are_the_tenant_routes_with_the_tenant_taken_out(desk: Desk) -> None:
    """The decision's list, and nothing else: each short path is a path that names a tenant, without
    ``tenants/{tenant_id}``, and each such path of a tenant has its short path. A route added to one form
    only fails here."""
    short = routes_of(mount_tenant_routes, desk.engine).keys()
    named = routes_of(mount_named_tenant_routes, desk.engine).keys()
    assert short == SHORT_PATHS
    assert {(method, path.replace(NAMED, SHORT)) for method, path in named} == short
    assert all("{tenant_id}" in path for _, path in named)
    assert not [path for _, path in short if "tenant" in path]


# -- a tenant admin: its own tenant, and nothing of another ------------------------------------------------


@pytest.mark.parametrize("tenant_id", TENANTS)
async def test_each_short_read_answers_a_tenant_admin_with_its_own_tenant_and_nothing_of_another(
    desk: Desk, tenant_id: str
) -> None:
    """The same answer the clearinghouse admin gets at the path that names the tenant: both forms call
    the same service with the tenant they resolved."""
    tenant, client = desk.tenants[tenant_id], desk.client
    other = desk.other_than(tenant)
    named = NAMED.replace("{tenant_id}", tenant.id)

    async def both(rest: str, **params: str) -> Any:
        mine = await client.get(SHORT + rest, params=params, headers=tenant.admin)
        assert mine.status_code == 200, (rest, mine.text)
        theirs = await client.get(named + rest, params=params, headers=desk.clearinghouse_admin)
        assert theirs.json() == mine.json(), rest
        return mine.json()

    assert (await both("/grant"))["tenant_id"] == tenant.id
    actors = await both("/actors")
    assert {row["tenant_id"] for row in actors} == {tenant.id}
    assert {row["actor_id"] for row in actors} == ({"alice", "bob"} if tenant.id == "other" else {"alice"})
    allocation = await both("/actors/alice/allocation")
    assert (allocation["tenant_id"], allocation["allocation_id"]) == (tenant.id, tenant.alice)
    keys = await both("/keys")
    assert {key["tenant_id"] for key in keys} == {tenant.id}
    assert (await both(f"/keys/{tenant.admin_key_id}"))["tenant_id"] == tenant.id
    usage = await both("/usage", limit="100")
    assert usage["items"] and {row["allocation_id"] for row in usage["items"]} == {tenant.alice}

    # what is another tenant's has no path here: its key and its actor read as missing
    missing = await client.get(f"{SHORT}/keys/{other.admin_key_id}", headers=tenant.admin)
    assert missing.status_code == 404
    if tenant.id == "default":
        assert (await client.get(SHORT + "/actors/bob/allocation", headers=tenant.admin)).status_code == 404


@pytest.mark.parametrize("tenant_id", TENANTS)
async def test_each_short_write_of_a_tenant_admin_acts_on_its_own_tenant_only(
    desk: Desk, tenant_id: str, manager: FakeAllocationManager
) -> None:
    tenant, client = desk.tenants[tenant_id], desk.client
    other = desk.other_than(tenant)
    other_alice = manager.allocations[other.alice]
    before = (other_alice.status, other_alice.granted)
    allocation = SHORT + "/actors/alice/allocation"

    carol = await client.post(
        SHORT + "/actors/carol/allocation",
        json={"amount": USD, "operation_ref": "carol"},
        headers=tenant.admin,
    )
    assert carol.status_code == 201, carol.text
    assert carol.json()["tenant_id"] == tenant.id
    assert manager.allocations[carol.json()["allocation_id"]].grant_id == tenant.id
    assert await desk.engine.store.get_actor(other.id, "carol") is None

    topped = await client.post(
        allocation + "/top-up", json={"amount": USD, "operation_ref": "more"}, headers=tenant.admin
    )
    assert topped.status_code == 200 and topped.json()["allocation_id"] == tenant.alice
    assert manager.allocations[tenant.alice].granted == Decimal(6)
    paused = await client.post(allocation + "/status", json={"status": "paused"}, headers=tenant.admin)
    assert paused.status_code == 200 and manager.allocations[tenant.alice].status == "paused"
    rotated = await client.post(
        allocation + "/rotate-payment-key", json={"operation_ref": "rotate"}, headers=tenant.admin
    )
    assert rotated.status_code == 200 and rotated.json()["tenant_id"] == tenant.id
    revoked = await client.post(allocation + "/revoke", headers=tenant.admin)
    assert revoked.status_code == 200 and manager.allocations[tenant.alice].status == "revoked"
    assert (other_alice.status, other_alice.granted) == before  # the other tenant's alice is untouched

    issued = await client.post(
        SHORT + "/keys", json={"actor_id": "watcher", "operation_ref": "watch"}, headers=tenant.admin
    )
    assert issued.status_code == 201 and issued.json()["key"]["tenant_id"] == tenant.id
    key_id = issued.json()["key"]["key_id"]
    renewed = await client.post(
        f"{SHORT}/keys/{key_id}/rotate", json={"operation_ref": "renew"}, headers=tenant.admin
    )
    assert renewed.status_code == 201 and renewed.json()["key"]["tenant_id"] == tenant.id
    renewed_id = renewed.json()["key"]["key_id"]
    assert (await client.post(f"{SHORT}/keys/{renewed_id}/revoke", headers=tenant.admin)).status_code == 204

    # another tenant's key, by its id: missing, and left as it was
    theirs = await desk.engine.store.get_access_key(other.admin_key_id)
    for action, body in (("rotate", {"operation_ref": "theirs"}), ("revoke", None)):
        answer = await client.post(
            f"{SHORT}/keys/{other.admin_key_id}/{action}", json=body, headers=tenant.admin
        )
        assert answer.status_code == 400 and "no active key" in answer.text, action
    assert await desk.engine.store.get_access_key(other.admin_key_id) == theirs


# -- an auditor reads; a clearinghouse admin and an actor are refused --------------------------------------


async def test_an_auditor_reads_every_short_path_of_its_tenant_and_writes_none(
    desk: Desk, manager: FakeAllocationManager
) -> None:
    other = desk.tenants["other"]
    params = {"actor_id": "bob", "key_id": other.admin_key_id}
    stored_before, calls_before = await everything_stored(desk.engine), list(manager.calls)
    wrong: list[str] = []
    routes = routes_of(mount_tenant_routes, desk.engine)
    assert routes.keys() == SHORT_PATHS
    for (method, path), route in sorted(routes.items()):
        answer = await desk.client.request(
            method, filled(path, params), json=body_for(route), headers=other.auditor
        )
        expected = 403 if method in WRITES else 200
        if answer.status_code != expected:
            wrong.append(f"{method} {path}: {answer.status_code}, expected {expected}: {answer.text[:100]}")
        elif method == "GET" and "tenant_id" in answer.text:
            assert '"tenant_id":"default"' not in answer.text, path
    assert not wrong, "\n".join(wrong)
    asked = manager.calls[len(calls_before) :]
    assert asked and set(asked) <= READS, asked  # its reads reached the provider; no write did
    assert await everything_stored(desk.engine) == stored_before


@pytest.mark.parametrize(
    ("who", "reason"),
    [
        ("clearinghouse_admin", "no tenant of its own"),
        ("actor", "tenant admin or auditor credential"),
    ],
)
async def test_a_credential_of_no_tenant_is_refused_on_every_short_path(
    desk: Desk, manager: FakeAllocationManager, who: str, reason: str
) -> None:
    """A clearinghouse admin is an admin of every tenant and of none in particular, so a path that names
    no tenant has none for it: it names the tenant instead. An actor acts for itself, not for a tenant."""
    headers = desk.clearinghouse_admin if who == "clearinghouse_admin" else desk.actor
    params = {"actor_id": "alice", "key_id": desk.tenants["default"].admin_key_id}
    stored_before, calls_before = await everything_stored(desk.engine), list(manager.calls)
    wrong: list[str] = []
    routes = routes_of(mount_tenant_routes, desk.engine)
    assert routes.keys() == SHORT_PATHS
    for (method, path), route in sorted(routes.items()):
        answer = await desk.client.request(
            method, filled(path, params), json=body_for(route), headers=headers
        )
        if answer.status_code != 403 or reason not in answer.text:
            wrong.append(f"{method} {path}: {answer.status_code} {answer.text[:120]}")
    assert not wrong, f"a short path took a {who} credential:\n" + "\n".join(wrong)
    assert manager.calls == calls_before
    assert await everything_stored(desk.engine) == stored_before


# -- the paths that name a tenant: the clearinghouse admin's ----------------------------------------------


@pytest.mark.parametrize(
    "who", ["default tenant admin", "other tenant admin", "default auditor", "other auditor", "actor"]
)
async def test_every_path_that_names_a_tenant_refuses_a_tenant_credential_its_own_tenant_included(
    desk: Desk, manager: FakeAllocationManager, who: str
) -> None:
    """The replacement of #165's sweep, "another tenant in the path is 403": a tenant credential is 403 on
    every path that names a tenant, the tenant's own included, before any lookup or provider call. The
    paths come from the app, so one added later is swept."""
    tenant_id, kind = who.split()[0], who.split(maxsplit=1)[-1]
    headers = desk.actor if who == "actor" else getattr(desk.tenants[tenant_id], kind.split()[-1])
    params = {"tenant_id": "default", "actor_id": "alice", "key_id": desk.tenants["default"].admin_key_id}
    named = {
        (method, route.path): route
        for route in desk.app.routes
        if isinstance(route, APIRoute) and "{tenant_id}" in route.path
        for method in route.methods
    }
    assert len(named) >= len(SHORT_PATHS) + 2  # the tenant's routes, a grant's top-up and a tenant's status
    stored_before, calls_before = await everything_stored(desk.engine), list(manager.calls)
    wrong: list[str] = []
    for (method, path), route in sorted(named.items()):
        answer = await desk.client.request(
            method, filled(path, params), json=body_for(route), headers=headers
        )
        if answer.status_code != 403:
            wrong.append(f"{method} {path}: {answer.status_code} {answer.text[:120]}")
    assert not wrong, f"a path that names a tenant took a {who} credential:\n" + "\n".join(wrong)
    assert manager.calls == calls_before
    assert await everything_stored(desk.engine) == stored_before


async def test_a_tenant_credential_on_a_tenants_named_route_is_told_where_its_own_are(desk: Desk) -> None:
    refused = await desk.client.get(SHORT + "/tenants/default/actors", headers=desk.tenants["default"].admin)
    assert refused.status_code == 403
    assert "clearinghouse admin" in refused.json()["error"] and "/v1/clearinghouse" in refused.json()["error"]


async def test_the_clearinghouse_admin_acts_on_the_tenant_it_names(
    desk: Desk, manager: FakeAllocationManager
) -> None:
    """Each tenant's routes at the path that names it, for the tenant it names and no other."""
    client, admin = desk.client, desk.clearinghouse_admin
    for tenant in desk.tenants.values():
        named = NAMED.replace("{tenant_id}", tenant.id)
        assert (await client.get(named + "/grant", headers=admin)).json()["tenant_id"] == tenant.id
        actors = (await client.get(named + "/actors", headers=admin)).json()
        assert {row["tenant_id"] for row in actors} == {tenant.id}
        allotted = await client.post(
            named + "/actors/dave/allocation",
            json={"amount": USD, "operation_ref": f"dave-{tenant.id}"},
            headers=admin,
        )
        assert allotted.status_code == 201 and allotted.json()["tenant_id"] == tenant.id
        assert manager.allocations[allotted.json()["allocation_id"]].grant_id == tenant.id
        issued = await client.post(
            named + "/keys",
            json={"kind": "tenant_admin", "actor_id": "second", "operation_ref": f"second-{tenant.id}"},
            headers=admin,
        )
        assert issued.status_code == 201 and issued.json()["key"]["tenant_id"] == tenant.id
        usage = (await client.get(named + "/usage", headers=admin)).json()
        assert {row["allocation_id"] for row in usage["items"]} == {tenant.alice}


# -- where each form is served ---------------------------------------------------------------------------


async def test_each_listener_serves_its_own_form_and_its_own_schema_says_so(
    make_engine: EngineFactory,
) -> None:
    """The public listener serves the short paths and no path that names a tenant; the private one the
    paths that name a tenant and no short path; one listener both. Each app's own ``/openapi.json`` lists
    what it serves and nothing else."""
    engine = await make_engine()
    named = routes_of(mount_named_tenant_routes, engine).keys()
    for listener, has, lacks in (
        ("public", SHORT_PATHS, named),
        ("private", named, SHORT_PATHS),
        ("only", SHORT_PATHS | named, set()),
    ):
        app = create_app(engine, manage_lifecycle=False, listener=listener)  # type: ignore[arg-type]
        served = {
            (method, route.path)
            for route in app.routes
            if isinstance(route, APIRoute)
            for method in route.methods
        }
        assert served >= has and not served & lacks, listener
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://e") as client:
            schema = (await client.get("/openapi.json")).json()
        published = {
            (method.upper(), path) for path, methods in schema["paths"].items() for method in methods
        }
        assert published >= has and not published & lacks, listener
