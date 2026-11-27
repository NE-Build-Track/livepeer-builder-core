"""#158: an id the payment provider answers with is kept only when the store keeps it, on SQLite and
PostgreSQL.

The provider answers a new key with its allocation's id and its own, and a new grant with the grant's
id, which is the tenant's from then on. The engine keeps each in a 200-character column. SQLite took a
longer id and one with a NUL; PostgreSQL refused the row, after the key was funded or the grant created:
a 500, with the key's secret already stored and the command's record already noting it. Neither kept a
lone surrogate. Now such an answer is one the engine cannot use, whatever the database: a
``ProviderError`` before anything is stored, which leaves the outcome unknown and the reference held.
The engine's own check refuses the long id and the NUL, from any allocation manager; the Batteries
reader refuses the lone surrogate first, which is no text at all.
"""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import String, select

from livepeer_builder import BuilderEngine, EngineSupply
from livepeer_builder.adapters.batteries import BatteriesClient
from livepeer_builder.config import generate_secret_key
from livepeer_builder.contracts import ActorContext, AllotRequest, Money
from livepeer_builder.errors import OperationInProgress, ProviderError, ProviderUnavailable
from livepeer_builder.http import create_app
from livepeer_builder.services.ledger import definite_refusal
from livepeer_builder.store import schema as t
from livepeer_builder.testing import FixedClock, make_engine

ADMIN = ActorContext(actor_id="operator", credential_kind="clearinghouse_admin")
BASE = "https://batteries.test"
USD = Money(amount=Decimal(5), currency="USD")
ALLOT = AllotRequest(operation_ref="p-1", tenant_id="default", actor_id="alice", amount=USD)
KEY = {"id": "key_1", "allocation_id": "alloc_1", "api_key": "lpg_key_1_secret"}
GRANT = {"id": "grant_1", "name": "ops", "status": "active", "currency": "usd", "total_usd": "5"}
NOT_KEPT = {"201 characters": "x" * 201, "a NUL": "a\x00b", "a lone surrogate": "a\ud800b"}
STALE_S = 121  # past LPB_OPERATION_STALE_S: a repeat may resume a command whose outcome is unknown


class Batteries:
    """A Batteries that answers a key create with ``key`` and a grant create or read with ``grant``,
    which keeps the metadata it was created with, as the list serves it."""

    def __init__(self, key: dict[str, Any] | None = None, grant: dict[str, Any] | None = None) -> None:
        self.key, self.grant = key or KEY, grant or GRANT
        self.asked: list[str] = []
        self.reads_a_grant_with = 200  # the status of a read of one grant

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.asked.append(f"{request.method} {request.url.path}")
        if request.url.path == "/v1/api-keys":
            return httpx.Response(201, content=json.dumps(self.key).encode())
        if request.url.path == "/v1/grants" and request.method == "POST":
            self.grant = self.grant | {"metadata": json.loads(request.content).get("metadata", "")}
            return httpx.Response(201, content=json.dumps({"id": self.grant["id"]}).encode())
        if request.url.path == "/v1/grants":
            return httpx.Response(200, content=json.dumps({"items": [self.grant]}).encode())
        if self.reads_a_grant_with >= 400:
            return httpx.Response(self.reads_a_grant_with, content=b'{"error": "overloaded"}')
        return httpx.Response(200, content=json.dumps(self.grant).encode())


@asynccontextmanager
async def engine_over(
    provider: Batteries, database_url: str, clock: FixedClock
) -> AsyncIterator[BuilderEngine]:
    client = httpx.AsyncClient(base_url=BASE, transport=httpx.MockTransport(provider.handle))
    manager = BatteriesClient(BASE, "t", client=client)
    supply = EngineSupply(allocation_manager=manager, grants_manager=manager, clock=clock)
    settings = {"database_url": database_url, "secret_key": generate_secret_key()}
    async with make_engine(supply=supply, **settings) as engine:
        yield engine
    await client.aclose()


async def kept(engine: BuilderEngine) -> dict[str, Any]:
    """What the engine's database holds of the commands: each record with what it noted, the actors with
    their ids, how many secrets, and the tenants that have an access key."""
    async with engine.store.engine.connect() as conn:  # type: ignore[attr-defined]
        records = await conn.execute(
            select(t.operations.c.operation_ref, t.operations.c.state, t.operations.c.context)
        )
        actors = await conn.execute(
            select(t.actors.c.actor_id, t.actors.c.allocation_id, t.actors.c.api_key_id)
        )
        return {
            "records": [(ref, state, sorted(context)) for ref, state, context in records],
            "actors": [tuple(row) for row in actors],
            "secrets": len(list(await conn.execute(select(t.secrets.c.ref)))),
            "key_tenants": list((await conn.execute(select(t.access_keys.c.tenant_id))).scalars()),
        }


