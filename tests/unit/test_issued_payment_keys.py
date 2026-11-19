"""#166: allotting an allocation returns the actor's payment key, once (D87).

A payment key is what an enterprise's engine presents when it asks the clearinghouse to sign a payment
for one actor. It may ask for that actor's payments and nothing else. The clearinghouse issues it at
allotment, replaces it when the payment key is rotated, and stops it when the allocation is revoked. It
keeps the key's hash and never the key, so the token is in the answer of the command that issued it and
nowhere else. The Batteries key the allocation pays with never leaves the clearinghouse, and an engine
with self-hosted payments still pays with it, as before.
"""

import logging
import re
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import delete, select

from livepeer_builder import BuilderEngine, EngineSupply
from livepeer_builder.contracts import ActorAllocation, ActorContext, AllotRequest, JobRequest, Money
from livepeer_builder.errors import NotAuthenticated, ProviderUnavailable
from livepeer_builder.http import create_app
from livepeer_builder.store import SqlStore
from livepeer_builder.store import schema as t
from livepeer_builder.testing import FakeAllocationManager, FixedClock, ScriptedStreamTransport
from livepeer_builder.utils import token_hash
from tests.conftest import EngineFactory
from tests.unit.clearinghouse_house import open_house
from tests.unit.test_tenants import House, Tenant, usd

PAYMENT_KEY = re.compile(r"lpp_[A-Za-z0-9_-]{43}")
NOT_SHOWN = {"token": None, "replayed": True}  # the key was issued by an earlier run: never shown again


@pytest.fixture
async def house(make_engine: EngineFactory) -> AsyncIterator[House]:
    async with open_house(make_engine) as opened:
        yield opened


def allot_body(reference: str, amount: str = "5") -> dict[str, Any]:
    return {"amount": usd(amount), "operation_ref": reference}


async def allot(house: House, tenant: Tenant, actor_id: str, reference: str | None = None) -> dict[str, Any]:
    body = allot_body(reference or f"allot-{tenant.id}-{actor_id}")
    answer = await house.client.post(tenant.allocation(actor_id), json=body, headers=tenant.admin)
    assert answer.status_code == 201, answer.text
    return dict(answer.json())


async def rotate(house: House, tenant: Tenant, actor_id: str, reference: str) -> dict[str, Any]:
    answer = await house.client.post(
        tenant.allocation(actor_id, "/rotate-payment-key"),
        json={"operation_ref": reference},
        headers=tenant.admin,
    )
    assert answer.status_code == 200, answer.text
    return dict(answer.json())


def token_of(answer: dict[str, Any]) -> str:
    token = answer["payment_key"]["token"]
    assert isinstance(token, str) and PAYMENT_KEY.fullmatch(token), answer["payment_key"]
    return token


async def refused(engine: BuilderEngine, token: str) -> bool:
    try:
        await engine.payments.resolve_payment_key(token)
    except NotAuthenticated:
        return True
    return False


async def everything_stored(store: SqlStore) -> str:
    """Every row of every table, as text: what a backup, a dump or a log of the database would hold."""
    found: list[str] = []
    async with store.engine.connect() as conn:
        for table in t.metadata.sorted_tables:
            found += [str(dict(row)) for row in (await conn.execute(select(table))).mappings()]
    return "\n".join(found)


# -- allotment ---------------------------------------------------------------------------------------------


async def test_allotting_an_allocation_returns_the_actors_payment_key_once(house: House) -> None:
    first = await allot(house, house.one, "alice")
    token = token_of(first)
    assert first["payment_key"] == {"token": token, "replayed": False}
    assert (first["tenant_id"], first["actor_id"], first["status"]) == (house.one.id, "alice", "active")

    # a repeat of the reference is answered from the operation ledger: the same allocation, no token
    body = allot_body(f"allot-{house.one.id}-alice")
    again = await house.client.post(house.one.allocation("alice"), json=body, headers=house.one.admin)
    assert again.status_code == 201 and again.headers["Idempotent-Replayed"] == "true"
    assert again.json()["payment_key"] == NOT_SHOWN
    assert {**again.json(), "payment_key": None} == {**first, "payment_key": None}

    # and no read shows it: the allocation is a record, and a secret never rides on a record
    read = await house.client.get(house.one.allocation("alice"), headers=house.one.admin)
    assert read.status_code == 200 and "payment_key" not in read.json()
    listed = await house.client.get(house.one.path("/actors"), headers=house.one.admin)
    assert token not in listed.text and "payment_key" not in listed.text


