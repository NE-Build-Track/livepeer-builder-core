"""#168: the key a clearinghouse hands over, at the engine that is handed it (D85 point 1, D87 point 1).

An allotment or a payment key rotation at a standalone clearinghouse answers the allocation's own
Batteries key once, and the clearinghouse stores none. The enterprise's engine runs its ordinary commands
over the remote ``AllocationManager``, so the key goes into the engine's own secret store, with
``credential_custody: secret_store``, and the engine signs with it at the signer.

What this file holds is what follows from "once":

- the key is in the engine's secret store and nowhere else: no table, no log line, no error;
- an answer without a key (``replayed``) is never a success and never an actor record. A lost key is
  replaced by a rotation, or, for an allotment the engine has no record of, by revoking the allocation at
  the clearinghouse and allotting again;
- a resumed command at the clearinghouse hands the key over again (``resumed``), and the engine stores it;
- the engine's own resume after it stored the key finishes from its own copy;
- two rotations of one actor leave no key live that the engine stored and dropped.

The two engines share nothing: each has a store and a secret store of its own (``payment_ports.py``).
"""

import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, cast

import pytest
from sqlalchemy import select

from livepeer_builder import BuilderEngine
from livepeer_builder.contracts import ActorAllocation, AllotRequest, TopUpRequest
from livepeer_builder.errors import (
    AlreadyAllotted,
    InvalidRequest,
    NotProvisioned,
    OperationInProgress,
    ProviderError,
    ProviderOutcomeUnknown,
)
from livepeer_builder.services.ledger import definite_refusal
from livepeer_builder.store import SqlStore
from livepeer_builder.store import schema as t
from livepeer_builder.testing import FixedClock
from tests.unit.payment_ports import BOTH_ROLE, CLEARINGHOUSE_URL, OPERATOR, Ports, open_ports, usd

ALLOCATION = "/actors/alice/allocation"
ROTATE = ALLOCATION + "/rotate-payment-key"
STALE_S = 121  # past LPB_OPERATION_STALE_S: a repeat may resume a command whose outcome is unknown


@pytest.fixture
async def ports() -> AsyncIterator[Ports]:
    async with open_ports("remote") as opened:
        yield opened


def allot_request(
    ports: Ports, actor_id: str = "alice", amount: str = "5", ref: str | None = None
) -> AllotRequest:
    return AllotRequest(
        tenant_id=ports.tenant_id,
        actor_id=actor_id,
        amount=usd(amount),
        operation_ref=ref or f"allot-{actor_id}",
    )


def house(ports: Ports) -> BuilderEngine:
    assert ports.clearinghouse is not None
    return ports.clearinghouse


def later(ports: Ports, seconds: float = STALE_S, *, there_too: bool = False) -> None:
    """Move the engine's clock, and the clearinghouse's with ``there_too``: each has its own."""
    cast(FixedClock, ports.engine.clock).advance(seconds)
    if there_too:
        cast(FixedClock, house(ports).clock).advance(seconds)


async def allot(ports: Ports, ref: str | None = None, amount: str = "5") -> ActorAllocation:
    return await ports.engine.payments.allot(ports.admin, allot_request(ports, ref=ref, amount=amount))


async def rotate(ports: Ports, ref: str) -> ActorAllocation:
    return await ports.engine.payments.rotate_payment_key(ports.admin, ports.tenant_id, "alice", ref)


async def stored_key(ports: Ports) -> str:
    """The key the engine would sign with for alice now."""
    key = await ports.credentials.credential(ports.actor("alice"))
    assert key.token is not None
    return key.token


def handed(ports: Ports, ending: str) -> list[dict[str, Any]]:
    """The clearinghouse's 2xx answers to the POSTs whose path ends so, in order."""
    posts = [
        (request, answer)
        for request, answer in zip(ports.requests, ports.answers, strict=True)
        if request.method == "POST" and request.url.path.endswith(ending)
    ]
    return [json.loads(body) for _, (status, body) in posts if status < 300]


def live_keys(ports: Ports) -> set[str]:
    """Every key the provider still takes a payment with."""
    return {
        secret
        for allocation_id, secret, revoked in ports.provider.keys.values()
        if not revoked and ports.provider.allocations[allocation_id].status != "revoked"
    }


async def everything_stored(store: SqlStore) -> str:
    async with store.engine.connect() as conn:
        return "\n".join(
            [repr((await conn.execute(select(table))).all()) for table in t.metadata.sorted_tables]
        )


