"""The work routes and the clearinghouse routes are two sets, and each mounts by itself (D84, #164).

The engine has two halves. The work half runs jobs and records their cost; the clearinghouse half holds
tenants, grants, allocations and payment keys. Each half's routes are one unit: ``mount_work_routes`` and
``mount_clearinghouse_routes`` add them to an app that ``shared_app`` built, which carries what both
halves share (health, the error answers, the request limit). ``create_app`` mounts both.

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
from livepeer_builder.http.clearinghouse import mount_clearinghouse_routes
from livepeer_builder.http.work import mount_work_routes
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
CLEARINGHOUSE = {
    ("GET", "/v1/clearinghouse/actors"),
    ("GET", ALLOCATION),
    ("POST", ALLOCATION),
    ("POST", ALLOCATION + "/top-up"),
    ("POST", ALLOCATION + "/status"),
    ("POST", ALLOCATION + "/revoke"),
    ("POST", ALLOCATION + "/rotate-payment-key"),
    ("POST", "/v1/clearinghouse/grant/top-up"),
}
GRANT_DESK = {("GET", "/v1/clearinghouse/grants"), ("POST", "/v1/clearinghouse/grants")}  # only when it is on


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
    engine = await make_engine()  # API keys and the grant desk on, so every route exists
    assert served(create_app(engine, manage_lifecycle=False)) == (
        HEALTH | WORK | KEYS | CLEARINGHOUSE | GRANT_DESK
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
        assert (await client.get("/v1/clearinghouse/actors", headers=admin)).status_code == 404
        allocation = ALLOCATION.format(tenant_id="default", actor_id="alice")
        assert (await client.get(allocation, headers=admin)).status_code == 404


async def test_the_clearinghouse_set_mounts_by_itself(make_engine: EngineFactory) -> None:
    engine = await make_engine()
    app = shared_app(engine, manage_lifecycle=False)
    mount_clearinghouse_routes(app, engine)
    assert served(app) == HEALTH | CLEARINGHOUSE | GRANT_DESK
    assert all(path.startswith("/v1/clearinghouse/") for _, path in served(app) - HEALTH)
    admin, alice = await keys_of(engine)
    async with client_of(app) as client:
        assert (await client.get("/readyz")).status_code == 200
        assert (await client.get("/v1/clearinghouse/actors", headers=admin)).status_code == 200
        assert (await client.get("/v1/clearinghouse/actors", headers=alice)).status_code == 403
        assert (await client.get("/v1/clearinghouse/actors")).status_code == 401
        # no work route is served, whoever asks
        for method, path in (("GET", "/v1/jobs"), ("GET", "/v1/offerings"), ("GET", "/v1/keys")):
            assert (await client.request(method, path, headers=admin)).status_code == 404, path
        assert (await client.post("/v1/costs/sync", headers=admin)).status_code == 404


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
    """Engine API keys off: the work set has no key route. The grant desk off: the clearinghouse set has
    no grant list or creation. Neither changes the other set."""
    off = EngineSupply(api_keys=None, authenticators=(HeaderAuthenticator(),), grants_manager=None)
    engine = await make_engine(supply=off)
    work = shared_app(engine, manage_lifecycle=False)
    mount_work_routes(work, engine)
    assert served(work) == HEALTH | WORK
    clearinghouse = shared_app(engine, manage_lifecycle=False)
    mount_clearinghouse_routes(clearinghouse, engine)
    assert served(clearinghouse) == HEALTH | CLEARINGHOUSE