async def test_a_payment_key_finds_its_actor_and_its_tenant(house: House) -> None:
    alice = await allot(house, house.one, "alice")
    actor = await house.engine.payments.resolve_payment_key(token_of(alice))
    assert isinstance(actor, ActorAllocation)
    assert (actor.tenant_id, actor.actor_id) == (house.one.id, "alice")
    assert actor.allocation_id == alice["allocation_id"]


async def test_a_payment_key_signs_for_its_own_actor_only(house: House) -> None:
    """Three actors, two tenants, one actor id used twice: each key finds the actor it was issued for."""
    keys = {
        (house.one.id, "alice"): token_of(await allot(house, house.one, "alice")),
        (house.one.id, "bob"): token_of(await allot(house, house.one, "bob")),
        (house.two.id, "alice"): token_of(await allot(house, house.two, "alice")),
    }
    assert len(set(keys.values())) == 3
    for holder, token in keys.items():
        actor = await house.engine.payments.resolve_payment_key(token)
        assert (actor.tenant_id, actor.actor_id) == holder


async def test_nothing_but_a_live_payment_key_is_a_payment_key(
    house: House, manager: FakeAllocationManager
) -> None:
    alice = await allot(house, house.one, "alice")
    token = token_of(alice)
    batteries_key = manager.keys[alice["api_key_id"]][1]  # what the allocation pays with: never a way in
    access_key = house.one.admin["Authorization"].removeprefix("Bearer ")
    others = [
        "",
        "lpp_",
        "lpp_" + "x" * 43,
        token[:-1],
        token + "x",
        token.upper(),
        batteries_key,
        access_key,
    ]
    others += [token_hash(token), "lpp_" + token_hash(token)]  # the stored hash is no key either
    for other in others:
        assert await refused(house.engine, other), other
    assert not await refused(house.engine, token)