async def record_of(ports: Ports, ref: str) -> Any:
    return await ports.engine.store.get_operation(OPERATOR.tenant_id, OPERATOR.actor_id, ref)


def lose_once[**P, R](
    monkeypatch: pytest.MonkeyPatch, target: object, name: str, *, apply: bool = False
) -> None:
    """The next call of ``target.name`` fails, before it applies or (``apply``) after."""
    original: Callable[P, Awaitable[R]] = getattr(target, name)

    async def lost(*args: P.args, **kwargs: P.kwargs) -> R:
        monkeypatch.setattr(target, name, original)
        if apply:
            await original(*args, **kwargs)
        raise ConnectionError("the database went away")

    monkeypatch.setattr(target, name, lost)


# -- handed over once, and kept by the engine alone -------------------------------------------------------


async def test_the_two_engines_share_no_store_and_no_secret_store(ports: Ports) -> None:
    there = house(ports)
    assert ports.engine is not there
    assert ports.engine.store is not there.store
    assert ports.secrets is not None and ports.clearinghouse_secrets is not None
    assert ports.secrets is not ports.clearinghouse_secrets
    assert ports.engine.payments._secrets is ports.secrets
    assert there.payments._secrets is ports.clearinghouse_secrets
    await allot(ports)
    assert await there.store.get_actor(ports.tenant_id, "alice") != await ports.engine.store.get_actor(
        ports.tenant_id, "alice"
    )  # two records of one allocation: the receipt there, the store's handle here


async def test_an_allotment_hands_the_engine_the_allocations_own_key_and_the_engine_stores_it(
    ports: Ports,
) -> None:
    answer = await allot(ports)
    assert ports.secrets is not None and ports.clearinghouse_secrets is not None

    [(_, minted, _)] = ports.provider.keys.values()
    assert minted.startswith("lpg_") and await stored_key(ports) == minted
    # here: the engine's own store holds it, and the record says so
    mine = await ports.engine.store.get_actor(ports.tenant_id, "alice")
    assert mine is not None and mine == answer and mine.credential_custody == "secret_store"
    assert await ports.secrets.get(mine.secret_ref) == minted
    # there: a receipt, and nothing anybody could resolve
    theirs = await house(ports).store.get_actor(ports.tenant_id, "alice")
    assert theirs is not None and theirs.credential_custody == "work_engine"
    assert (theirs.allocation_id, theirs.api_key_id) == (mine.allocation_id, mine.api_key_id)
    assert ports.clearinghouse_secrets.values == {}
    assert minted not in await everything_stored(cast(SqlStore, house(ports).store))
    with pytest.raises(NotProvisioned, match="held by the work engine"):
        await house(ports).payments.credential(ports.actor("alice"))
    # and it crossed the wire once, in one answer
    [first] = handed(ports, ALLOCATION)
    assert first["allocation_api_key"] == minted and first["replayed"] is False


async def test_a_rotation_hands_the_engine_the_new_key_and_the_clearinghouse_still_keeps_none(
    ports: Ports,
) -> None:
    await allot(ports)
    old = await stored_key(ports)

    rotated = await rotate(ports, "r-1")

    new = await stored_key(ports)
    assert ports.secrets is not None and ports.clearinghouse_secrets is not None
    assert new != old and new.startswith("lpg_") and live_keys(ports) == {new}
    assert rotated.credential_custody == "secret_store"
    assert list(ports.secrets.values.values()) == [
        new
    ]  # the old key's secret is gone from the engine's store
    assert ports.clearinghouse_secrets.values == {}
    stored_there = await everything_stored(cast(SqlStore, house(ports).store))
    assert old not in stored_there and new not in stored_there


