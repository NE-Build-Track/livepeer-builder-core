"""ISS-23: authorization denial across the whole admin surface, read from the FastAPI route table.

Every admin route answers 403 to an actor key and 401 with no credential, and every admin write answers
403 to an auditor key (a read-only admin, D46). The admin surface is every ``/v1/clearinghouse/*`` route
and, in the work half since #164, an engine's own access keys (``/v1/keys``), reconciliation and cost
sync. The routes come from ``app.routes``, so an admin route added later under one of those paths is swept
without editing this test; one whose body has no sample fails here and says so.

#165: every route that names a tenant in its path answers 403 to the admin of another tenant, before any
lookup or provider call. Those routes come from ``app.routes`` too, so one added later is swept, or fails
here until its body has a sample.
"""

import re
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute

from livepeer_builder import BuilderEngine
from livepeer_builder.contracts import ActorContext, AllotRequest, Money
from livepeer_builder.http import create_app
from livepeer_builder.testing import FakeAllocationManager
from tests.conftest import EngineFactory

WRITES = {"POST", "PUT", "PATCH", "DELETE"}
# Where the admin routes are: the clearinghouse set, and the work set's admin routes.
ADMIN_PATHS = ("/v1/clearinghouse/", "/v1/keys", "/v1/costs/reconciliation", "/v1/costs/sync")
USD = {"amount": "0.01", "currency": "USD"}
# A valid body for each admin body model, so a denial is the policy's answer and never a 422.
SAMPLE_BODIES: dict[str, dict[str, Any]] = {
    "IssueKeyBody": {"actor_id": "mallory", "tenant_id": "default", "operation_ref": "sweep"},
    "RotateKeyBody": {"operation_ref": "sweep"},
    "AllotBody": {"amount": USD, "operation_ref": "sweep"},
    "TopUpRequest": {"amount": USD, "operation_ref": "sweep"},
    "OnboardBody": {"name": "sweep", "amount": USD, "operation_ref": "sweep"},
    "TenantStatusBody": {"status": "paused", "operation_ref": "sweep"},
    "TenantKeyBody": {"actor_id": "mallory", "operation_ref": "sweep"},
    "StatusBody": {"status": "paused"},
    "RotateBody": {"operation_ref": "sweep"},
}


def admin_routes(app: FastAPI) -> list[tuple[str, str, APIRoute]]:
    """(method, path, route) for every admin route the app serves."""
    return [
        (method, route.path, route)
        for route in app.routes
        if isinstance(route, APIRoute) and route.path.startswith(ADMIN_PATHS)
        for method in sorted(route.methods)
    ]


def body_for(route: APIRoute) -> dict[str, Any] | None:
    params = route.dependant.body_params
    if not params:
        return None
    name = params[0].field_info.annotation.__name__  # type: ignore[union-attr]
    assert name in SAMPLE_BODIES, f"add a sample {name} to SAMPLE_BODIES for {route.path}"
    return SAMPLE_BODIES[name]


