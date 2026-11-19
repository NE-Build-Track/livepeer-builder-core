"""The work routes and the clearinghouse routes are two sets, and each mounts by itself (D84, #164).

The engine has two halves. The work half runs jobs and records their cost; the clearinghouse half holds
tenants, grants, allocations and payment keys. Each half's routes are one unit: ``mount_work_routes`` and
``mount_clearinghouse_routes`` add them to an app that ``shared_app`` built, which carries what both
halves share (health, the error answers, the request limit). ``create_app`` mounts both.

Since #165 every clearinghouse route that names a tenant is under ``/v1/clearinghouse/tenants``; the
runner list a clearinghouse publishes is beside them, at ``/v1/clearinghouse/discovery`` (#166). Since
19 November 2026 (D87 point 3, as amended) a tenant's admin and auditor call the tenant's routes at paths
that name no tenant, and the clearinghouse admin at the paths under ``/v1/clearinghouse/tenants``. The
set is five groups that mount by themselves too: a tenant's own routes (``mount_tenant_routes``), the
same routes naming the tenant (``mount_named_tenant_routes``), the runner list, the signer's two paths,
and what only a clearinghouse admin does (``mount_clearinghouse_admin_routes``). #167 serves the first
on the public listener and the second and the last on the private one.

Reconciliation, cost sync and the engine's own access keys are work routes
(tests/unit/test_work_route_paths.py).

Since #169 the engine's role (``LPB_ROLE``) says which of the two sets an app mounts: both, the work set
alone, or the clearinghouse set alone on a public and a private listener. The last section pins the
exact set of each role.
"""

from dataclasses import dataclass

import httpx
from fastapi import FastAPI
from fastapi.routing import APIRoute

from livepeer_builder import BuilderEngine, EngineSupply
from livepeer_builder.contracts import ActorContext
from livepeer_builder.http import create_app
from livepeer_builder.http.app import shared_app
from livepeer_builder.http.clearinghouse import (
    mount_clearinghouse_admin_routes,
    mount_clearinghouse_routes,
    mount_named_tenant_routes,
    mount_runner_list_routes,
    mount_tenant_routes,
)
from livepeer_builder.http.signing import mount_signing_routes
from livepeer_builder.http.work import mount_work_routes
from livepeer_builder.testing import FakeSigningTransport
from tests.conftest import EngineFactory, allot
from tests.unit.role_engines import in_role
from tests.unit.test_assembly import HeaderAuthenticator

HEALTH = {("GET", "/livez"), ("GET", "/readyz")}
WORK = {
    ("GET", "/v1/offerings"),
    ("GET", "/v1/runners"),
    ("POST", "/v1/jobs"),
    ("GET", "/v1/jobs"),
    ("GET", "/v1/jobs/{job_id}"),
    ("GET", "/v1/jobs/{job_id}/result"),
    ("POST", "/v1/jobs/{job_id}/cancel"),  # a queued async job's cancel (ISS-26)
    ("POST", "/v1/jobs/{job_id}/usage"),
    ("GET", "/v1/jobs/{job_id}/cost"),
    ("GET", "/v1/costs/projections"),
    ("GET", "/v1/costs/summary"),
    ("GET", "/v1/costs/reconciliation"),
    ("POST", "/v1/costs/sync"),
    ("GET", "/v1/allowance"),
    ("GET", "/v1/events"),
    ("POST", "/v1/sessions"),
}
KEYS = {  # the engine's own access keys: in the work set, and only when engine API keys are on
    ("POST", "/v1/keys"),
    ("GET", "/v1/keys"),
    ("GET", "/v1/keys/{key_id}"),
    ("POST", "/v1/keys/{key_id}/rotate"),
    ("POST", "/v1/keys/{key_id}/revoke"),
}
OWN = "/v1/clearinghouse"  # a tenant's own routes: the tenant is the credential's (19 November 2026)
TENANTS = OWN + "/tenants"
TENANT = TENANTS + "/{tenant_id}"  # the same routes, with the tenant a clearinghouse admin names
ACTORS = TENANT + "/actors"
ALLOCATION = ACTORS + "/{actor_id}/allocation"