async def test_nothing_the_engine_stores_logs_or_raises_holds_a_handed_key(
    ports: Ports, caplog: pytest.LogCaptureFixture
) -> None:
    """After an allotment and a rotation through the remote adapter: the engine's database, both engines'
    log records and the text of every error a later command raises hold neither key. The engine's secret
    store here is in memory, so its database holds no form of a secret at all."""
    caplog.set_level(logging.DEBUG)
    await allot(ports)
    first = await stored_key(ports)
    await rotate(ports, "r-1")
    second = await stored_key(ports)
    payments = ports.engine.payments

    errors: list[BaseException] = []
    refused: list[Callable[[], Awaitable[object]]] = [
        lambda: allot(ports, ref="again", amount="7"),  # already allotted
        lambda: rotate(ports, "allot-alice"),  # a reference another command holds
        lambda: payments.top_up(
            ports.admin, ports.tenant_id, "alice", TopUpRequest(amount=usd(500), operation_ref="too-much")
        ),
    ]
    ports.lose_the_answer_to(ROTATE)
    refused.append(lambda: rotate(ports, "r-lost"))  # the answer, with a key in it, never arrives
    for command in refused:
        with pytest.raises(Exception) as error:  # noqa: PT011 - each kind is asserted below
            await command()
        errors.append(error.value)
    later(ports)
    with pytest.raises(InvalidRequest) as replayed:  # the repeat is answered without the key
        await rotate(ports, "r-lost")
    errors.append(replayed.value)
    assert [type(error).__name__ for error in errors] == [
        "AlreadyAllotted",
        "OperationConflict",
        "ProviderError",
        "ProviderOutcomeUnknown",
        "InvalidRequest",
    ]

    # the key of the answer that was lost: the last one any answer carried
    third = [answer["allocation_api_key"] for answer in handed(ports, ROTATE) if not answer["replayed"]][-1]
    assert third.startswith("lpg_") and third not in (first, second)
    stored = await everything_stored(cast(SqlStore, ports.engine.store))
    logged = "\n".join(f"{record.getMessage()} {vars(record)}" for record in caplog.records)
    said = "\n".join(f"{error} {error!r} {vars(error)}" for error in errors)
    for key in (first, second, third):
        assert key not in stored and key not in logged and key not in said
    assert "lpg_" not in stored and "lpg_" not in logged and "lpg_" not in said
    assert ports.token not in logged and ports.token not in said


# -- an answer that never arrives: the clearinghouse finished, and its repeat carries no key ---------------


async def test_a_lost_allotment_is_never_a_success_and_never_a_record_that_points_at_nothing(
    ports: Ports,
) -> None:
    ports.lose_the_answer_to(ALLOCATION)
    with pytest.raises(ProviderOutcomeUnknown):
        await allot(ports)
    assert len(ports.provider.allocations) == 1  # it was applied, and the engine does not know
    with pytest.raises(OperationInProgress):  # the reference stays held: no second allotment under it
        await allot(ports)
    assert len(ports.sent()) == 1  # and the adapter sent the command once

    later(ports)
    with pytest.raises(InvalidRequest) as lost:
        await allot(ports)

    # the clearinghouse answered its finished command: the allocation, and no key
    again = handed(ports, ALLOCATION)[-1]
    assert again["allocation_api_key"] is None and again["replayed"] is True
    # plainly, to the admin: what happened, and what replaces the key
    message = str(lost.value)
    assert "handed its key to an answer that never reached this engine" in message
    assert "Revoke the allocation at the clearinghouse" in message
    assert "allot again here under a new operation_ref" in message
    assert again["allocation_id"] in message
    # nothing saved, nothing stored, nothing allotted twice, and no job can pay
    assert await ports.engine.store.get_actor(ports.tenant_id, "alice") is None
    assert ports.secrets is not None and ports.secrets.values == {}
    assert len(ports.provider.allocations) == 1 and ports.provider.unallocated(ports.tenant_id) == 95
    with pytest.raises(NotProvisioned):
        await ports.credentials.credential(ports.actor("alice"))
    # the engine's own record of the command stays started: it finished nothing
    held = await record_of(ports, "allot-alice")
    assert held is not None and held.state == "started"


async def test_a_lost_allotment_is_put_right_by_revoking_it_at_the_clearinghouse_and_allotting_again(
    ports: Ports,
) -> None:
    """The first review thread of #182. The engine has no record of the actor, so it can neither rotate
    nor revoke it here; the tenant's admin revokes the allocation at the clearinghouse, with the key the
    engine holds, and the engine allots again under a new reference. The clearinghouse keys its ledger
    on the reference it is sent, which follows the admin's, so the second allotment is a new command
    there and is handed a key."""
    ports.lose_the_answer_to(ALLOCATION)
    with pytest.raises(ProviderOutcomeUnknown):
        await allot(ports)
    lost_allocation = next(iter(ports.provider.allocations))
    client = ports.manager._wire._client  # type: ignore[attr-defined]
    revoke = f"http://clearinghouse.test/v1/clearinghouse/tenants/{ports.tenant_id}{ALLOCATION}/revoke"
    answer = await client.post(revoke, headers={"Authorization": f"Bearer {ports.token}"})
    assert answer.status_code == 200 and answer.json()["status"] == "revoked"
    assert ports.provider.unallocated(ports.tenant_id) == 100  # what was allotted is back in the grant

    anew = await allot(ports, ref="allot-alice-again", amount="4")

    assert anew.allocation_id != lost_allocation and anew.status == "active"
    assert ports.provider.unallocated(ports.tenant_id) == 96
    key = await stored_key(ports)
    assert await ports.pays_for(key) == anew.allocation_id and live_keys(ports) == {key}
    # the first reference's record can now finish: its repeat is told the actor is allotted, and is freed
    later(ports)
    with pytest.raises(AlreadyAllotted):
        await allot(ports)
    assert await record_of(ports, "allot-alice") is None
    assert len(ports.provider.allocations) == 2 and ports.provider.unallocated(ports.tenant_id) == 96


