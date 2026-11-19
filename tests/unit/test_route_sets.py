"""The work routes and the clearinghouse routes are two sets, and each mounts by itself (D84, #164).

The engine has two halves. The work half runs jobs and records their cost; the clearinghouse half holds
tenants, grants, allocations and payment keys. Each half's routes are one unit: ``mount_work_routes`` and
``mount_clearinghouse_routes`` add them to an app that ``shared_app`` built, which carries what both
halves share (health, the error answers, the request limit). ``create_app`` mounts both.

Since #165 every clearinghouse route that names a tenant is under ``/v1/clearinghouse/tenants``; the
runner list a clearinghouse publishes is beside them, at ``/v1/clearinghouse/discovery`` (#166). The set
is two groups that mount by themselves too: what a tenant's own admin calls (``mount_tenant_routes``),
and what only a clearinghouse admin does (``mount_clearinghouse_admin_routes``), which #167 serves on a
private listener.

Reconciliation, cost sync and the engine's own access keys are work routes
(tests/unit/test_work_route_paths.py).
"""

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
    mount_tenant_routes,
)
from livepeer_builder.http.signing import mount_signing_routes
from livepeer_builder.http.work import mount_work_routes
from livepeer_builder.testing import FakeSigningTransport
from tests.conftest import EngineFactory
from tests.unit.test_assembly import HeaderAuthenticator

ALLOCATION = "/v1/clearinghouse/tenants/{tenant_id}/actors/{actor_id}/allocation"
HEALTH = {("GET", "/livez"), ("GET", "/readyz")}
WORK = {
    ("GET", "/v1/offerings"),
    ("GET", "/v1/runners"),
    ("POST", "/v1/jobs"),
    ("GET", "/v1/jobs"),
    ("GET", "/v1/jobs/{job_id}"),
    ("GET", "/v1/jobs/{job_id}/result"),
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
TENANTS = "/v1/clearinghouse/tenants"
TENANT = TENANTS + "/{tenant_id}"
ACTORS = TENANT + "/actors"
# What a tenant's own admin or auditor calls, for its own tenant.
TENANT_ROUTES = {
    ("GET", ACTORS),
    ("GET", ALLOCATION),
    ("POST", ALLOCATION),
    ("POST", ALLOCATION + "/top-up"),
    ("POST", ALLOCATION + "/status"),
    ("POST", ALLOCATION + "/revoke"),
    ("POST", ALLOCATION + "/rotate-payment-key"),
}
TENANT_KEYS = {  # a tenant's access keys: only when engine API keys are on
    ("POST", TENANT + "/keys"),
    ("GET", TENANT + "/keys"),
    ("GET", TENANT + "/keys/{key_id}"),
    ("POST", TENANT + "/keys/{key_id}/rotate"),
    ("POST", TENANT + "/keys/{key_id}/revoke"),
}
GRANT_READ = {("GET", TENANT + "/grant")}  # only when the tenant desk is on
# only with a usage source that reads one tenant's records (#166)
USAGE_READ = {("GET", TENANT + "/usage")}
# the runner list a clearinghouse publishes (#166): only when the discovery source holds its document,
# as the HTTP reader does
RUNNER_LIST = {("GET", "/v1/clearinghouse/discovery")}
# The signer's two paths, at the root, where the gateway SDK calls a signer (D87, #166): only on an engine
# whose runner transport forwards a signer call. The shared test engine's scripted transport does not.
SIGNING = {("POST", "/sign-orchestrator-info"), ("POST", "/generate-live-payment")}
# What only a clearinghouse admin does: a private listener's routes (#167).
ADMIN_ROUTES = {("POST", TENANT + "/grant/top-up")}
TENANT_DESK = {("POST", TENANTS), ("GET", TENANTS), ("POST", TENANT + "/status")}  # only when it is on
CLEARINGHOUSE = TENANT_ROUTES | ADMIN_ROUTES
OPTIONAL = TENANT_KEYS | GRANT_READ | TENANT_DESK | USAGE_READ | RUNNER_LIST
# Gone since #165, with no alias: the tenant is in the path, and a tenant's id is its grant's id.
REMOVED = [
    ("GET", "/v1/clearinghouse/actors"),
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
    in_a_tenant = served(app) - HEALTH - RUNNER_LIST  # the list is every tenant's: it names none
    assert all(path.startswith("/v1/clearinghouse/tenants") for _, path in in_a_tenant)
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
    tenants = shared_app(engine, manage_lifecycle=False)
    mount_tenant_routes(tenants, engine)
    assert not served(tenants) & SIGNING  # a group of its own: neither of the two #165 made


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


async def test_the_two_groups_of_the_clearinghouse_set_mount_by_themselves(
    make_engine: EngineFactory,
) -> None:
    """#167 serves what only a clearinghouse admin does on a private listener, so that group is one
    function, and the tenants' own routes are another. Together they are the set, with nothing twice."""
    engine = await make_engine()
    tenants = shared_app(engine, manage_lifecycle=False)
    mount_tenant_routes(tenants, engine)
    assert served(tenants) == HEALTH | TENANT_ROUTES | TENANT_KEYS | GRANT_READ | USAGE_READ | RUNNER_LIST
    private = shared_app(engine, manage_lifecycle=False)
    mount_clearinghouse_admin_routes(private, engine)
    assert served(private) == HEALTH | ADMIN_ROUTES | TENANT_DESK
    both = shared_app(engine, manage_lifecycle=False)
    mount_clearinghouse_routes(both, engine)
    assert len([route for route in both.routes if isinstance(route, APIRoute)]) == len(
        (served(tenants) | served(private)) - HEALTH
    ) + len(HEALTH)
    admin, _ = await keys_of(engine)
    body = {"name": "ops", "amount": {"amount": "5", "currency": "USD"}, "operation_ref": "onboard-1"}
    async with client_of(tenants) as client:  # the tenants' listener onboards nobody, whoever asks
        assert (await client.post(TENANTS, json=body, headers=admin)).status_code in (404, 405)
        assert (await client.get(TENANTS, headers=admin)).status_code in (404, 405)


async def test_the_routes_that_took_a_tenant_or_a_grant_in_a_query_or_a_body_are_gone(
    make_engine: EngineFactory,
) -> None:
    """No alias (D83): the tenant is in the path, and no route takes a separate grant id."""
    engine = await make_engine()
    admin, _ = await keys_of(engine)
    paths = {path for _, path in served(create_app(engine, manage_lifecycle=False))}
    async with client_of(create_app(engine, manage_lifecycle=False)) as client:
        for method, path in REMOVED:
            assert path not in paths
            assert (await client.request(method, path, json={}, headers=admin)).status_code == 404, path
