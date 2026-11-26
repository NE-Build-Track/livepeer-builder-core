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

A tenant's routes come in two forms (D87 point 3, as amended on 19 November 2026), and the rule is the
same for both: its own, which name no tenant and take the tenant admin's key, on the public listener;
and the ones a clearinghouse admin names the tenant in, on the private listener. One listener serves
both. So a clearinghouse hands the key to the tenant's admin on the public listener, and to the
clearinghouse admin on the private one: it stores none, and an answer without it there would leave the
key with nobody.
"""

from collections.abc import Callable
from typing import Any, Literal

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

Form = Literal["own", "named"]
ALLOCATIONS: dict[Form, str] = {
    "own": "/v1/clearinghouse/actors/alice/allocation",  # the tenant is the credential's
    "named": "/v1/clearinghouse/tenants/default/actors/alice/allocation",  # a clearinghouse admin's
}
ROTATE = "/rotate-payment-key"
ALLOT = {"amount": {"amount": "5", "currency": "USD"}, "operation_ref": "allot-alice"}
# The listeners each role has. An engine in its clearinghouse role always has two (test_roles.py).
LISTENERS: dict[Role, tuple[ListenerName, ...]] = {
    "both": ("only", "public", "private"),
    "work": ("only", "public", "private"),
    "clearinghouse": ("public", "private"),
}
EVERY = [(role, listener) for role in ROLES for listener in LISTENERS[role]]
# The forms of a tenant's routes each listener serves.
FORMS: dict[ListenerName, tuple[Form, ...]] = {
    "only": ("own", "named"),
    "public": ("own",),
    "private": ("named",),
}


def served_forms(role: Role) -> list[tuple[ListenerName, Form]]:
    return [(listener, form) for listener in LISTENERS[role] for form in FORMS[listener]]


def client_of(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://engine")


def bearer(token: str | None) -> dict[str, str]:
    assert token is not None
    return {"Authorization": f"Bearer {token}"}


async def caller_of(engine: BuilderEngine, form: Form, admin: ActorContext) -> dict[str, str]:
    """A credential that may allot at a path of ``form``: the tenant's own admin where the path names no
    tenant, and a clearinghouse admin where it names one."""
    assert engine.keys is not None
    if form == "own":
        issued = await engine.keys.issue(admin, kind="tenant_admin", actor_id="desk", operation_ref="desk")
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


@pytest.mark.parametrize(("listener", "form"), served_forms("both"))
async def test_a_both_engine_stores_each_key_and_answers_none_on_every_listener(
    make_engine: EngineFactory,
    manager: FakeAllocationManager,
    admin: ActorContext,
    listener: ListenerName,
    form: Form,
) -> None:
    secrets = MemorySecretStore()
    engine = await make_engine(supply=EngineSupply(secret_store=secrets))
    assert engine.role == "both"
    headers, allocation = await caller_of(engine, form, admin), ALLOCATIONS[form]
    async with client_of(create_app(engine, manage_lifecycle=False, listener=listener)) as client:
        allotted = await client.post(allocation, json=ALLOT, headers=headers)
        assert allotted.status_code == 201, allotted.text
        [first] = minted(manager)
        assert "allocation_api_key" not in allotted.json() and first not in allotted.text
        assert allotted.json()["credential_custody"] == "secret_store"
        assert list(secrets.values.values()) == [first]  # where its own jobs find it

        rotated = await client.post(
            allocation + ROTATE, json={"operation_ref": "rotate-alice"}, headers=headers
        )
        assert rotated.status_code == 200, rotated.text
        second = minted(manager)[-1]
        assert second != first and "allocation_api_key" not in rotated.json()
        assert first not in rotated.text and second not in rotated.text
        assert list(secrets.values.values()) == [second]  # the old one is gone from the store
    credential = await engine.payments.credential(ActorContext(actor_id="alice"))
    assert credential.token == second


# -- clearinghouse: each key is handed to the caller once, and none is stored ----------------------------


@pytest.mark.parametrize(("listener", "form"), served_forms("clearinghouse"))
async def test_a_clearinghouse_hands_each_key_to_the_caller_and_stores_none_on_both_listeners(
    make_engine: EngineFactory,
    manager: FakeAllocationManager,
    admin: ActorContext,
    listener: ListenerName,
    form: Form,
) -> None:
    """On the public listener the caller is the tenant's admin, at the path that names no tenant: its
    credential names the tenant. On the private listener it is the clearinghouse admin, at the path that
    names the tenant. Each is handed the key once, and the clearinghouse stores none either way."""
    secrets = MemorySecretStore()
    engine = await in_role(make_engine, "clearinghouse", supply=EngineSupply(secret_store=secrets))
    headers, allocation = await caller_of(engine, form, admin), ALLOCATIONS[form]
    async with client_of(create_app(engine, manage_lifecycle=False, listener=listener)) as client:
        allotted = await client.post(allocation, json=ALLOT, headers=headers)
        assert allotted.status_code == 201, allotted.text
        [first] = minted(manager)
        answer = allotted.json()
        assert (answer["allocation_api_key"], answer["replayed"], answer["resumed"]) == (first, False, False)
        assert answer["credential_custody"] == "work_engine"
        assert allotted.headers["cache-control"] == "no-store"

        # once: the repeat of the finished command answers the allocation without the key
        repeat = await client.post(allocation, json=ALLOT, headers=headers)
        assert (repeat.json()["allocation_api_key"], repeat.json()["replayed"]) == (None, True)
        assert first not in repeat.text and minted(manager) == [first]
        # and a read never carries it
        assert first not in (await client.get(allocation, headers=headers)).text

        rotated = await client.post(
            allocation + ROTATE, json={"operation_ref": "rotate-alice"}, headers=headers
        )
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
    role, on both listeners, and the allocation alone in the ``both`` role. Each form of a tenant's
    routes says the same, on the listener that serves it, and the other form is not in that schema."""
    paths = {
        "own": "/v1/clearinghouse/actors/{actor_id}/allocation",
        "named": "/v1/clearinghouse/tenants/{tenant_id}/actors/{actor_id}/allocation",
    }

    def answers(app: FastAPI, form: str) -> tuple[Any, Any]:
        served_paths = app.openapi()["paths"]
        allot = served_paths[paths[form]]["post"]["responses"]["201"]["content"]["application/json"]["schema"]
        rotate = served_paths[paths[form] + ROTATE]["post"]["responses"]["200"]
        return allot, rotate["content"]["application/json"]["schema"]

    delivered = {"$ref": "#/components/schemas/DeliveredAllocationBody"}
    kept = {"$ref": "#/components/schemas/ActorAllocation"}
    for role, expected in (("clearinghouse", delivered), ("both", kept)):
        engine = await in_role(make_engine, role)  # type: ignore[arg-type]
        for listener in LISTENERS[role]:  # type: ignore[index]
            app = create_app(engine, manage_lifecycle=False, listener=listener)
            for form in FORMS[listener]:
                assert answers(app, form) == (expected, expected), (role, listener, form)
            for form in set(paths) - set(FORMS[listener]):
                assert paths[form] not in app.openapi()["paths"], (role, listener, form)


# -- work: no route allots; its own commands store the key ------------------------------------------------


@pytest.mark.parametrize("listener", LISTENERS["work"])
async def test_a_work_engine_mounts_no_route_that_allots_or_rotates_on_any_listener(
    make_engine: EngineFactory, manager: FakeAllocationManager, admin: ActorContext, listener: ListenerName
) -> None:
    engine = await in_role(make_engine, "work")
    calls_before = list(manager.calls)
    app = create_app(engine, manage_lifecycle=False, listener=listener)
    assert not [path for _, path in served(app) if path.startswith("/v1/clearinghouse")]
    forms: tuple[Form, ...] = ("own", "named")
    for form in forms:  # neither form of a tenant's routes, whoever asks
        headers, allocation = await caller_of(engine, form, admin), ALLOCATIONS[form]
        async with client_of(app) as client:
            assert (await client.post(allocation, json=ALLOT, headers=headers)).status_code == 404
            rotate = await client.post(
                allocation + ROTATE, json={"operation_ref": "rotate-alice"}, headers=headers
            )
            assert rotate.status_code == 404
    assert manager.calls == calls_before and minted(manager) == []
