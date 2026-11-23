"""#169: who keeps each allocation's key, by the engine's role and on each of its listeners (D84 point
2, D85 point 1, D87 point 1).

| ``LPB_ROLE`` | Allotment and rotation answer | The key is kept |
| --- | --- | --- |
| ``both`` | The allocation, and no key | In the engine's own secret store, for its own jobs |
| ``work`` | Nothing: it mounts no route that allots | In its own store, by its own commands |
| ``clearinghouse`` | The allocation and its key, once | By the caller. The clearinghouse stores none |

``delivers_allocation_keys`` in ``http/listeners.py`` is the one place that decides, ``create_app`` asks
it for every app it builds, and ``mount_listener_routes`` is handed the answer. So the rule is the same
on one listener and on two, and nothing but the role chooses it.
"""

from collections.abc import Callable
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from livepeer_builder import BuilderEngine, EngineSupply
from livepeer_builder.contracts import ActorContext
from livepeer_builder.http import create_app
from livepeer_builder.http.app import shared_app
from livepeer_builder.http.listeners import ListenerName, delivers_allocation_keys, mount_listener_routes
from livepeer_builder.testing import FakeAllocationManager, MemorySecretStore
from tests.conftest import EngineFactory
from tests.unit.role_engines import ROLES, Role, in_role, served

ALLOCATION = "/v1/clearinghouse/tenants/default/actors/alice/allocation"
ROTATE = ALLOCATION + "/rotate-payment-key"
ALLOT = {"amount": {"amount": "5", "currency": "USD"}, "operation_ref": "allot-alice"}
# The listeners each role has. An engine in its clearinghouse role always has two (test_roles.py).
LISTENERS: dict[Role, tuple[ListenerName, ...]] = {
    "both": ("only", "public", "private"),
    "work": ("only", "public", "private"),
    "clearinghouse": ("public", "private"),
}
EVERY = [(role, listener) for role in ROLES for listener in LISTENERS[role]]


