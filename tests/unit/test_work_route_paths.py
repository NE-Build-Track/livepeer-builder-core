"""Reconciliation, cost sync and an engine's own access keys are work routes (D87, #164).

They were served under ``/v1/clearinghouse``, where #163 left them. An enterprise that uses a payment
clearinghouse runs the work half alone, and it still reconciles, syncs costs and issues keys to its own
users, so the three groups sit with the work routes: ``GET /v1/costs/reconciliation``,
``POST /v1/costs/sync`` and the key routes under ``/v1/keys``. Only the paths moved. Who may call each
route, its body, its status codes and its repeat-safety are as they were, and no alias is kept: the old
paths are 404.
"""

from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi.routing import APIRoute

from livepeer_builder import BuilderEngine
from livepeer_builder.contracts import ActorContext
from livepeer_builder.http import create_app
from tests.conftest import EngineFactory, allot

OPERATOR = ActorContext(actor_id="operator", credential_kind="clearinghouse_admin")
# Where the three moved groups were served until #164. Each is 404 now.
OLD_PATHS = [
    ("GET", "/v1/clearinghouse/reconciliation"),
    ("POST", "/v1/clearinghouse/costs/sync"),
    ("POST", "/v1/clearinghouse/keys"),
    ("GET", "/v1/clearinghouse/keys"),
    ("GET", "/v1/clearinghouse/keys/{key_id}"),
    ("POST", "/v1/clearinghouse/keys/{key_id}/rotate"),
    ("POST", "/v1/clearinghouse/keys/{key_id}/revoke"),
]


Served = tuple[httpx.AsyncClient, BuilderEngine, dict[str, str], dict[str, str]]


@pytest.fixture
async def api(make_engine: EngineFactory) -> AsyncIterator[Served]:
    engine = await make_engine()
    assert engine.keys is not None
    admin = await engine.keys.bootstrap_admin()
    alice = await engine.keys.issue(OPERATOR, kind="actor", actor_id="alice", operation_ref="issue-alice")
    await allot(engine, ActorContext(actor_id="alice"))  # reconciliation reads her allocation
    app = create_app(engine, manage_lifecycle=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://engine") as client:
        yield (
            client,
            engine,
            {"Authorization": f"Bearer {admin.token}"},
            {"Authorization": f"Bearer {alice.token}"},
        )


async def test_reconciliation_and_cost_sync_are_work_routes_under_costs(api: Served) -> None:
    client, _engine, admin, alice = api
    synced = await client.post("/v1/costs/sync", headers=admin)
    assert synced.status_code == 200 and synced.json()["pages"] == 1
    reconciled = await client.get("/v1/costs/reconciliation", params={"actor_id": "alice"}, headers=admin)
    assert reconciled.status_code == 200 and reconciled.json()["tenant_id"] == "default"
    # who may call them is as it was: an actor key may not, and no credential is a 401
    assert (await client.post("/v1/costs/sync", headers=alice)).status_code == 403
    assert (await client.get("/v1/costs/reconciliation", headers=alice)).status_code == 403
    assert (await client.post("/v1/costs/sync")).status_code == 401
    assert (await client.get("/v1/costs/reconciliation")).status_code == 401


async def test_the_engines_own_access_keys_are_work_routes_under_keys(api: Served) -> None:
    client, _engine, admin, alice = api
    body = {"kind": "actor", "actor_id": "bob", "label": "bob laptop", "operation_ref": "key-bob-1"}
    issued = await client.post("/v1/keys", json=body, headers=admin)
    assert issued.status_code == 201 and issued.json()["token"].startswith("lpk_")
    key_id = issued.json()["key"]["key_id"]
    again = await client.post("/v1/keys", json=body, headers=admin)  # a repeat: answered from the ledger
    assert again.status_code == 201 and again.headers["Idempotent-Replayed"] == "true"
    listed = await client.get("/v1/keys", headers=admin)
    assert listed.status_code == 200 and key_id in [key["key_id"] for key in listed.json()]
    assert (await client.get(f"/v1/keys/{key_id}", headers=admin)).json()["actor_id"] == "bob"
    rotated = await client.post(
        f"/v1/keys/{key_id}/rotate", json={"operation_ref": "rotate-bob-1"}, headers=admin
    )
    assert rotated.status_code == 201 and rotated.json()["key"]["key_id"] != key_id
    revoked = await client.post(f"/v1/keys/{rotated.json()['key']['key_id']}/revoke", headers=admin)
    assert revoked.status_code == 204
    # who may call them is as it was: an actor key may not, and no credential is a 401
    assert (await client.get("/v1/keys", headers=alice)).status_code == 403
    assert (await client.post("/v1/keys", json=body, headers=alice)).status_code == 403
    assert (await client.get("/v1/keys")).status_code == 401


@pytest.mark.parametrize(("method", "path"), OLD_PATHS, ids=[f"{m} {p}" for m, p in OLD_PATHS])
async def test_a_moved_route_is_404_at_its_old_path(api: Served, method: str, path: str) -> None:
    """No alias: the old path is no route, for an admin and for no credential alike."""
    client, engine, admin, _alice = api
    assert engine.keys is not None
    existing = (await engine.keys.list_keys(OPERATOR))[0]  # a key that exists, so a 404 is the route's
    url = path.format(key_id=existing.key_id)
    body = {"actor_id": "bob", "operation_ref": "old-path"}
    assert (await client.request(method, url, json=body, headers=admin)).status_code == 404
    assert (await client.request(method, url, json=body)).status_code == 404
    routes = create_app(engine, manage_lifecycle=False).routes
    assert path not in [route.path for route in routes if isinstance(route, APIRoute)]