def unusable(refused: pytest.ExceptionInfo[ProviderError], why: str, what: str, field: str) -> None:
    """No refusal, so the ledger keeps the reference; the error names the id and does not quote it."""
    said = f"no usable {field}" if why == "a lone surrogate" else f"{what} the engine cannot store"
    assert refused.value.status_code == 200 and not definite_refusal(refused.value)
    assert said in str(refused.value) and NOT_KEPT[why] not in str(refused.value)


@pytest.mark.parametrize("why", NOT_KEPT)
@pytest.mark.parametrize(("field", "what"), [("allocation_id", "an allocation id"), ("id", "a key id")])
async def test_an_allotment_whose_key_has_an_id_the_store_cannot_keep_stores_nothing_and_keeps_its_reference(
    database_url: str, clock: FixedClock, field: str, what: str, why: str
) -> None:
    provider = Batteries(key=KEY | {field: NOT_KEPT[why]})
    async with engine_over(provider, database_url, clock) as engine:
        with pytest.raises(ProviderError) as refused:
            await engine.payments.allot(ADMIN, ALLOT)
        unusable(refused, why, what, field)
        nothing = {
            "records": [("p-1", "started", ["begun_for", "sent_label"])],  # as it began: nothing noted
            "actors": [],
            "secrets": 0,
            "key_tenants": [],
        }
        assert await kept(engine) == nothing
        with pytest.raises(OperationInProgress):  # the key may exist: unknown, and held
            await engine.payments.allot(ADMIN, ALLOT)
        clock.advance(STALE_S)
        with pytest.raises(ProviderError):  # the resume is answered the same key, and keeps none of it
            await engine.payments.allot(ADMIN, ALLOT)
        assert await kept(engine) == nothing
        assert provider.asked == ["POST /v1/api-keys"] * 2


@pytest.mark.parametrize("why", NOT_KEPT)
async def test_a_rotation_whose_new_key_has_an_id_the_store_cannot_keep_leaves_the_actor_its_key(
    database_url: str, clock: FixedClock, why: str
) -> None:
    provider = Batteries()
    async with engine_over(provider, database_url, clock) as engine:
        await engine.payments.allot(ADMIN, ALLOT)
        before = await kept(engine)
        assert (before["actors"], before["secrets"]) == ([("alice", "alloc_1", "key_1")], 1)
        provider.key = {"id": NOT_KEPT[why], "allocation_id": "alloc_1", "api_key": "lpg_key_2_secret"}
        with pytest.raises(ProviderError) as refused:
            await engine.payments.rotate_payment_key(ADMIN, "default", "alice", "r-1")
        unusable(refused, why, "a key id", "id")
        after = await kept(engine)
        assert (after["actors"], after["secrets"]) == (before["actors"], 1)  # no second secret stored
        [begun] = [noted for ref, state, noted in after["records"] if (ref, state) == ("r-1", "started")]
        assert not [name for name in begun if name.startswith("new_secret")]
        assert "POST /v1/api-keys/key_1/revoke" not in provider.asked  # the key it holds stays live


@pytest.mark.parametrize("why", NOT_KEPT)
async def test_an_onboarding_whose_grant_has_an_id_the_store_cannot_keep_issues_no_key_and_keeps_its_reference(
    database_url: str, clock: FixedClock, why: str
) -> None:
    provider = Batteries(grant=GRANT | {"id": NOT_KEPT[why]})
    async with engine_over(provider, database_url, clock) as engine:
        desk = engine.payments.tenants
        assert desk is not None
        with pytest.raises(ProviderError) as refused:
            await desk.onboard(ADMIN, "ops", USD, operation_ref="o-1")
        unusable(refused, why, "a grant id", "id")
        nothing = {"records": [("o-1", "started", [])], "actors": [], "secrets": 0, "key_tenants": []}
        assert await kept(engine) == nothing
        # an id that is no text is never put in the path of the read-back
        assert ("GET /v1/grants/" in " ".join(provider.asked)) is (why != "a lone surrogate")
        clock.advance(STALE_S)
        with pytest.raises(ProviderError):  # the resume finds the grant by its tag, and keeps none of it
            await desk.onboard(ADMIN, "ops", USD, operation_ref="o-1")
        assert await kept(engine) == nothing
        assert provider.asked.count("POST /v1/grants") == 1  # and creates no second grant