@dataclass(frozen=True)
class OfATenant:
    """A tenant's routes under one prefix: always its actors and their allocations, and three optional
    groups (its access keys, its grant and its usage)."""

    routes: frozenset[tuple[str, str]]
    keys: frozenset[tuple[str, str]]  # only when engine API keys are on
    grant: frozenset[tuple[str, str]]  # only when the tenant desk is on
    usage: frozenset[tuple[str, str]]  # only with a usage source that reads one tenant's records (#166)

    @property
    def optional(self) -> frozenset[tuple[str, str]]:
        return self.keys | self.grant | self.usage

    @property
    def every(self) -> frozenset[tuple[str, str]]:
        return self.routes | self.optional


def of_a_tenant(prefix: str) -> OfATenant:
    allocation = prefix + "/actors/{actor_id}/allocation"
    return OfATenant(
        routes=frozenset(
            {
                ("GET", prefix + "/actors"),
                ("GET", allocation),
                ("POST", allocation),
                ("POST", allocation + "/top-up"),
                ("POST", allocation + "/status"),
                ("POST", allocation + "/revoke"),
                ("POST", allocation + "/rotate-payment-key"),
            }
        ),
        keys=frozenset(
            {
                ("POST", prefix + "/keys"),
                ("GET", prefix + "/keys"),
                ("GET", prefix + "/keys/{key_id}"),
                ("POST", prefix + "/keys/{key_id}/rotate"),
                ("POST", prefix + "/keys/{key_id}/revoke"),
            }
        ),
        grant=frozenset({("GET", prefix + "/grant")}),
        usage=frozenset({("GET", prefix + "/usage")}),
    )


NAMED = of_a_tenant(TENANT)  # what a clearinghouse admin calls, naming the tenant
OWN_TENANT = of_a_tenant(OWN)  # what a tenant admin or an auditor calls, for its own tenant
# the runner list a clearinghouse publishes (#166): only when the discovery source holds its document,
# as the HTTP reader does
RUNNER_LIST = {("GET", "/v1/clearinghouse/discovery")}
# The signer's two paths, at the root, where the gateway SDK calls a signer (D87, #166): only on an engine
# whose runner transport forwards a signer call. The shared test engine's scripted transport does not.
SIGNING = {("POST", "/sign-orchestrator-info"), ("POST", "/generate-live-payment")}
# What only a clearinghouse admin does: a private listener's routes (#167).
ADMIN_ROUTES = {("POST", TENANT + "/grant/top-up")}
TENANT_DESK = {("POST", TENANTS), ("GET", TENANTS), ("POST", TENANT + "/status")}  # only when it is on
# the wallet report (#167): only when the grants manager reports the escrow and the transport can ask the
# signer, as the packaged ones do. A clearinghouse admin's, so in the private listener's group.
WALLET = {("GET", "/v1/clearinghouse/wallet")}
ADMIN_SET = ADMIN_ROUTES | TENANT_DESK | WALLET  # what only a clearinghouse admin does
CLEARINGHOUSE = NAMED.routes | OWN_TENANT.routes | ADMIN_ROUTES
OPTIONAL = NAMED.optional | OWN_TENANT.optional | TENANT_DESK | RUNNER_LIST | WALLET
# Gone since #165, with no alias: a tenant's id is its grant's id, and the grant is the tenant's.
REMOVED = [
    ("GET", "/v1/clearinghouse/grants"),
    ("POST", "/v1/clearinghouse/grants"),
    ("POST", "/v1/clearinghouse/grant/top-up"),
]


def served(app: FastAPI) -> set[tuple[str, str]]:
    """Every (method, path) the app's own routes answer. FastAPI's schema and docs pages are not routes
    of the engine."""
    return {
        (method, route.path)
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in route.methods
    }