def client_of(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://engine")


def bearer(token: str | None) -> dict[str, str]:
    assert token is not None
    return {"Authorization": f"Bearer {token}"}


async def caller_on(engine: BuilderEngine, listener: ListenerName, admin: ActorContext) -> dict[str, str]:
    """A credential that may allot on ``listener``: the private one takes a clearinghouse admin only, and
    the public one refuses it, so there the tenant's own admin allots."""
    assert engine.keys is not None
    if listener == "public":
        issued = await engine.keys.issue(
            admin, kind="tenant_admin", actor_id="desk", operation_ref=f"desk-{listener}"
        )
        return bearer(issued.token)
    return bearer((await engine.keys.bootstrap_admin()).token)


def minted(manager: FakeAllocationManager) -> list[str]:
    """Every key the provider minted, live or ended, in order."""
    return [secret for _, secret, _ in manager.keys.values()]


async def stored_anywhere(engine: BuilderEngine) -> str:
    """Every row of every table of the engine's database, as text: where a key must not be."""
    rows: list[Any] = []
    async with engine.store.engine.connect() as conn:  # type: ignore[attr-defined]
        tables = await conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type = 'table'")
        for (table,) in tables.all():
            rows += (await conn.exec_driver_sql(f'SELECT * FROM "{table}"')).all()  # noqa: S608
    return repr(rows)


# -- the one place that decides ------------------------------------------------------------------------


def test_only_the_clearinghouse_role_hands_keys_over() -> None:
    """An engine that runs jobs stores each key for them. Only a role that runs none hands them over."""
    assert {role: delivers_allocation_keys(role) for role in ROLES} == {
        "both": False,
        "work": False,
        "clearinghouse": True,
    }


@pytest.mark.parametrize(("role", "listener"), EVERY)
async def test_whoever_mounts_a_listeners_routes_says_who_keeps_the_keys(
    make_engine: EngineFactory, role: Role, listener: ListenerName
) -> None:
    """``deliver_allocation_keys`` reaches ``mount_listener_routes`` as a parameter with no default, as
    it reaches ``mount_tenant_routes`` (Mike, 23 November 2026), and it is what the role says: a value
    that disagrees with the role is refused, and nothing is mounted."""
    engine = await in_role(make_engine, role)
    health = served(shared_app(engine, manage_lifecycle=False)).keys()
    app = shared_app(engine, manage_lifecycle=False)
    mount: Callable[..., None] = mount_listener_routes  # called below without the keyword, on purpose
    with pytest.raises(TypeError, match="deliver_allocation_keys"):
        mount(app, engine, listener)
    with pytest.raises(ValueError, match="delivers_allocation_keys") as refused:
        mount_listener_routes(
            app, engine, listener, deliver_allocation_keys=not delivers_allocation_keys(role)
        )
    assert "role" in str(refused.value)
    assert served(app).keys() == health
    mount_listener_routes(app, engine, listener, deliver_allocation_keys=delivers_allocation_keys(role))
    assert served(app).keys() == served(create_app(engine, manage_lifecycle=False, listener=listener)).keys()


# -- both: the engine stores each key for its own jobs, and returns none ----------------------------------


@pytest.mark.parametrize("listener", LISTENERS["both"])
async def test_a_both_engine_stores_each_key_and_answers_none_on_every_listener(
    make_engine: EngineFactory, manager: FakeAllocationManager, admin: ActorContext, listener: ListenerName
) -> None:
    secrets = MemorySecretStore()
    engine = await make_engine(supply=EngineSupply(secret_store=secrets))
    assert engine.role == "both"
    headers = await caller_on(engine, listener, admin)
    async with client_of(create_app(engine, manage_lifecycle=False, listener=listener)) as client:
        allotted = await client.post(ALLOCATION, json=ALLOT, headers=headers)
        assert allotted.status_code == 201, allotted.text
        [first] = minted(manager)
        assert "allocation_api_key" not in allotted.json() and first not in allotted.text
        assert allotted.json()["credential_custody"] == "secret_store"
        assert list(secrets.values.values()) == [first]  # where its own jobs find it

        rotated = await client.post(ROTATE, json={"operation_ref": "rotate-alice"}, headers=headers)
        assert rotated.status_code == 200, rotated.text
        second = minted(manager)[-1]
        assert second != first and "allocation_api_key" not in rotated.json()
        assert first not in rotated.text and second not in rotated.text
        assert list(secrets.values.values()) == [second]  # the old one is gone from the store
    credential = await engine.payments.credential(ActorContext(actor_id="alice"))
    assert credential.token == second


# -- clearinghouse: each key is handed to the caller once, and none is stored ----------------------------


@pytest.mark.parametrize("listener", LISTENERS["clearinghouse"])
async def test_a_clearinghouse_hands_each_key_to_the_caller_and_stores_none_on_both_listeners(
    make_engine: EngineFactory, manager: FakeAllocationManager, admin: ActorContext, listener: ListenerName
) -> None:
    secrets = MemorySecretStore()
    engine = await in_role(make_engine, "clearinghouse", supply=EngineSupply(secret_store=secrets))
    headers = await caller_on(engine, listener, admin)
    async with client_of(create_app(engine, manage_lifecycle=False, listener=listener)) as client:
        allotted = await client.post(ALLOCATION, json=ALLOT, headers=headers)
        assert allotted.status_code == 201, allotted.text
        [first] = minted(manager)
        answer = allotted.json()
        assert (answer["allocation_api_key"], answer["replayed"], answer["resumed"]) == (first, False, False)
        assert answer["credential_custody"] == "work_engine"
        assert allotted.headers["cache-control"] == "no-store"

        # once: the repeat of the finished command answers the allocation without the key
        repeat = await client.post(ALLOCATION, json=ALLOT, headers=headers)
        assert (repeat.json()["allocation_api_key"], repeat.json()["replayed"]) == (None, True)
        assert first not in repeat.text and minted(manager) == [first]
        # and a read never carries it
        assert first not in (await client.get(ALLOCATION, headers=headers)).text

        rotated = await client.post(ROTATE, json={"operation_ref": "rotate-alice"}, headers=headers)
        assert rotated.status_code == 200, rotated.text
        second = minted(manager)[-1]
        assert second != first
        assert (rotated.json()["allocation_api_key"], rotated.json()["replayed"]) == (second, False)
        assert first not in rotated.text and rotated.headers["cache-control"] == "no-store"
    # neither key is anywhere at the clearinghouse: not in its secret store, and not in its database
    assert secrets.values == {}
    everything = await stored_anywhere(engine)
    assert "alice" in everything  # the scan reads the actor's record, the ledger and the rest
    assert first not in everything and second not in everything
    held = await engine.store.get_actor("default", "alice")
    assert held is not None and held.credential_custody == "work_engine"


async def test_a_clearinghouse_documents_the_answer_that_hands_a_key_over_and_a_both_engine_does_not(
    make_engine: EngineFactory,
) -> None:
    """Each app's own schema says what its routes answer: the delivered body in the ``clearinghouse``
    role, on both listeners, and the allocation alone in the ``both`` role."""

    def answers(app: FastAPI) -> tuple[Any, Any]:
        paths = app.openapi()["paths"]
        tenant = "/v1/clearinghouse/tenants/{tenant_id}/actors/{actor_id}/allocation"
        allot = paths[tenant]["post"]["responses"]["201"]["content"]["application/json"]["schema"]
        rotate = paths[tenant + "/rotate-payment-key"]["post"]["responses"]["200"]
        return allot, rotate["content"]["application/json"]["schema"]

    delivered = {"$ref": "#/components/schemas/DeliveredAllocationBody"}
    kept = {"$ref": "#/components/schemas/ActorAllocation"}
    house = await in_role(make_engine, "clearinghouse")
    for listener in LISTENERS["clearinghouse"]:
        assert answers(create_app(house, manage_lifecycle=False, listener=listener)) == (delivered, delivered)
    whole = await in_role(make_engine, "both")
    for listener in LISTENERS["both"]:
        assert answers(create_app(whole, manage_lifecycle=False, listener=listener)) == (kept, kept)


# -- work: no route allots; its own commands store the key ------------------------------------------------


@pytest.mark.parametrize("listener", LISTENERS["work"])
async def test_a_work_engine_mounts_no_route_that_allots_or_rotates_on_any_listener(
    make_engine: EngineFactory, manager: FakeAllocationManager, admin: ActorContext, listener: ListenerName
) -> None:
    engine = await in_role(make_engine, "work")
    headers = await caller_on(engine, listener, admin)
    calls_before = list(manager.calls)
    app = create_app(engine, manage_lifecycle=False, listener=listener)
    assert not [path for _, path in served(app) if path.startswith("/v1/clearinghouse")]
    async with client_of(app) as client:
        assert (await client.post(ALLOCATION, json=ALLOT, headers=headers)).status_code == 404
        rotate = await client.post(ROTATE, json={"operation_ref": "rotate-alice"}, headers=headers)
        assert rotate.status_code == 404
    assert manager.calls == calls_before and minted(manager) == []