@pytest.mark.parametrize("why", ["201 characters", "a NUL"])
async def test_a_new_grant_that_is_not_read_back_is_not_named_by_an_id_the_store_cannot_keep(
    database_url: str, clock: FixedClock, why: str
) -> None:
    """The create answered the id and the read-back failed: an unknown outcome, whose error names the
    grant by its id so an operator can find it. An id the engine cannot keep is not repeated there."""
    provider = Batteries(grant=GRANT | {"id": NOT_KEPT[why]})
    provider.reads_a_grant_with = 500
    async with engine_over(provider, database_url, clock) as engine:
        desk = engine.payments.tenants
        assert desk is not None
        with pytest.raises(ProviderUnavailable) as unknown:
            await desk.onboard(ADMIN, "ops", USD, operation_ref="o-1")
        assert "created but not read back" in str(unknown.value)
        assert "an id the engine cannot store" in str(unknown.value)
        assert NOT_KEPT[why] not in str(unknown.value) and repr(NOT_KEPT[why])[1:-1] not in str(unknown.value)
        nothing = {"records": [("o-1", "started", [])], "actors": [], "secrets": 0, "key_tenants": []}
        assert await kept(engine) == nothing
    kept_id = Batteries()  # and one it keeps is still named
    kept_id.reads_a_grant_with = 500
    async with engine_over(kept_id, database_url, clock) as engine:
        desk = engine.payments.tenants
        assert desk is not None
        with pytest.raises(ProviderUnavailable, match="grant grant_1 created but not read back"):
            await desk.onboard(ADMIN, "ops", USD, operation_ref="o-2")


async def test_ids_of_exactly_the_stored_length_are_kept(database_url: str, clock: FixedClock) -> None:
    longest = {"id": "k" * 200, "allocation_id": "a" * 200, "api_key": "lpg_key_1_secret"}
    provider = Batteries(key=longest, grant=GRANT | {"id": "g" * 200})
    async with engine_over(provider, database_url, clock) as engine:
        allotted = await engine.payments.allot(ADMIN, ALLOT)
        assert (allotted.allocation_id, allotted.api_key_id) == ("a" * 200, "k" * 200)
        desk = engine.payments.tenants
        assert desk is not None
        onboarded = await desk.onboard(ADMIN, "ops", USD, operation_ref="o-1")
        assert onboarded.tenant_id == "g" * 200
        stored = await kept(engine)
        assert stored["actors"] == [("alice", "a" * 200, "k" * 200)]
        assert stored["key_tenants"] == ["g" * 200]


async def test_the_rest_service_answers_such_a_key_with_502_and_its_repeat_with_409(
    database_url: str, clock: FixedClock
) -> None:
    provider = Batteries(key=KEY | {"allocation_id": NOT_KEPT["201 characters"]})
    async with engine_over(provider, database_url, clock) as engine:
        assert engine.keys is not None
        token = (await engine.keys.bootstrap_admin()).token
        transport = httpx.ASGITransport(
            app=create_app(engine, manage_lifecycle=False), raise_app_exceptions=False
        )
        headers = {"Authorization": f"Bearer {token}"}
        body = {"amount": {"amount": "5", "currency": "USD"}, "operation_ref": "p-1"}
        path = "/v1/clearinghouse/tenants/default/actors/alice/allocation"
        async with httpx.AsyncClient(transport=transport, base_url="http://engine", headers=headers) as http:
            created = await http.post(path, json=body)
            again = await http.post(path, json=body)
        assert (created.status_code, again.status_code) == (502, 409)
        assert "an allocation id the engine cannot store" in created.json()["error"]
        assert "x" * 201 not in created.text


def test_the_columns_that_keep_a_provider_s_id_are_as_long_as_the_check_allows() -> None:
    """``services.grants.stored_id`` lets through what ``contracts.costs.storable_text`` keeps by
    default: 200 characters. Every column one of those ids is written to has to be that long."""
    columns = [
        t.actors.c.allocation_id, t.actors.c.api_key_id, t.actors.c.grant_id, t.actors.c.tenant_id,
        t.attempts.c.allocation_id, t.access_keys.c.tenant_id, t.jobs.c.tenant_id,
    ]  # fmt: skip
    lengths = {str(column): column.type.length for column in columns if isinstance(column.type, String)}
    assert len(lengths) == len(columns) and set(lengths.values()) == {t.ID} and t.ID == 200, (
        "an id the payment provider answers with is checked against 200 characters before it is "
        "stored (services/grants.py stored_id): keep these columns String(ID), with ID = 200"
    )