def client_of(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://engine")


async def keys_of(engine: BuilderEngine) -> tuple[dict[str, str], dict[str, str]]:
    """An admin's and an actor's Authorization header."""
    assert engine.keys is not None
    operator = ActorContext(actor_id="operator", credential_kind="clearinghouse_admin")
    admin = await engine.keys.bootstrap_admin()
    alice = await engine.keys.issue(operator, kind="actor", actor_id="alice", operation_ref="issue-alice")
    return {"Authorization": f"Bearer {admin.token}"}, {"Authorization": f"Bearer {alice.token}"}


async def test_the_app_mounts_both_sets_and_nothing_else(make_engine: EngineFactory) -> None:
    engine = await make_engine()  # API keys and the tenant desk on, so every route exists
    assert served(create_app(engine, manage_lifecycle=False)) == (
        HEALTH | WORK | KEYS | CLEARINGHOUSE | OPTIONAL
    )


async def test_the_work_set_mounts_by_itself(make_engine: EngineFactory) -> None:
    engine = await make_engine()
    app = shared_app(engine, manage_lifecycle=False)
    mount_work_routes(app, engine)
    assert served(app) == HEALTH | WORK | KEYS
    assert not [path for _, path in served(app) if path.startswith("/v1/clearinghouse")]
    admin, alice = await keys_of(engine)
    async with client_of(app) as client:
        assert (await client.get("/readyz")).status_code == 200
        assert (await client.get("/v1/jobs", headers=alice)).status_code == 200
        assert (await client.get("/v1/keys", headers=admin)).status_code == 200
        assert (await client.post("/v1/costs/sync", headers=admin)).status_code == 200
        assert (await client.get("/v1/jobs")).status_code == 401  # the shared answers: a challenge
        # no clearinghouse route is served, whoever asks
        assert (await client.get(ACTORS.format(tenant_id="default"), headers=admin)).status_code == 404
        allocation = ALLOCATION.format(tenant_id="default", actor_id="alice")
        assert (await client.get(allocation, headers=admin)).status_code == 404


async def test_the_clearinghouse_set_mounts_by_itself(make_engine: EngineFactory) -> None:
    engine = await make_engine()
    app = shared_app(engine, manage_lifecycle=False)
    mount_clearinghouse_routes(app, engine)
    assert served(app) == HEALTH | CLEARINGHOUSE | OPTIONAL
    assert all(path.startswith("/v1/clearinghouse/") for _, path in served(app) - HEALTH)
    # The runner list is every tenant's and the wallet is the clearinghouse's: neither names a tenant. A
    # tenant's own routes name none either: the tenant is the credential's. Every other route names one.
    named = served(app) - HEALTH - RUNNER_LIST - WALLET - OWN_TENANT.every
    assert all(path.startswith("/v1/clearinghouse/tenants") for _, path in named)
    assert not [path for _, path in OWN_TENANT.every if "tenant" in path]
    admin, alice = await keys_of(engine)
    actors = ACTORS.format(tenant_id="default")
    async with client_of(app) as client:
        assert (await client.get("/readyz")).status_code == 200
        assert (await client.get(actors, headers=admin)).status_code == 200
        assert (await client.get(actors, headers=alice)).status_code == 403
        assert (await client.get(actors)).status_code == 401
        # no work route is served, whoever asks
        for method, path in (("GET", "/v1/jobs"), ("GET", "/v1/offerings"), ("GET", "/v1/keys")):
            assert (await client.request(method, path, headers=admin)).status_code == 404, path
        assert (await client.post("/v1/costs/sync", headers=admin)).status_code == 404


async def test_the_signers_two_paths_are_a_group_of_the_clearinghouse_set_that_mounts_by_itself(
    make_engine: EngineFactory,
) -> None:
    """An engine whose transport forwards a signer call serves the two paths: with the whole app, with
    the clearinghouse set, and from their own mount function, which a listener's table names (#167).
    The work set never has them."""
    engine = await make_engine(supply=EngineSupply(transport=FakeSigningTransport()))
    alone = shared_app(engine, manage_lifecycle=False)
    mount_signing_routes(alone, engine)
    assert served(alone) == HEALTH | SIGNING
    clearinghouse = shared_app(engine, manage_lifecycle=False)
    mount_clearinghouse_routes(clearinghouse, engine)
    assert served(clearinghouse) == HEALTH | CLEARINGHOUSE | OPTIONAL | SIGNING
    assert served(create_app(engine, manage_lifecycle=False)) == (
        HEALTH | WORK | KEYS | CLEARINGHOUSE | OPTIONAL | SIGNING
    )
    work = shared_app(engine, manage_lifecycle=False)
    mount_work_routes(work, engine)
    assert not served(work) & SIGNING
    for mount in (mount_tenant_routes, mount_named_tenant_routes):
        tenants = shared_app(engine, manage_lifecycle=False)
        mount(tenants, engine)
        assert not served(tenants) & SIGNING  # a group of its own: no form of a tenant's routes


async def test_health_is_shared_by_both_halves(make_engine: EngineFactory) -> None:
    """An app with neither set still answers health: it is mounted with what the halves share."""
    engine = await make_engine()
    app = shared_app(engine, manage_lifecycle=False)
    assert served(app) == HEALTH
    async with client_of(app) as client:
        assert (await client.get("/livez")).json() == {"status": "ok"}
        assert (await client.get("/readyz")).json()["ready"] is True


async def test_each_optional_group_is_absent_from_its_own_set_when_it_is_off(
    make_engine: EngineFactory,
) -> None:
    """Engine API keys off: neither set has a key route. The tenant desk off: the clearinghouse set has
    no onboarding, tenant list, grant read or tenant status. The grant's top-up uses the engine's own
    provider credential and stays. No usage source: no tenant's usage is served (#166)."""
    off = EngineSupply(
        api_keys=None, authenticators=(HeaderAuthenticator(),), grants_manager=None, usage_source=None
    )
    engine = await make_engine(supply=off)
    work = shared_app(engine, manage_lifecycle=False)
    mount_work_routes(work, engine)
    assert served(work) == HEALTH | WORK
    clearinghouse = shared_app(engine, manage_lifecycle=False)
    mount_clearinghouse_routes(clearinghouse, engine)
    assert served(clearinghouse) == HEALTH | CLEARINGHOUSE | RUNNER_LIST  # the reader is always there


async def test_the_groups_of_the_clearinghouse_set_mount_by_themselves(make_engine: EngineFactory) -> None:
    """#167 serves the groups on different listeners, so each is one function: a tenant's own routes,
    the same routes with the tenant a clearinghouse admin names, the runner list, what only a
    clearinghouse admin does, and the signer's two paths. Together they are the set, with nothing
    twice."""
    engine = await make_engine(supply=EngineSupply(transport=FakeSigningTransport()))
    groups = {
        mount_tenant_routes: OWN_TENANT.every,
        mount_named_tenant_routes: NAMED.every,
        mount_runner_list_routes: RUNNER_LIST,
        mount_clearinghouse_admin_routes: ADMIN_SET,
        mount_signing_routes: SIGNING,
    }
    for mount, routes in groups.items():
        alone = shared_app(engine, manage_lifecycle=False)
        mount(alone, engine)
        assert served(alone) == HEALTH | routes, mount.__name__
    whole = shared_app(engine, manage_lifecycle=False)
    mount_clearinghouse_routes(whole, engine)
    assert len([route for route in whole.routes if isinstance(route, APIRoute)]) == len(HEALTH) + sum(
        len(routes) for routes in groups.values()
    )
    admin, _ = await keys_of(engine)
    body = {"name": "ops", "amount": {"amount": "5", "currency": "USD"}, "operation_ref": "onboard-1"}
    for mount in (mount_tenant_routes, mount_named_tenant_routes):
        tenants = shared_app(engine, manage_lifecycle=False)
        mount(tenants, engine)
        async with client_of(tenants) as client:  # a tenant's routes onboard nobody, whoever asks
            assert (await client.post(TENANTS, json=body, headers=admin)).status_code in (404, 405)
            assert (await client.get(TENANTS, headers=admin)).status_code in (404, 405)


async def test_the_routes_that_took_a_grant_in_a_body_are_gone(make_engine: EngineFactory) -> None:
    """No alias (D83): no route takes a separate grant id, and the grant is the tenant's."""
    engine = await make_engine()
    admin, _ = await keys_of(engine)
    paths = {path for _, path in served(create_app(engine, manage_lifecycle=False))}
    async with client_of(create_app(engine, manage_lifecycle=False)) as client:
        for method, path in REMOVED:
            assert path not in paths
            assert (await client.request(method, path, json={}, headers=admin)).status_code == 404, path


async def test_the_actor_list_that_took_a_tenant_in_its_query_is_a_tenants_own_and_reads_no_query(
    make_engine: EngineFactory, admin: ActorContext
) -> None:
    """Until #165 ``GET /v1/clearinghouse/actors`` took the tenant in its query string. Since 19 November
    2026 the path is a tenant's own actor list (D87 point 3, as amended): the tenant is the credential's,
    and a ``tenant_id`` in the query is not read, so it names no other tenant."""
    engine = await make_engine()
    await allot(
        engine,
        ActorContext(actor_id="alice"),
        ActorContext(actor_id="mallory", tenant_id="other"),
    )
    assert engine.keys is not None
    desk = await engine.keys.issue(admin, kind="tenant_admin", actor_id="desk", operation_ref="desk")
    async with client_of(create_app(engine, manage_lifecycle=False)) as client:
        own = await client.get(
            OWN + "/actors", params={"tenant_id": "other"}, headers={"Authorization": f"Bearer {desk.token}"}
        )
    assert own.status_code == 200
    assert [(row["tenant_id"], row["actor_id"]) for row in own.json()] == [("default", "alice")]


# -- the role chooses the set (#169) ---------------------------------------------------------------------

# The clearinghouse half's routes on each of two listeners, with every optional group on, beside the
# signer's two paths: a tenant's own routes, which name no tenant, on the public one; the same routes
# naming the tenant, and what only a clearinghouse admin does, on the private one (19 November 2026).
PUBLIC_SET = OWN_TENANT.every | RUNNER_LIST
PRIVATE_SET = NAMED.every | RUNNER_LIST | ADMIN_SET


async def test_the_both_role_mounts_both_sets_as_before(make_engine: EngineFactory) -> None:
    """The default, and what self-hosted payments runs: nothing a deployment served is gone, and
    nothing is new. With a private listener it is the deployment of #167."""
    engine = await in_role(make_engine, "both")
    assert engine.role == "both"
    work = HEALTH | WORK | KEYS
    assert served(create_app(engine, manage_lifecycle=False)) == work | PUBLIC_SET | PRIVATE_SET | SIGNING
    assert (
        served(create_app(engine, manage_lifecycle=False, listener="private")) == work | PRIVATE_SET | SIGNING
    )
    assert (
        served(create_app(engine, manage_lifecycle=False, listener="public")) == work | PUBLIC_SET | SIGNING
    )


async def test_the_work_role_mounts_the_work_set_and_nothing_else(make_engine: EngineFactory) -> None:
    """Health, the work routes and the engine's own access keys. No tenant route, nothing a
    clearinghouse admin does, no wallet, no runner list and no signer path, on any listener."""
    engine = await in_role(make_engine, "work")
    work = HEALTH | WORK | KEYS
    assert served(create_app(engine, manage_lifecycle=False)) == work
    # A work engine may have a private listener too (#167): it has no private group, so both serve the
    # work set, and the listener's rule about kinds still holds (its own admin on the private one).
    assert served(create_app(engine, manage_lifecycle=False, listener="public")) == work
    assert served(create_app(engine, manage_lifecycle=False, listener="private")) == work


async def test_the_clearinghouse_role_mounts_the_clearinghouse_set_on_two_listeners(
    make_engine: EngineFactory,
) -> None:
    """Health and the clearinghouse half. The public listener has a tenant's own routes, the runner list
    and the signer's two paths; the private one has the runner list, the signer's two paths and what a
    clearinghouse admin does, the tenant's routes at the paths that name it among them. No job route, no
    catalog, no cost route, no allowance read and none of the engine's own key routes."""
    engine = await in_role(make_engine, "clearinghouse")
    public = served(create_app(engine, manage_lifecycle=False, listener="public"))
    private = served(create_app(engine, manage_lifecycle=False, listener="private"))
    assert public == HEALTH | PUBLIC_SET | SIGNING
    assert private == HEALTH | PRIVATE_SET | SIGNING
    assert not (private - HEALTH) & (WORK | KEYS)
    under_the_prefix = {path for _, path in private - HEALTH - SIGNING}
    assert all(path.startswith("/v1/clearinghouse/") for path in under_the_prefix)


async def test_the_three_roles_serve_every_route_between_them_and_the_halves_share_health_alone(
    make_engine: EngineFactory,
) -> None:
    whole = served(create_app(await in_role(make_engine, "both"), manage_lifecycle=False))
    work = served(create_app(await in_role(make_engine, "work"), manage_lifecycle=False))
    house = await in_role(make_engine, "clearinghouse")
    clearinghouse = served(create_app(house, manage_lifecycle=False, listener="private")) | served(
        create_app(house, manage_lifecycle=False, listener="public")
    )
    assert work | clearinghouse == whole
    assert work & clearinghouse == HEALTH