@pytest.fixture
async def surface(
    make_engine: EngineFactory, admin: ActorContext
) -> AsyncIterator[tuple[FastAPI, httpx.AsyncClient, BuilderEngine, dict[str, str]]]:
    """An engine with keys and the grant desk on, alice allotted, and real ids for every path."""
    engine = await make_engine()
    await engine.payments.allot(
        admin,
        AllotRequest(
            operation_ref="allot:default:alice", tenant_id="default", actor_id="alice",
            amount=Money.model_validate(USD),
        ),
    )  # fmt: skip
    alice = await engine.keys.issue(admin, kind="actor", actor_id="alice", operation_ref="sweep-alice")
    params = {"key_id": alice.key.key_id, "tenant_id": "default", "actor_id": "alice"}
    app = create_app(engine, manage_lifecycle=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://engine") as client:
        yield app, client, engine, params


def filled(path: str, params: dict[str, str]) -> str:
    return re.sub(r"\{(\w+)\}", lambda match: params[match.group(1)], path)


def query_for(route: APIRoute, params: dict[str, str]) -> dict[str, str]:
    """Each query parameter the route requires, from the same real ids, so a denial is never a 422."""
    return {
        field.alias: params[field.alias]
        for field in route.dependant.query_params
        if field.field_info.is_required()
    }


async def test_every_admin_route_is_denied_to_actor_and_auditor_keys_and_needs_a_credential(
    surface: tuple[FastAPI, httpx.AsyncClient, BuilderEngine, dict[str, str]],
    admin: ActorContext,
    manager: FakeAllocationManager,
) -> None:
    app, client, engine, params = surface
    actor = await engine.keys.issue(admin, kind="actor", actor_id="bob", operation_ref="sweep-bob")
    auditor = await engine.keys.issue(admin, kind="auditor", actor_id="dashboard", operation_ref="sweep-aud")
    as_actor = {"Authorization": f"Bearer {actor.token}"}
    as_auditor = {"Authorization": f"Bearer {auditor.token}"}
    keys_before, calls_before = await engine.store.list_access_keys(None), list(manager.calls)

    routes = admin_routes(app)
    writes = [(method, path) for method, path, _ in routes if method in WRITES]
    assert len(writes) >= 15, f"the sweep found too few admin writes to be meaningful: {writes}"
    swept = {path for _, path, _ in routes}
    assert all(any(path.startswith(prefix) for path in swept) for prefix in ADMIN_PATHS), swept
    wrong: list[str] = []
    for method, path, route in routes:
        url, body = filled(path, params), body_for(route)
        callers = [("none", {}, 401), ("actor", as_actor, 403)]
        if method in WRITES:
            callers.append(("auditor", as_auditor, 403))
        for who, headers, expected in callers:
            query = query_for(route, params)
            answer = await client.request(method, url, params=query, json=body, headers=headers)
            if answer.status_code != expected:
                wrong.append(f"{method} {path} as {who}: {answer.status_code}, expected {expected}")
    assert not wrong, "an admin route answered a caller it must refuse:\n" + "\n".join(wrong)
    assert await engine.store.list_access_keys(None) == keys_before  # no key issued, rotated or revoked
    assert manager.calls == calls_before  # and the payment provider was never called


async def everything_stored(engine: BuilderEngine) -> tuple[object, ...]:
    """What a refused request must leave as it was: the keys, the actors and the operation ledger."""
    async with engine.store.engine.connect() as conn:  # type: ignore[attr-defined]
        operations = (await conn.exec_driver_sql("SELECT * FROM lpb_operations ORDER BY 1, 2, 3")).all()
    return await engine.store.list_access_keys(None), await engine.store.list_actors(None), operations


async def test_every_route_that_names_a_tenant_is_403_for_another_tenants_admin_before_any_provider_call(
    surface: tuple[FastAPI, httpx.AsyncClient, BuilderEngine, dict[str, str]],
    admin: ActorContext,
    manager: FakeAllocationManager,
) -> None:
    """The tenant ``default`` has an actor with an allocation, a key and a grant, so nothing is refused
    here for being missing: each 403 is the policy's, for the tenant in the path. Both tenants' grants
    exist at the provider, which hears of none of it."""
    app, client, engine, params = surface
    manager.add_grant("other")
    outsider = await engine.keys.issue(
        admin, kind="tenant_admin", actor_id="other-admin", tenant_id="other", operation_ref="sweep-other"
    )
    as_outsider = {"Authorization": f"Bearer {outsider.token}"}
    stored_before, calls_before = await everything_stored(engine), list(manager.calls)

    routes = [
        (method, route.path, route)
        for route in app.routes
        if isinstance(route, APIRoute) and "{tenant_id}" in route.path
        for method in sorted(route.methods)
    ]
    assert len(routes) >= 15, f"the sweep found too few routes that name a tenant: {routes}"
    assert {path for _, path, _ in routes} >= {
        "/v1/clearinghouse/tenants/{tenant_id}/grant",
        "/v1/clearinghouse/tenants/{tenant_id}/grant/top-up",
        "/v1/clearinghouse/tenants/{tenant_id}/status",
        "/v1/clearinghouse/tenants/{tenant_id}/actors",
        "/v1/clearinghouse/tenants/{tenant_id}/actors/{actor_id}/allocation/rotate-payment-key",
        "/v1/clearinghouse/tenants/{tenant_id}/keys/{key_id}/revoke",
    }
    wrong: list[str] = []
    for method, path, route in routes:
        answer = await client.request(method, filled(path, params), json=body_for(route), headers=as_outsider)
        if answer.status_code != 403:
            wrong.append(f"{method} {path}: {answer.status_code} {answer.text[:120]}")
    assert not wrong, "a route let another tenant's admin past the tenant in its path:\n" + "\n".join(wrong)
    assert manager.calls == calls_before  # the payment provider was never called
    assert await everything_stored(engine) == stored_before  # and nothing was read into a change

    # the same key is an admin of its own tenant: the refusals above are for the tenant, not the kind
    own = {"Authorization": f"Bearer {outsider.token}"}
    assert (await client.get("/v1/clearinghouse/tenants/other/actors", headers=own)).status_code == 200
    assert (await client.get("/v1/clearinghouse/tenants/other/grant", headers=own)).status_code == 200