async def test_a_lost_rotation_is_refused_plainly_and_a_rotation_under_a_new_reference_replaces_the_key(
    ports: Ports,
) -> None:
    allotted = await allot(ports)
    old = await stored_key(ports)
    ports.lose_the_answer_to(ROTATE)
    with pytest.raises(ProviderOutcomeUnknown):
        await rotate(ports, "r-1")
    # the clearinghouse rotated: the key the engine still holds pays for nothing now
    assert await ports.pays_for(old) is None and len(live_keys(ports)) == 1

    later(ports)
    with pytest.raises(InvalidRequest) as lost:
        await rotate(ports, "r-1")

    assert handed(ports, ROTATE)[-1]["replayed"] is True
    message = str(lost.value)
    assert "handed its key to an answer that never reached this engine" in message
    assert "A lost key is replaced by a rotation" in message and "under a new operation_ref" in message
    assert await stored_key(ports) == old  # the engine keeps what it has: nothing was written over it
    held = await record_of(ports, "r-1")
    assert held is not None and held.state == "started"

    # D87 point 1: a lost key is replaced by a rotation. From this engine, end to end
    rotated = await rotate(ports, "r-2")

    new = await stored_key(ports)
    assert rotated.allocation_id == allotted.allocation_id and rotated.credential_custody == "secret_store"
    assert await ports.pays_for(new) == allotted.allocation_id and live_keys(ports) == {new}
    assert ports.secrets is not None and list(ports.secrets.values.values()) == [new]
    assert ports.provider.allocations[allotted.allocation_id].granted == 5  # no rotation moves money

    # the first rotation's record never finishes: its repeat is refused in the same words, for good, and
    # changes nothing. An operator leaves it
    later(ports)
    with pytest.raises(InvalidRequest, match="never reached this engine"):
        await rotate(ports, "r-1")
    held = await record_of(ports, "r-1")
    assert held is not None and held.state == "started"
    assert await stored_key(ports) == new and live_keys(ports) == {new}


# -- the clearinghouse's own run had not finished: its repeat is a resume, and hands the key over ----------


