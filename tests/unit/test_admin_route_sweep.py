"""ISS-23: authorization denial across the whole admin surface, read from the FastAPI route table.

Every ``/v1/admin/*`` route answers 403 to an actor key and 401 with no credential, and every admin write
answers 403 to an auditor key (a read-only admin, D46). The routes come from ``app.routes``, so an admin
route added later is swept without editing this test; one whose body has no sample fails here and says so.
"""

import re
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute

from livepeer_builder import BuilderEngine
from livepeer_builder.contracts import ActorContext, Money, ProvisionRequest
from livepeer_builder.http import create_app
from livepeer_builder.testing import FakeAllocationManager
from tests.conftest import EngineFactory

WRITES = {"POST", "PUT", "PATCH", "DELETE"}
USD = {"amount": "0.01", "currency": "USD"}
# A valid body for each admin body model, so a denial is the policy's answer and never a 422.
SAMPLE_BODIES: dict[str, dict[str, Any]] = {
    "IssueKeyBody": {"actor_id": "mallory", "operation_ref": "sweep"},
    "RotateKeyBody": {"operation_ref": "sweep"},
    "ProvisionBody": {"amount": USD, "grant_id": "grant_1", "operation_ref": "sweep"},
    "FundRequest": {"amount": USD, "grant_id": "grant_1", "operation_ref": "sweep"},
    "GrantBody": {"name": "sweep", "amount": USD, "operation_ref": "sweep"},
    "StatusBody": {"status": "paused"},
    "RotateBody": {"operation_ref": "sweep"},
}


def admin_routes(app: FastAPI) -> list[tuple[str, str, APIRoute]]:
    """(method, path, route) for every admin route the app serves."""
    return [
        (method, route.path, route)
        for route in app.routes
        if isinstance(route, APIRoute) and route.path.startswith("/v1/admin/")
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
    """An engine with keys and the grant desk on, alice provisioned, and real ids for every path."""
    engine = await make_engine()
    await engine.payments.provision(
        admin,
        ProvisionRequest(
            operation_ref="provision:default:alice", application_id="default", actor_id="alice",
            amount=Money.model_validate(USD), grant_id="grant_1",
        ),
    )  # fmt: skip
    alice = await engine.keys.issue(admin, kind="actor", actor_id="alice", operation_ref="sweep-alice")
    params = {"key_id": alice.key.key_id, "application_id": "default", "actor_id": "alice"}
    app = create_app(engine, manage_lifecycle=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://engine") as client:
        yield app, client, engine, params


def filled(path: str, params: dict[str, str]) -> str:
    return re.sub(r"\{(\w+)\}", lambda match: params[match.group(1)], path)


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
    assert len(writes) >= 11, f"the sweep found too few admin writes to be meaningful: {writes}"
    wrong: list[str] = []
    for method, path, route in routes:
        url, body = filled(path, params), body_for(route)
        callers = [("none", {}, 401), ("actor", as_actor, 403)]
        if method in WRITES:
            callers.append(("auditor", as_auditor, 403))
        for who, headers, expected in callers:
            answer = await client.request(method, url, json=body, headers=headers)
            if answer.status_code != expected:
                wrong.append(f"{method} {path} as {who}: {answer.status_code}, expected {expected}")
    assert not wrong, "an admin route answered a caller it must refuse:\n" + "\n".join(wrong)
    assert await engine.store.list_access_keys(None) == keys_before  # no key issued, rotated or revoked
    assert manager.calls == calls_before  # and the payment provider was never called
