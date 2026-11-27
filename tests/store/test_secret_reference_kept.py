"""#158: the reference a supplied ``SecretStore`` returns is kept only when every database keeps it, on
SQLite and PostgreSQL.

A reference is opaque text of any length (#115). PostgreSQL keeps no NUL in text, and no database keeps
a lone surrogate. A store that returned one did so after the provider had minted the key: SQLite stored
the NUL, and PostgreSQL refused the actor's row with the reference already noted in the command's
record, so every resume took that reference up again and no repeat could finish. The engine now checks
the reference where ``put`` returns it: it deletes the copy from the store again and fails the command
with nothing noted, so a repeat finishes once the store returns text.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from livepeer_builder import BuilderEngine, EngineSupply
from livepeer_builder.config import generate_secret_key
from livepeer_builder.contracts import ActorContext, AllotRequest, Money, SecretRef
from livepeer_builder.errors import EngineError, OperationInProgress
from livepeer_builder.http import create_app
from livepeer_builder.services.ledger import definite_refusal
from livepeer_builder.store import schema as t
from livepeer_builder.testing import FakeAllocationManager, FixedClock, MemorySecretStore, make_engine

ADMIN = ActorContext(actor_id="operator", credential_kind="clearinghouse_admin")
USD = Money(amount=Decimal(5), currency="USD")
ALLOT = AllotRequest(operation_ref="p-1", tenant_id="default", actor_id="alice", amount=USD)
NOT_KEPT = {"a NUL": "vault:kv/a\x00b", "a lone surrogate": "vault:kv/a\ud800b"}
STALE_S = 121  # past LPB_OPERATION_STALE_S: a repeat may resume a command whose outcome is unknown


class Vault(MemorySecretStore):
    """A supplied store that names the next secret ``names`` while that is set, and whose ``delete``
    fails while ``away`` is."""

    def __init__(self) -> None:
        super().__init__()
        self.names: str | None = None
        self.away = False

    async def put(self, name: str, secret: str) -> SecretRef:
        if self.names is None:
            return await super().put(name, secret)
        self.values[self.names] = secret
        return SecretRef.model_construct(ref=self.names)  # as a store that checks nothing builds one

    async def delete(self, ref: SecretRef) -> None:
        if self.away:
            raise ConnectionError("the vault is away")
        await super().delete(ref)


@asynccontextmanager
async def engine_with(
    vault: Vault, database_url: str, clock: FixedClock
) -> AsyncIterator[tuple[BuilderEngine, FakeAllocationManager]]:
    provider = FakeAllocationManager()
    provider.add_grant("default")
    supply = EngineSupply(
        secret_store=vault, allocation_manager=provider, grants_manager=provider, clock=clock
    )
    settings = {"database_url": database_url, "secret_key": generate_secret_key()}
    async with make_engine(supply=supply, **settings) as engine:
        yield engine, provider


async def noted(engine: BuilderEngine, operation_ref: str) -> tuple[str, list[str]]:
    """The command's record: its state, and the names of what it noted."""
    query = select(t.operations.c.state, t.operations.c.context).where(
        t.operations.c.operation_ref == operation_ref
    )
    async with engine.store.engine.connect() as conn:  # type: ignore[attr-defined]
        state, context = (await conn.execute(query)).one()
    return state, sorted(context)


def is_the_stores_fault(failed: pytest.ExceptionInfo[ValueError], ref: str) -> None:
    """No engine error, so no refusal and HTTP 500, as when a supplied store raises; the reference is
    not quoted. This provider replays a key to the repeat of the call that minted it, as Batteries
    does, so the error says to repeat the same reference, and names no way out that is a
    clearinghouse's."""
    message = str(failed.value)
    assert not isinstance(failed.value, EngineError) and not definite_refusal(failed.value)
    assert ref not in message and repr(ref)[1:-1] not in message
    assert "replays it" in message and "repeat this request, under the same operation_ref" in message
    assert "clearinghouse" not in message and "new operation_ref" not in message


@pytest.mark.parametrize("why", NOT_KEPT)
async def test_an_allotment_whose_secret_reference_no_database_keeps_notes_nothing_and_finishes_on_a_repeat(
    database_url: str, clock: FixedClock, why: str
) -> None:
    vault = Vault()
    async with engine_with(vault, database_url, clock) as (engine, provider):
        vault.names = NOT_KEPT[why]
        with pytest.raises(ValueError, match="the secret store returned a reference") as failed:
            await engine.payments.allot(ADMIN, ALLOT)
        is_the_stores_fault(failed, NOT_KEPT[why])
        assert vault.values == {}  # the copy under that reference is deleted again
        assert await noted(engine, "p-1") == ("started", ["begun_for", "sent_label"])  # as it began
        assert await engine.store.get_actor("default", "alice") is None
        assert len(provider.keys) == 1  # the provider minted the key: the outcome is unknown, and held
        with pytest.raises(OperationInProgress):
            await engine.payments.allot(ADMIN, ALLOT)

        vault.names = None  # the store is put right
        clock.advance(STALE_S)
        allotted = await engine.payments.allot(ADMIN, ALLOT)

        [(_, minted, _)] = provider.keys.values()  # the provider replayed the one key
        assert await vault.get(allotted.secret_ref) == minted and len(vault.values) == 1
        assert (await noted(engine, "p-1"))[0] == "done"