async def test_an_allotment_the_clearinghouse_resumes_hands_the_key_over_and_the_engine_stores_it(
    ports: Ports, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The clearinghouse's run died after Batteries made the allocation and before it saved the actor. Its
    answer was a 500, an unknown outcome here. The repeat carries the same reference, the clearinghouse
    resumes, Batteries replays the key, and the answer says ``resumed`` and carries it."""
    lose_once(monkeypatch, house(ports).store, "allot_actor")
    with pytest.raises(ProviderOutcomeUnknown):
        await allot(ports)
    assert len(ports.provider.allocations) == 1
    assert await ports.engine.store.get_actor(ports.tenant_id, "alice") is None

    later(ports)  # the engine may resume; the clearinghouse's own record is not stale yet
    with pytest.raises(ProviderOutcomeUnknown, match="still running"):
        await allot(ports)
    later(ports, there_too=True)
    answer = await allot(ports)

    resumed = handed(ports, ALLOCATION)[-1]
    assert resumed["resumed"] is True and resumed["replayed"] is False
    key = await stored_key(ports)
    assert key == resumed["allocation_api_key"] and await ports.pays_for(key) == answer.allocation_id
    assert len(ports.provider.allocations) == 1 and ports.provider.unallocated(ports.tenant_id) == 95
    assert ports.provider.calls.count("create_key_for_grant") == 2  # the second is Batteries' replay
    # each POST carried the one reference
    refs = {json.loads(r.content)["operation_ref"] for r in ports.requests if r.method == "POST"}
    assert len(refs) == 1


async def test_a_rotation_the_clearinghouse_resumes_hands_the_key_over_and_the_engine_stores_it(
    ports: Ports, monkeypatch: pytest.MonkeyPatch
) -> None:
    allotted = await allot(ports)
    old = await stored_key(ports)
    lose_once(monkeypatch, house(ports).store, "set_actor_key")
    with pytest.raises(ProviderOutcomeUnknown):
        await rotate(ports, "r-1")
    assert await stored_key(ports) == old

    later(ports, there_too=True)
    await rotate(ports, "r-1")

    resumed = handed(ports, ROTATE)[-1]
    assert resumed["resumed"] is True and resumed["replayed"] is False
    new = await stored_key(ports)
    assert new == resumed["allocation_api_key"] and new != old
    assert await ports.pays_for(new) == allotted.allocation_id and live_keys(ports) == {new}


# -- the engine's own resume, when its run died after the clearinghouse answered ---------------------------


async def test_a_rotation_the_engine_resumes_after_it_stored_the_key_finishes_from_its_own_copy(
    ports: Ports, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The engine's run stored the handed key and pointed the actor at it, then died. Its repeat sends the
    same reference; the clearinghouse finished that command and answers without the key. The engine has
    the key of exactly that answer in its own store, so the command finishes, and nothing rotates twice."""
    await allot(ports)
    old = await stored_key(ports)
    assert ports.secrets is not None
    lose_once(monkeypatch, ports.secrets, "delete")  # the last step: forgetting the old key's secret
    with pytest.raises(ConnectionError):
        await rotate(ports, "r-1")
    new = await stored_key(ports)
    assert new != old and live_keys(ports) == {new}

    later(ports)
    rotated = await rotate(ports, "r-1")

    assert handed(ports, ROTATE)[-1]["replayed"] is True  # the clearinghouse handed nothing over again
    assert rotated.api_key_id == handed(ports, ROTATE)[-1]["api_key_id"]
    assert await stored_key(ports) == new and live_keys(ports) == {new}
    assert list(ports.secrets.values.values()) == [new]  # the old key's secret is gone at last
    assert ports.provider.calls.count("create_key") == 1  # one rotation at the provider
    done = await record_of(ports, "r-1")
    assert done is not None and done.state == "done"


async def test_an_allotment_the_engine_resumes_after_it_saved_the_actor_asks_the_clearinghouse_nothing(
    ports: Ports, monkeypatch: pytest.MonkeyPatch
) -> None:
    lose_once(monkeypatch, ports.engine.store, "complete_operation")
    with pytest.raises(ConnectionError):
        await allot(ports)
    key = await stored_key(ports)
    sent = len(ports.requests)

    later(ports)
    answer = await allot(ports)

    assert len(ports.requests) == sent  # its own record of the actor is the answer
    assert answer.credential_custody == "secret_store" and await stored_key(ports) == key


# -- two rotations of one actor ----------------------------------------------------------------------------


async def test_a_rotation_that_another_overtook_leaves_no_key_live_that_the_engine_stored_and_dropped(
    ports: Ports, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second review thread of #182. Rotation B begins, and before it reaches the clearinghouse
    rotation A runs whole: the clearinghouse replaces the first key with A's, and the engine stores A's.
    B's call then replaces A's key at the clearinghouse, but the engine writes a rotation's key only over
    the key that rotation began from, so B writes nothing, as at Batteries. There the loser revokes its
    own key by its id. A clearinghouse ends a key only by replacing it, and the loser's key is the one it
    takes now: the adapter's ``revoke_key`` replaces it and keeps nothing of the answer, so the key the
    engine stored and then dropped is dead, as the refusal says. The actor then holds a key the
    clearinghouse has ended, and the next rotation, which the refusal asks for, puts that right."""
    allotted = await allot(ports)
    first = await stored_key(ports)
    manager = ports.manager
    original = manager.for_actor  # type: ignore[attr-defined]
    overtaken: list[ActorAllocation] = []

    def b_waits_for_a(*args: Any, **kwargs: Any) -> Any:
        """The manager rotation B is given: its call to the clearinghouse lets rotation A run first."""
        monkeypatch.setattr(manager, "for_actor", original)
        mine = original(*args, **kwargs)
        create_key = mine.create_key

        async def after_a(*call: Any) -> Any:
            overtaken.append(await rotate(ports, "r-a"))
            return await create_key(*call)

        monkeypatch.setattr(mine, "create_key", after_a)
        return mine

    monkeypatch.setattr(manager, "for_actor", b_waits_for_a)
    with pytest.raises(InvalidRequest) as lost:
        await rotate(ports, "r-b")

    [a] = overtaken
    a_key = await stored_key(ports)
    assert "another key rotation" in str(lost.value) and "this rotation's key is revoked" in str(lost.value)
    assert (await ports.engine.store.get_actor(ports.tenant_id, "alice")) == a  # B wrote nothing
    assert ports.secrets is not None and list(ports.secrets.values.values()) == [a_key]
    # every key this engine ever held for the actor is dead at the provider: the first, A's, and B's
    engine_saw = [answer["allocation_api_key"] for answer in handed(ports, ROTATE)[:2]]
    assert len(set(engine_saw)) == 2 and a_key in engine_saw
    for key in (first, *engine_saw):
        assert await ports.pays_for(key) is None
    # B's key was ended the one way a clearinghouse ends a key: by a third rotation, whose key nobody kept
    assert [path.rsplit("/", 1)[1] for path in ports.sent()] == ["allocation"] + ["rotate-payment-key"] * 3
    assert len(live_keys(ports)) == 1 and live_keys(ports).isdisjoint({first, *engine_saw})
    assert ports.provider.allocations[allotted.allocation_id].granted == 5

    # the refusal asks for a rotation under a new reference, and that leaves the engine holding the one
    # key the clearinghouse takes
    await rotate(ports, "r-c")
    final = await stored_key(ports)
    assert await ports.pays_for(final) == allotted.allocation_id and live_keys(ports) == {final}
    assert list(ports.secrets.values.values()) == [final]


async def test_the_rotation_that_wrote_first_in_the_engine_and_last_at_the_clearinghouse_is_the_one_kept(
    ports: Ports, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other order: rotation B runs whole while A's answer is on its way back. B's key is the one the
    clearinghouse takes and the one the engine stored; A, which then writes nothing, has a key the
    clearinghouse already ended, so nothing is sent to end it."""
    allotted = await allot(ports)
    store = ports.engine.store
    original = store.set_actor_key  # type: ignore[attr-defined]

    async def b_first(*args: Any, **kwargs: Any) -> Any:
        monkeypatch.setattr(store, "set_actor_key", original)
        await rotate(ports, "r-b")
        return await original(*args, **kwargs)

    monkeypatch.setattr(store, "set_actor_key", b_first)
    with pytest.raises(InvalidRequest, match="another key rotation"):
        await rotate(ports, "r-a")

    kept = await stored_key(ports)
    assert await ports.pays_for(kept) == allotted.allocation_id and live_keys(ports) == {kept}
    assert ports.secrets is not None and list(ports.secrets.values.values()) == [kept]
    assert [path.rsplit("/", 1)[1] for path in ports.sent()] == ["allocation"] + ["rotate-payment-key"] * 2


# -- a clearinghouse that hands no key over ----------------------------------------------------------------


async def test_a_clearinghouse_that_keeps_the_keys_itself_is_an_answer_the_engine_cannot_use() -> None:
    """An engine in its ``work`` role pointed at an engine in its ``both`` role (#169): self-hosted
    payments, which stores each allocation's key for its own jobs and answers none. The enterprise's
    engine is handed nothing: the allotment happened there, so this is no refusal and frees no
    reference, and the error says which role the other engine must be in."""
    from livepeer_builder.http import create_app

    async with open_ports(
        "remote", lambda engine: create_app(engine, manage_lifecycle=False, listener="public"), BOTH_ROLE
    ) as ports:
        assert (ports.engine.role, house(ports).role) == ("work", "both")
        with pytest.raises(ProviderError) as unusable:
            await allot(ports)

        assert "LPB_ROLE=clearinghouse" in unusable.value.message
        assert "LPB_CLEARINGHOUSE_URL" in unusable.value.message
        assert CLEARINGHOUSE_URL not in unusable.value.message  # the setting is named, not its value
        assert not definite_refusal(unusable.value)
        assert "keeps each allocation's key itself" in unusable.value.message
        assert len(ports.provider.allocations) == 1  # allotted there, and held there
        there = await house(ports).store.get_actor(ports.tenant_id, "alice")
        assert there is not None and there.credential_custody == "secret_store"
        assert await ports.engine.store.get_actor(ports.tenant_id, "alice") is None
        held = await record_of(ports, "allot-alice")
        assert held is not None and held.state == "started"