async def test_the_payment_key_is_stored_nowhere_and_only_its_hash_is_kept(
    house: House, store: SqlStore, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        token = token_of(await allot(house, house.one, "alice"))
        rotated = token_of(await rotate(house, house.one, "alice", "rotate-1"))
    stored = await everything_stored(store)
    assert token not in stored and rotated not in stored
    assert token_hash(rotated) in stored and token_hash(token) not in stored  # one key for an actor
    logged = "\n".join(f"{record.getMessage()} {vars(record)}" for record in caplog.records)
    assert token not in logged and rotated not in logged  # at any level, the database driver's included
    own = "\n".join(
        f"{record.getMessage()} {vars(record)}"
        for record in caplog.records
        if record.name.startswith("livepeer_builder")
    )
    assert token_hash(token) not in own and token_hash(rotated) not in own  # the engine's own lines
    issued = [
        record for record in caplog.records if getattr(record, "event", "").startswith("payments.payment_key")
    ]
    assert [(record.event, record.actor_id) for record in issued] == [  # type: ignore[attr-defined]
        ("payments.payment_key_issued", "alice"),
        ("payments.payment_key_replaced", "alice"),
    ]
    record = await store.get_operation(house.one.id, "tenant-admin", f"allot-{house.one.id}-alice")
    assert record is not None and "payment_key" not in record.result


async def test_the_embedded_api_returns_the_allocation_with_its_payment_key(
    make_engine: EngineFactory, admin: ActorContext
) -> None:
    engine = await make_engine()
    request = AllotRequest(
        tenant_id="default", actor_id="alice", amount=Money(amount=Decimal(5), currency="USD"),
        operation_ref="allot-alice",
    )  # fmt: skip
    allotted = await engine.payments.allot(admin, request)
    assert isinstance(allotted, ActorAllocation)  # with every field an allocation has, as before
    assert allotted.payment_key is not None and allotted.payment_key.replayed is False
    assert PAYMENT_KEY.fullmatch(allotted.payment_key.token or "")
    stored = await engine.store.get_actor("default", "alice")
    assert stored is not None and allotted.model_dump(exclude={"payment_key"}) == stored.model_dump()

    replay = await engine.payments.allot(admin, request)
    assert replay.payment_key is not None
    assert (replay.payment_key.token, replay.payment_key.replayed) == (None, True)


# -- rotation and revocation -------------------------------------------------------------------------------


async def test_rotation_replaces_the_payment_key_and_the_old_one_stops_at_once(
    house: House, manager: FakeAllocationManager
) -> None:
    before = await allot(house, house.one, "alice")
    old = token_of(before)
    calls = len(manager.calls)

    rotated = await rotate(house, house.one, "alice", "rotate-1")
    new = token_of(rotated)
    assert new != old and rotated["payment_key"]["replayed"] is False
    assert await refused(house.engine, old)
    actor = await house.engine.payments.resolve_payment_key(new)
    assert (actor.tenant_id, actor.actor_id, actor.allocation_id) == (
        house.one.id, "alice", before["allocation_id"],
    )  # fmt: skip

    # the Batteries key is rotated as before: a new one on the same allocation, and the old one revoked
    assert manager.calls[calls:] == ["create_key", "revoke_key"]
    assert rotated["api_key_id"] != before["api_key_id"]
    assert manager.keys[before["api_key_id"]][2] is True  # revoked at the provider

    again = await house.client.post(
        house.one.allocation("alice", "/rotate-payment-key"),
        json={"operation_ref": "rotate-1"},
        headers=house.one.admin,
    )
    assert again.status_code == 200 and again.headers["Idempotent-Replayed"] == "true"
    assert again.json()["payment_key"] == NOT_SHOWN  # the repeat rotates nothing and shows nothing
    assert not await refused(house.engine, new)
    assert manager.calls[calls:] == ["create_key", "revoke_key"]


async def test_revoking_an_allocation_stops_its_payment_key(house: House) -> None:
    token = token_of(await allot(house, house.one, "alice"))
    revoked = await house.client.post(house.one.allocation("alice", "/revoke"), headers=house.one.admin)
    assert revoked.status_code == 200 and "payment_key" not in revoked.json()
    assert await refused(house.engine, token)

    # allotted again, the actor has another allocation and another payment key; the old one stays stopped
    again = await allot(house, house.one, "alice", "allot-alice-again")
    assert token_of(again) != token and await refused(house.engine, token)
    actor = await house.engine.payments.resolve_payment_key(token_of(again))
    assert actor.allocation_id == again["allocation_id"]


async def test_a_paused_allocation_keeps_its_payment_key(house: House) -> None:
    """Pausing stops the payments at the payment provider, which refuses them (403); the key is not
    replaced, so resuming needs no new one."""
    token = token_of(await allot(house, house.one, "alice"))
    paused = await house.client.post(
        house.one.allocation("alice", "/status"), json={"status": "paused"}, headers=house.one.admin
    )
    assert paused.status_code == 200 and "payment_key" not in paused.json()
    assert (await house.engine.payments.resolve_payment_key(token)).status == "paused"


async def test_no_other_allocation_command_shows_or_changes_a_payment_key(house: House) -> None:
    token = token_of(await allot(house, house.one, "alice"))
    top_up = await house.client.post(
        house.one.allocation("alice", "/top-up"), json=allot_body("top-up-1", "1"), headers=house.one.admin
    )
    assert top_up.status_code == 200 and "payment_key" not in top_up.json()
    assert not await refused(house.engine, token)


# -- a payment key is not an access key ---------------------------------------------------------------------


async def test_a_payment_key_authenticates_nothing_on_any_route(house: House) -> None:
    """Presented as a bearer on every route the engine serves, a payment key gets what no credential
    gets. The payments route, which takes one, is not built yet (#166, part two)."""
    token = token_of(await allot(house, house.one, "alice"))
    as_payment_key = {"Authorization": f"Bearer {token}"}
    app = create_app(house.engine, manage_lifecycle=False)
    params = {"tenant_id": house.one.id, "actor_id": "alice", "key_id": house.one.key_id, "job_id": "x"}
    routes = [
        (method, re.sub(r"\{(\w+)\}", lambda match: params[match.group(1)], route.path))
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in sorted(route.methods)
    ]
    assert len(routes) >= 35, routes  # every route of both sets: fewer means the walk missed some
    let_in: list[str] = []
    refused_as_nobody = 0
    for method, path in routes:
        body = None if method == "GET" else {}
        nobody = await house.client.request(method, path, json=body)
        answer = await house.client.request(method, path, json=body, headers=as_payment_key)
        refused_as_nobody += answer.status_code == 401
        if answer.status_code != nobody.status_code or answer.text != nobody.text:
            let_in.append(
                f"{method} {path}: {answer.status_code}, and {nobody.status_code} with no credential"
            )
    assert not let_in, "a payment key was read as a credential:\n" + "\n".join(let_in)
    assert refused_as_nobody >= len(routes) - 3  # all but /livez, /readyz and the public catalog


# -- self-hosted payments ----------------------------------------------------------------------------------


async def test_self_hosted_payments_still_pays_with_the_batteries_key(
    make_engine: EngineFactory,
    admin: ActorContext,
    alice: ActorContext,
    manager: FakeAllocationManager,
    transport: ScriptedStreamTransport,
) -> None:
    """One engine, both halves: the work half's ``CredentialResolver`` returns the stored Batteries key,
    and that is what the runner call presents to the signer. The payment key changes nothing there."""
    engine = await make_engine()
    request = AllotRequest(
        tenant_id="default", actor_id="alice", amount=Money(amount=Decimal(5), currency="USD"),
        operation_ref="allot-alice",
    )  # fmt: skip
    allotted = await engine.payments.allot(admin, request)
    assert allotted.payment_key is not None and allotted.payment_key.token is not None
    batteries_key = manager.keys[allotted.api_key_id][1]
    assert batteries_key.startswith("lpg_")

    credential = await engine.payments.credential(alice)
    assert (credential.token, credential.allocation_id) == (batteries_key, allotted.allocation_id)
    transport.steps.append(ScriptedStreamTransport.ok())
    await engine.jobs.run(alice, JobRequest(app="example/echo", payload={}))
    assert transport.calls[-1][1] == batteries_key != allotted.payment_key.token

    rotated = await engine.payments.rotate_payment_key(admin, "default", "alice", "rotate-1")
    assert (await engine.payments.credential(alice)).token == manager.keys[rotated.api_key_id][1]


# -- allocations without a payment key -----------------------------------------------------------------------


async def test_an_allocation_allotted_before_payment_keys_gets_one_by_rotation(
    house: House, store: SqlStore
) -> None:
    """An allocation a release before #166 allotted has a record and no payment key. Its allotment's
    repeat still answers from the ledger, with no token, and rotating its payment key issues its first."""
    before = await allot(house, house.one, "alice")
    async with store.engine.begin() as conn:  # as that release left it: the actor's row and nothing else
        await conn.execute(delete(t.payment_keys))
    assert await refused(house.engine, token_of(before))

    body = allot_body(f"allot-{house.one.id}-alice")
    repeat = await house.client.post(house.one.allocation("alice"), json=body, headers=house.one.admin)
    assert repeat.status_code == 201 and repeat.json()["payment_key"] == NOT_SHOWN

    rotated = await rotate(house, house.one, "alice", "first-payment-key")
    actor = await house.engine.payments.resolve_payment_key(token_of(rotated))
    assert (actor.actor_id, actor.allocation_id) == ("alice", before["allocation_id"])


async def test_a_resumed_allotment_issues_the_payment_key_its_first_run_did_not(
    house: House, store: SqlStore, clock: FixedClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The engine stopped between storing the actor and issuing its payment key. No key was ever shown,
    so the resume issues the first one and shows it; a later repeat shows nothing."""
    put = store.put_payment_key

    async def died(*args: Any, **kwargs: Any) -> bool:
        monkeypatch.setattr(store, "put_payment_key", put)  # once
        raise ProviderUnavailable("the engine stopped here")

    monkeypatch.setattr(store, "put_payment_key", died)
    body = allot_body("allot-alice")
    cut_short = await house.client.post(house.one.allocation("alice"), json=body, headers=house.one.admin)
    assert cut_short.status_code == 503
    assert await store.get_actor(house.one.id, "alice") is not None  # allotted, with no payment key

    clock.advance(120)  # LPB_OPERATION_STALE_S: the first run is presumed dead
    resumed = await house.client.post(house.one.allocation("alice"), json=body, headers=house.one.admin)
    assert resumed.status_code == 201, resumed.text
    assert resumed.json()["payment_key"]["replayed"] is False
    assert not await refused(house.engine, token_of(resumed.json()))

    repeat = await house.client.post(house.one.allocation("alice"), json=body, headers=house.one.admin)
    assert repeat.json()["payment_key"] == NOT_SHOWN
    assert not await refused(house.engine, token_of(resumed.json()))


class NoPaymentKeys:
    """A supplied store written before payment keys: every repo of ``SqlStore`` but ``PaymentKeyRepo``."""

    def __init__(self, inner: SqlStore) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        if name in ("put_payment_key", "delete_payment_key", "actor_by_payment_key"):
            raise AttributeError(name)
        return getattr(self._inner, name)


async def test_a_store_that_keeps_no_payment_keys_allots_without_one(
    make_engine: EngineFactory, store: SqlStore, admin: ActorContext
) -> None:
    engine = await make_engine(supply=EngineSupply(store=NoPaymentKeys(store)))  # type: ignore[arg-type]
    request = AllotRequest(
        tenant_id="default", actor_id="alice", amount=Money(amount=Decimal(5), currency="USD"),
        operation_ref="allot-alice",
    )  # fmt: skip
    allotted = await engine.payments.allot(admin, request)
    assert allotted.status == "active" and allotted.payment_key is None
    rotated = await engine.payments.rotate_payment_key(admin, "default", "alice", "rotate-1")
    assert rotated.payment_key is None and rotated.api_key_id != allotted.api_key_id
    assert (await engine.payments.revoke(admin, "default", "alice")).status == "revoked"
    assert await refused(engine, "lpp_" + "x" * 43)