@pytest.mark.parametrize("why", NOT_KEPT)
async def test_a_rotation_whose_secret_reference_no_database_keeps_leaves_the_actor_its_key_and_finishes_on_a_repeat(
    database_url: str, clock: FixedClock, why: str
) -> None:
    vault = Vault()
    async with engine_with(vault, database_url, clock) as (engine, provider):
        before = await engine.payments.allot(ADMIN, ALLOT)
        vault.names = NOT_KEPT[why]
        with pytest.raises(ValueError, match="the secret store returned a reference") as failed:
            await engine.payments.rotate_payment_key(ADMIN, "default", "alice", "r-1")
        is_the_stores_fault(failed, NOT_KEPT[why])
        assert list(vault.values) == [before.secret_ref.ref]  # only the key the actor holds
        assert await engine.store.get_actor("default", "alice") == before
        state, names = await noted(engine, "r-1")
        assert state == "started" and not [name for name in names if name.startswith("new_secret")]
        assert provider.keys[before.api_key_id][2] is False  # the key it holds is not revoked

        vault.names = None
        clock.advance(STALE_S)
        rotated = await engine.payments.rotate_payment_key(ADMIN, "default", "alice", "r-1")

        assert rotated.api_key_id != before.api_key_id and len(provider.keys) == 2  # one key minted
        assert provider.keys[before.api_key_id][2] is True
        assert list(vault.values) == [rotated.secret_ref.ref]
        assert await vault.get(rotated.secret_ref) == provider.keys[rotated.api_key_id][1]


async def test_a_store_that_cannot_delete_the_copy_answers_with_its_own_error_and_the_copy_stays(
    database_url: str, clock: FixedClock
) -> None:
    """The delete is the store's too. When it fails, that error is the command's answer: still no
    refusal, and still nothing noted, so the repeat finishes. The copy under the reference no database
    keeps is then left in the store, where nothing names it."""
    vault = Vault()
    async with engine_with(vault, database_url, clock) as (engine, provider):
        vault.names, vault.away = NOT_KEPT["a NUL"], True
        with pytest.raises(ConnectionError, match="the vault is away"):
            await engine.payments.allot(ADMIN, ALLOT)
        assert list(vault.values) == [NOT_KEPT["a NUL"]]
        assert await noted(engine, "p-1") == ("started", ["begun_for", "sent_label"])

        vault.names, vault.away = None, False
        clock.advance(STALE_S)
        allotted = await engine.payments.allot(ADMIN, ALLOT)

        assert sorted(vault.values) == sorted([NOT_KEPT["a NUL"], allotted.secret_ref.ref])
        assert len(provider.keys) == 1


async def test_a_secret_reference_of_any_length_is_kept_through_an_allotment_and_a_rotation(
    database_url: str, clock: FixedClock
) -> None:
    """Length is no rule (#115): the actor's column is unbounded text, and the command's record is JSON."""
    vault = Vault()
    async with engine_with(vault, database_url, clock) as (engine, _):
        vault.names = "vault:kv/data/livepeer/" + "p" * 5000
        allotted = await engine.payments.allot(ADMIN, ALLOT)
        vault.names = "vault:kv/data/livepeer/" + "q" * 5000
        rotated = await engine.payments.rotate_payment_key(ADMIN, "default", "alice", "r-1")
        stored = await engine.store.get_actor("default", "alice")
        assert stored == rotated and len(rotated.secret_ref.ref) > 5000 < len(allotted.secret_ref.ref)
        assert list(vault.values) == [rotated.secret_ref.ref]


async def test_the_rest_service_answers_such_a_reference_with_500_and_the_repeat_with_201(
    database_url: str, clock: FixedClock
) -> None:
    vault = Vault()
    async with engine_with(vault, database_url, clock) as (engine, _):
        assert engine.keys is not None
        token = (await engine.keys.bootstrap_admin()).token
        transport = httpx.ASGITransport(
            app=create_app(engine, manage_lifecycle=False), raise_app_exceptions=False
        )
        headers = {"Authorization": f"Bearer {token}"}
        body: dict[str, Any] = {"amount": {"amount": "5", "currency": "USD"}, "operation_ref": "p-1"}
        path = "/v1/clearinghouse/tenants/default/actors/alice/allocation"
        async with httpx.AsyncClient(transport=transport, base_url="http://engine", headers=headers) as http:
            vault.names = NOT_KEPT["a NUL"]
            failed = await http.post(path, json=body)
            held = await http.post(path, json=body)
            vault.names = None
            clock.advance(STALE_S)
            finished = await http.post(path, json=body)
        assert (failed.status_code, held.status_code, finished.status_code) == (500, 409, 201)
        assert "vault:kv" not in failed.text
