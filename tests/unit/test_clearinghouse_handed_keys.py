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
- the engine's own resume after it stored the key finishes from its own copy, and asks the clearinghouse
  nothing;
- when two rotations of one actor overlap, the clearinghouse's order decides which key the engine keeps
  (Mike, 23 November 2026): the engine ends up holding the key the clearinghouse takes, no rotation is
  ever made to end a key, and no secret is left in the engine's store that nothing points at.

The two engines share nothing: each has a store and a secret store of its own (``payment_ports.py``).
"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, cast

import httpx
import pytest
from sqlalchemy import select

from livepeer_builder import BuilderEngine
from livepeer_builder.adapters.clearinghouse import ClearinghouseAllocations
from livepeer_builder.contracts import ActorAllocation, AllotRequest, SecretRef, TopUpRequest
from livepeer_builder.errors import (
    AlreadyAllotted,
    InvalidRequest,
    NotFound,
    NotProvisioned,
    OperationInProgress,
    ProviderError,
    ProviderOutcomeUnknown,
    ProviderUnavailable,
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
    assert ports.wire is not None
    client = ports.wire._client
    revoke = f"http://clearinghouse.test/v1/clearinghouse{ALLOCATION}/revoke"  # the tenant's own route
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
    """The engine's run stored the handed key and pointed the actor at it, then died. The clearinghouse
    finished that command and would answer its repeat without the key, so the engine does not ask: its
    own record notes the key of that very call, and the command finishes from the engine's own copy."""
    await allot(ports)
    old = await stored_key(ports)
    assert ports.secrets is not None
    lose_once(monkeypatch, ports.secrets, "delete")  # the last step: forgetting the old key's secret
    with pytest.raises(ConnectionError):
        await rotate(ports, "r-1")
    new = await stored_key(ports)
    assert new != old and live_keys(ports) == {new}
    sent = len(ports.requests)

    later(ports)
    rotated = await rotate(ports, "r-1")

    assert len(ports.requests) == sent  # nothing is asked of the clearinghouse
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


# -- two rotations of one actor: the clearinghouse's order decides which key the engine keeps -------------
#
# Rotations A and B of one actor, each under its own reference. The clearinghouse runs them one after the
# other: the later one's key is the one the allocation takes, and the earlier one's is ended. In the engine
# a rotation writes its key only over the key it began from, so the first to write wins there. When the
# rotation the clearinghouse ran first also writes first, the other holds the only key the clearinghouse
# takes. It asks the clearinghouse which key the allocation holds, and stores its own over the ended one.


async def settled(ports: Ports, *, rotations: int) -> str:
    """What every outcome below comes to: the clearinghouse takes exactly one key; the engine's record of
    the actor and its secret store hold exactly that key, and no other secret; the clearinghouse rotated
    ``rotations`` times and no more, so no key was ever minted to end another. Returns that key."""
    [taken] = live_keys(ports)
    assert await stored_key(ports) == taken
    assert await ports.pays_for(taken) is not None
    assert ports.secrets is not None and list(ports.secrets.values.values()) == [taken]
    assert ports.provider.calls.count("create_key") == rotations
    sent = [r for r in ports.requests if r.method == "POST" and r.url.path.endswith("/rotate-payment-key")]
    assert len({json.loads(request.content)["operation_ref"] for request in sent}) == rotations
    assert len(ports.provider.keys) == 1 + rotations  # the allotment's key, and one for each rotation
    return taken


def a_whole_before_b_reaches_the_clearinghouse(
    ports: Ports,
    monkeypatch: pytest.MonkeyPatch,
    *,
    then: Callable[[], Awaitable[object]] | None = None,
    when_b_asks: Callable[[], Awaitable[object]] | None = None,
) -> list[ActorAllocation]:
    """Arrange the next rotation (B) so that, once it has begun and before its call reaches the
    clearinghouse, rotation A (``r-a``) runs whole: the clearinghouse runs A first, and A writes the
    engine's record first. ``then`` runs after A and before B's call. ``when_b_asks`` runs after B has
    asked the clearinghouse which key the allocation takes, and before B acts on the answer. A's answer
    is appended to the list."""
    manager = ports.manager
    original = manager.for_actor  # type: ignore[attr-defined]
    first: list[ActorAllocation] = []

    def for_b(*args: Any, **kwargs: Any) -> Any:
        monkeypatch.setattr(manager, "for_actor", original)
        mine = original(*args, **kwargs)
        create_key, holds_key = mine.create_key, getattr(mine, "holds_key", None)

        async def after_a(*call: Any) -> Any:
            first.append(await rotate(ports, "r-a"))
            if then is not None:
                await then()
            return await create_key(*call)

        async def asked(*call: Any) -> Any:
            answer = await holds_key(*call)
            if when_b_asks is not None:
                monkeypatch.setattr(mine, "holds_key", holds_key)
                await when_b_asks()
            return answer

        monkeypatch.setattr(mine, "create_key", after_a)
        if holds_key is not None:
            monkeypatch.setattr(mine, "holds_key", asked)
        return mine

    monkeypatch.setattr(manager, "for_actor", for_b)
    return first


def fail_the_call(
    monkeypatch: pytest.MonkeyPatch, target: object, name: str, number: int, *, apply: bool = False
) -> None:
    """The ``number``th call of ``target.name`` from now fails, before it applies or (``apply``) after:
    the engine's run dies there."""
    original = getattr(target, name)
    calls = [0]

    async def counted(*args: Any, **kwargs: Any) -> Any:
        calls[0] += 1
        if calls[0] != number:
            return await original(*args, **kwargs)
        monkeypatch.setattr(target, name, original)
        if apply:
            await original(*args, **kwargs)
        raise ConnectionError("the engine's run died here")

    monkeypatch.setattr(target, name, counted)


async def test_the_rotation_the_clearinghouse_ran_last_keeps_its_key_though_the_other_wrote_first(
    ports: Ports, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second review thread of #182, as Mike decided it. A at the clearinghouse first, and A writes
    first. B's key is the one the clearinghouse takes, so B stores it over A's, which is ended, drops A's
    secret, and answers as a success. No third rotation, and the actor pays at once."""
    allotted = await allot(ports)
    first = a_whole_before_b_reaches_the_clearinghouse(ports, monkeypatch)
    before = len(ports.requests)

    b = await rotate(ports, "r-b")

    [a] = first
    taken = await settled(ports, rotations=2)
    assert b.api_key_id != a.api_key_id and b.allocation_id == allotted.allocation_id
    assert await ports.engine.store.get_actor(ports.tenant_id, "alice") == b
    assert taken == handed(ports, ROTATE)[-1]["allocation_api_key"]  # the key of the later answer
    assert ports.provider.keys[a.api_key_id][2] is True  # A's key: ended by the clearinghouse itself
    done = await record_of(ports, "r-b")
    assert done is not None and done.state == "done"
    # what B asked of the clearinghouse, apart from A's own calls: a read, the rotation, and one read of
    # which key the allocation takes. Nothing after the rotation writes
    asked = [(r.method, r.url.path.rsplit("/", 1)[1]) for r in ports.requests[before:]]
    assert asked == [
        ("GET", "allocation"),  # A, whole: its read
        ("POST", "rotate-payment-key"),  # and its rotation
        ("GET", "allocation"),  # B reads the allocation
        ("POST", "rotate-payment-key"),  # B's rotation
        ("GET", "allocation"),  # B asks which key the allocation takes
    ]


async def test_the_rotation_the_clearinghouse_ran_first_is_refused_when_the_other_wrote_first(
    ports: Ports, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other order: A at the clearinghouse first, and B, which ran there last, writes first. A's key
    is ended already, so A keeps nothing, drops its secret, and is refused. Nothing live is dropped."""
    await allot(ports)
    store = ports.engine.store
    original = store.set_actor_key  # type: ignore[attr-defined]
    second: list[ActorAllocation] = []

    async def b_first(*args: Any, **kwargs: Any) -> Any:
        monkeypatch.setattr(store, "set_actor_key", original)
        second.append(await rotate(ports, "r-b"))
        return await original(*args, **kwargs)

    monkeypatch.setattr(store, "set_actor_key", b_first)
    with pytest.raises(InvalidRequest) as lost:
        await rotate(ports, "r-a")

    [b] = second
    taken = await settled(ports, rotations=2)
    assert "another key rotation" in str(lost.value) and "Rotate again under a new reference" in str(
        lost.value
    )
    assert await ports.engine.store.get_actor(ports.tenant_id, "alice") == b
    assert taken == handed(ports, ROTATE)[-1]["allocation_api_key"]
    held = await record_of(ports, "r-a")
    assert held is not None and held.state == "started"  # never freed: its key is ended


async def test_an_overtaken_rotation_that_dies_before_its_write_finishes_from_the_secret_it_noted(
    ports: Ports, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B was handed its key, stored it and noted it, and died at its write of the actor. The clearinghouse
    finished B's command and would answer the repeat without the key. The engine holds that key, so the
    resume asks for no rotation: it finishes from the secret B's record noted, and keeps the key."""
    await allot(ports)
    a_whole_before_b_reaches_the_clearinghouse(ports, monkeypatch)
    fail_the_call(monkeypatch, ports.engine.store, "set_actor_key", 2)  # A's write, then B's
    with pytest.raises(ConnectionError):
        await rotate(ports, "r-b")
    assert ports.secrets is not None and len(ports.secrets.values) == 2  # A's key, and B's, noted
    assert await ports.pays_for(await stored_key(ports)) is None  # the actor holds A's key, which is ended
    rotations = len(ports.sent())

    later(ports)
    b = await rotate(ports, "r-b")

    assert len(ports.sent()) == rotations  # no command is sent: the key is the engine's own copy
    taken = await settled(ports, rotations=2)
    assert await ports.engine.store.get_actor(ports.tenant_id, "alice") == b
    assert taken == [answer["allocation_api_key"] for answer in handed(ports, ROTATE)][-1]


async def test_an_overtaken_rotation_that_dies_after_it_kept_its_key_drops_the_ended_one_on_its_resume(
    ports: Ports, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B pointed the actor at its key and died before it dropped the secret of the key it replaced. B's
    record noted that secret before the write, so the resume drops it."""
    await allot(ports)
    a_whole_before_b_reaches_the_clearinghouse(ports, monkeypatch)
    assert ports.secrets is not None
    fail_the_call(monkeypatch, ports.secrets, "delete", 2)  # A forgets the first key; B, the key it replaced
    with pytest.raises(ConnectionError):
        await rotate(ports, "r-b")
    [taken] = live_keys(ports)
    assert await stored_key(ports) == taken  # the write was made
    assert len(ports.secrets.values) == 2  # and A's secret is still in the store
    sent = len(ports.requests)

    later(ports)
    await rotate(ports, "r-b")

    assert len(ports.requests) == sent
    assert await settled(ports, rotations=2) == taken


async def test_an_overtaken_rotation_that_dies_after_it_dropped_its_ended_key_is_refused_on_its_resume(
    ports: Ports, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A at the clearinghouse first, B writes first, and A dies just after it dropped its own secret and
    before it noted that it lost. Its resume finds no secret where its record noted one, asks the
    clearinghouse, and is refused: the key is ended, and nothing is left to drop."""
    await allot(ports)
    store = ports.engine.store
    original = store.set_actor_key  # type: ignore[attr-defined]

    async def b_first(*args: Any, **kwargs: Any) -> Any:
        monkeypatch.setattr(store, "set_actor_key", original)
        await rotate(ports, "r-b")
        assert ports.secrets is not None
        fail_the_call(monkeypatch, ports.secrets, "delete", 1, apply=True)  # A's own secret
        return await original(*args, **kwargs)

    monkeypatch.setattr(store, "set_actor_key", b_first)
    with pytest.raises(ConnectionError):
        await rotate(ports, "r-a")
    taken = await settled(ports, rotations=2)

    later(ports)
    with pytest.raises(InvalidRequest, match="again, under a new operation_ref"):
        await rotate(ports, "r-a")
    assert await settled(ports, rotations=2) == taken


async def test_an_overtaken_rotation_that_dies_before_it_stored_its_key_has_lost_it_and_says_so(
    ports: Ports, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one outcome that needs one more rotation, by D87 point 1: B was handed its key and died before
    it stored it. The clearinghouse hands a key over once, so nobody holds the key it takes. B's resume
    is refused with what replaces it, and that rotation leaves the engine holding the key that is taken."""
    await allot(ports)
    a_whole_before_b_reaches_the_clearinghouse(ports, monkeypatch)
    assert ports.secrets is not None
    fail_the_call(monkeypatch, ports.secrets, "put", 2)  # A's key, then B's
    with pytest.raises(ConnectionError):
        await rotate(ports, "r-b")
    assert await ports.pays_for(await stored_key(ports)) is None  # the actor holds A's key, which is ended
    assert len(ports.secrets.values) == 1 and len(live_keys(ports)) == 1

    later(ports)
    with pytest.raises(InvalidRequest) as lost:
        await rotate(ports, "r-b")
    assert "A lost key is replaced by a rotation" in str(lost.value)
    assert ports.provider.calls.count("create_key") == 2  # the repeat rotated nothing

    await rotate(ports, "r-c")
    await settled(ports, rotations=3)


async def test_an_overtaken_rotation_cut_short_at_its_third_call_wrote_nothing_and_finishes_on_its_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stale window (``config.py``): the ledger cancels a run at half of it. An overtaken rotation's
    third call is a read, of which key the allocation takes. Cut short there, the command has no write
    in flight, at the clearinghouse or in the engine, and its resume asks again and keeps its key."""
    quick = {"operation_stale_s": 2.0, "clearinghouse_timeout_s": 0.25, "batteries_timeout_s": 0.25}
    async with open_ports("remote", **quick) as ports:
        await allot(ports)

        async def never() -> None:
            await asyncio.Event().wait()

        a_whole_before_b_reaches_the_clearinghouse(ports, monkeypatch, when_b_asks=never)
        with pytest.raises(ProviderUnavailable, match="ran past its 1 s budget"):
            await rotate(ports, "r-b")
        sent = len(ports.sent())
        assert ports.provider.calls.count("create_key") == 2
        assert ports.secrets is not None and len(ports.secrets.values) == 2  # A's, and B's, noted

        later(ports)
        b = await rotate(ports, "r-b")

        assert len(ports.sent()) == sent  # the resume writes nothing at the clearinghouse
        await settled(ports, rotations=2)
        assert await ports.engine.store.get_actor(ports.tenant_id, "alice") == b


async def test_a_third_rotation_between_the_question_and_the_write_is_the_one_whose_key_is_kept(
    ports: Ports, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A, then B, then C at the clearinghouse. B is told its key is the one the allocation takes, and
    before B writes, C runs whole and writes its own. B writes only over the key the actor held when it
    asked, so it writes nothing, asks again, learns its key is ended, and is refused. C's key is kept."""
    await allot(ports)
    third: list[ActorAllocation] = []

    async def c_whole() -> None:
        third.append(await rotate(ports, "r-c"))

    a_whole_before_b_reaches_the_clearinghouse(ports, monkeypatch, when_b_asks=c_whole)
    with pytest.raises(InvalidRequest, match="another key rotation"):
        await rotate(ports, "r-b")

    [c] = third
    taken = await settled(ports, rotations=3)
    assert await ports.engine.store.get_actor(ports.tenant_id, "alice") == c
    assert taken == handed(ports, ROTATE)[-1]["allocation_api_key"]


async def test_a_rotation_that_wrote_meanwhile_with_an_ended_key_does_not_cost_the_live_one_its_place(
    ports: Ports, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A, then D, then B at the clearinghouse, so B's key is the one it takes. A writes the engine's
    record first. D began from A's key and is held up at its own write. B is told its key is live; then
    D's write lands, over A's key, with a key the clearinghouse has ended. B's write, over the key the
    actor held when it asked, writes nothing. B asks again, is told the same, and writes over D's."""
    await allot(ports)
    store = ports.engine.store
    original = store.set_actor_key  # type: ignore[attr-defined]
    reached, release = asyncio.Event(), asyncio.Event()
    writes = [0]
    running: list[asyncio.Task[ActorAllocation]] = []

    async def d_is_held_up(*args: Any, **kwargs: Any) -> Any:
        writes[0] += 1
        if writes[0] == 2:  # A's write is the first, D's the second
            reached.set()
            await release.wait()
        return await original(*args, **kwargs)

    async def d_reaches_the_clearinghouse() -> None:
        running.append(asyncio.create_task(rotate(ports, "r-d")))
        await reached.wait()

    async def d_writes() -> None:
        release.set()
        await running[0]

    monkeypatch.setattr(store, "set_actor_key", d_is_held_up)
    a_whole_before_b_reaches_the_clearinghouse(
        ports, monkeypatch, then=d_reaches_the_clearinghouse, when_b_asks=d_writes
    )
    b = await rotate(ports, "r-b")

    d = running[0].result()
    taken = await settled(ports, rotations=3)
    assert b.api_key_id != d.api_key_id
    assert await ports.engine.store.get_actor(ports.tenant_id, "alice") == b
    assert taken == handed(ports, ROTATE)[-1]["allocation_api_key"]
    assert ports.provider.keys[d.api_key_id][2] is True  # D's key: ended by B's rotation


async def test_a_revoke_between_the_question_and_the_write_leaves_nothing_live_and_nothing_stored(
    ports: Ports, monkeypatch: pytest.MonkeyPatch
) -> None:
    allotted = await allot(ports)

    async def revoked() -> None:
        await ports.engine.payments.revoke(ports.admin, ports.tenant_id, "alice")

    a_whole_before_b_reaches_the_clearinghouse(ports, monkeypatch, when_b_asks=revoked)
    with pytest.raises(NotFound, match="has no allocation"):
        await rotate(ports, "r-b")

    assert live_keys(ports) == set()  # the allocation is revoked: the clearinghouse takes no key
    assert ports.provider.allocations[allotted.allocation_id].status == "revoked"
    stored = await ports.engine.store.get_actor(ports.tenant_id, "alice")
    assert stored is not None and stored.status == "revoked"
    assert ports.secrets is not None and ports.secrets.values == {}
    assert ports.provider.calls.count("create_key") == 2
    with pytest.raises(NotProvisioned):
        await ports.credentials.credential(ports.actor("alice"))


async def test_a_revoke_before_the_question_leaves_nothing_live_and_nothing_stored(
    ports: Ports, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The allocation is revoked after B's rotation and before B's write: B's write is refused for a
    revoked actor, as any rotation's is, and its secret is dropped."""
    await allot(ports)
    store = ports.engine.store
    original = store.set_actor_key  # type: ignore[attr-defined]

    async def revoked_first(*args: Any, **kwargs: Any) -> Any:
        monkeypatch.setattr(store, "set_actor_key", original)
        await ports.engine.payments.revoke(ports.admin, ports.tenant_id, "alice")
        return await original(*args, **kwargs)

    monkeypatch.setattr(store, "set_actor_key", revoked_first)
    with pytest.raises(NotFound, match="has no allocation"):
        await rotate(ports, "r-b")

    assert live_keys(ports) == set()
    assert ports.secrets is not None and ports.secrets.values == {}
    assert ports.provider.calls.count("create_key") == 1


async def test_self_hosted_payments_still_revoke_the_overtaken_rotations_own_key() -> None:
    """The same two rotations over Batteries' stand-in, which keeps both keys live until one is revoked:
    the rotation that wrote first keeps its key, the other revokes its own and is refused, and the
    provider is never asked which key an allocation takes."""
    async with open_ports("local") as local:
        await allot(local)
        store = local.engine.store
        original = store.set_actor_key  # type: ignore[attr-defined]
        second: list[ActorAllocation] = []

        async def b_first(*args: Any, **kwargs: Any) -> Any:
            monkeypatch_restore()
            second.append(await rotate(local, "r-b"))
            return await original(*args, **kwargs)

        def monkeypatch_restore() -> None:
            store.set_actor_key = original  # type: ignore[attr-defined]

        store.set_actor_key = b_first  # type: ignore[attr-defined]
        with pytest.raises(InvalidRequest, match="another key rotation"):
            await rotate(local, "r-a")

        [b] = second
        assert not hasattr(local.manager, "holds_key")
        assert local.provider.calls.count("revoke_key") == 3  # the first key twice, and A's own
        assert [key for key, (_, _, revoked) in local.provider.keys.items() if not revoked] == [b.api_key_id]
        assert await local.engine.store.get_actor(local.tenant_id, "alice") == b


# -- how many calls a command makes ------------------------------------------------------------------------


async def test_no_command_writes_after_its_second_call_and_only_an_overtaken_rotation_makes_a_third(
    ports: Ports,
) -> None:
    """What ``LPB_OPERATION_STALE_S`` is held to (``config.py``): the ledger cancels a run at half the
    stale window, and the longest command makes three calls to the clearinghouse, each within
    ``LPB_CLEARINGHOUSE_TIMEOUT_S``. The three are an overtaken rotation's (the test above counts them);
    every other command makes one or two, and the last write is never later than the second."""
    payments = ports.engine.payments

    async def calls_of(command: Callable[[], Awaitable[object]]) -> list[str]:
        before = len(ports.requests)
        await command()
        return [request.method for request in ports.requests[before:]]

    assert await calls_of(lambda: allot(ports)) == ["POST"]
    top_up = TopUpRequest(amount=usd(1), operation_ref="t-1")
    assert await calls_of(lambda: payments.top_up(ports.admin, ports.tenant_id, "alice", top_up)) == [
        "GET",
        "POST",
    ]
    assert await calls_of(lambda: payments.set_status(ports.admin, ports.tenant_id, "alice", "paused")) == [
        "GET",
        "POST",
    ]
    assert await calls_of(lambda: rotate(ports, "r-1")) == ["GET", "POST"]
    assert await calls_of(lambda: payments.revoke(ports.admin, ports.tenant_id, "alice")) == ["GET", "POST"]


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


# -- a clearinghouse that names an allocation or a key by an id the engine cannot keep (#158) --------------

UNKEPT = {"201 characters": "x" * 201, "a NUL": "a\x00b"}


def names_otherwise(
    ports: Ports, monkeypatch: pytest.MonkeyPatch, ending: str, method: str = "POST", **said: str
) -> list[bool]:
    """A clearinghouse whose 2xx answers to the requests whose path ends ``ending`` say ``said`` in place
    of what it holds. No engine of this repository does: its own clearinghouse refuses such an id from
    its provider first. Clear the list that is returned, and it answers as it holds again."""
    assert ports.transport is not None
    carried, saying = ports.transport.handle_async_request, [True]

    async def otherwise(request: httpx.Request) -> httpx.Response:
        response = await carried(request)
        ours = request.method == method and request.url.path.endswith(ending) and response.is_success
        if not (saying and ours):
            return response
        body = json.loads(await response.aread()) | said
        return httpx.Response(response.status_code, content=json.dumps(body).encode(), request=request)

    monkeypatch.setattr(ports.transport, "handle_async_request", otherwise)
    return saying


def nowhere(unkept: str, *texts: str) -> bool:
    """Whether ``unkept`` is in none of ``texts``, as itself or as ``repr`` or JSON writes it."""
    forms = {unkept, repr(unkept)[1:-1], json.dumps(unkept)[1:-1]}
    return not any(form in text for form in forms for text in texts)


def cannot_use(refused: pytest.ExceptionInfo[ProviderError], unkept: str, *way_out: str) -> str:
    """The error of an answer the engine cannot use: no refusal, so the reference stays held (HTTP 502),
    the id not quoted, and the way out said."""
    message = str(refused.value)
    assert refused.value.status_code == 200 and not definite_refusal(refused.value)
    assert "this engine cannot store" in message and nowhere(unkept, message)
    assert all(words in message for words in way_out), message
    return message


@pytest.mark.parametrize("field", ["allocation_id", "api_key_id"])
@pytest.mark.parametrize("why", UNKEPT)
async def test_an_allotment_handed_a_key_under_an_id_the_engine_cannot_keep_is_answered_the_same_on_its_resume(
    ports: Ports, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, field: str, why: str
) -> None:
    """The allotment's answer names the allocation or its key by an id the engine's 200-character columns
    cannot keep. The engine takes no such key: nothing reaches its secret store or its record of the
    actor, and the allotment happened at the clearinghouse, so this is no refusal and frees no reference.
    The clearinghouse answers the resume with the same ids and no key (``replayed``). That is the same
    answer the engine cannot use, in the same words: never the lost-answer refusal (a 400), which quoted
    the allocation's id (review of #201)."""
    caplog.set_level(logging.DEBUG)
    unkept = UNKEPT[why]
    names_otherwise(ports, monkeypatch, ALLOCATION, **{field: unkept})
    way_out = ("Revoke the allocation at the clearinghouse", "allot again here under a new operation_ref")
    with pytest.raises(ProviderError) as first:
        await allot(ports)
    said = cannot_use(first, unkept, *way_out)
    with pytest.raises(OperationInProgress):
        await allot(ports)

    later(ports)
    with pytest.raises(ProviderError) as resumed:
        await allot(ports)

    again = handed(ports, ALLOCATION)[-1]  # as the clearinghouse wrote it: its finished command, no key
    assert again["allocation_api_key"] is None and again["replayed"] is True
    assert cannot_use(resumed, unkept, *way_out) == said
    assert ports.secrets is not None and ports.secrets.values == {}
    assert await ports.engine.store.get_actor(ports.tenant_id, "alice") is None
    held = await record_of(ports, "allot-alice")
    assert held is not None and held.state == "started"
    assert not [name for name in held.context if name.startswith("new_secret")]  # nothing noted
    stored = await everything_stored(cast(SqlStore, ports.engine.store))
    assert nowhere(unkept, stored, caplog.text, *(str(record.args) for record in caplog.records))
    assert len(ports.provider.allocations) == 1  # allotted there once, and its key is live there


@pytest.mark.parametrize("field", ["allocation_id", "api_key_id"])
async def test_the_way_out_of_such_an_allotment_is_a_revoke_at_the_clearinghouse_and_a_new_reference(
    ports: Ports, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    """What the error says to do, done: the key was handed over once and not kept, so no repeat of the
    reference can finish. The tenant's admin revokes the allocation at the clearinghouse, whose route
    names the actor and no allocation id, and the engine allots again under a new reference. The first
    reference's record then finishes as a lost allotment's does: its repeat is told the actor is allotted."""
    saying = names_otherwise(ports, monkeypatch, ALLOCATION, **{field: UNKEPT["201 characters"]})
    with pytest.raises(ProviderError):
        await allot(ports)
    assert ports.wire is not None and ports.provider.unallocated(ports.tenant_id) == 95
    revoke = f"http://clearinghouse.test/v1/clearinghouse{ALLOCATION}/revoke"  # the tenant's own route
    answer = await ports.wire._client.post(revoke, headers={"Authorization": f"Bearer {ports.token}"})
    assert answer.status_code == 200 and answer.json()["status"] == "revoked"
    assert ports.provider.unallocated(ports.tenant_id) == 100 and live_keys(ports) == set()

    saying.clear()  # the next allocation has ids the engine keeps
    anew = await allot(ports, ref="allot-alice-again", amount="4")

    key = await stored_key(ports)
    assert await ports.pays_for(key) == anew.allocation_id and live_keys(ports) == {key}
    later(ports)
    with pytest.raises(AlreadyAllotted):
        await allot(ports)
    assert await record_of(ports, "allot-alice") is None  # the first reference is free
    assert len(ports.provider.allocations) == 2 and ports.provider.unallocated(ports.tenant_id) == 96


@pytest.mark.parametrize("why", UNKEPT)
async def test_a_rotation_handed_a_key_under_an_id_the_engine_cannot_keep_is_answered_the_same_on_its_resume(
    ports: Ports, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, why: str
) -> None:
    """The same for a rotation. The clearinghouse rotated: the key the engine holds is ended, and the new
    one was handed over once, under an id the engine cannot keep. The way out is the lost rotation's:
    another rotation, under a new reference, which the engine can begin from its own record."""
    caplog.set_level(logging.DEBUG)
    unkept = UNKEPT[why]
    allotted = await allot(ports)
    old = await stored_key(ports)
    saying = names_otherwise(ports, monkeypatch, ROTATE, api_key_id=unkept)
    way_out = ("rotate the payment key of actor 'alice' again", "under a new operation_ref")
    with pytest.raises(ProviderError) as first:
        await rotate(ports, "r-1")
    said = cannot_use(first, unkept, *way_out)
    assert await ports.pays_for(old) is None and len(live_keys(ports)) == 1  # rotated there

    later(ports)
    with pytest.raises(ProviderError) as resumed:
        await rotate(ports, "r-1")

    assert handed(ports, ROTATE)[-1]["replayed"] is True
    assert cannot_use(resumed, unkept, *way_out) == said
    assert await stored_key(ports) == old  # the engine keeps what it has, and one secret
    assert ports.secrets is not None and list(ports.secrets.values.values()) == [old]
    held = await record_of(ports, "r-1")
    assert held is not None and held.state == "started"
    stored = await everything_stored(cast(SqlStore, ports.engine.store))
    assert nowhere(unkept, stored, caplog.text, *(str(record.args) for record in caplog.records))

    saying.clear()  # the next key has an id the engine keeps
    rotated = await rotate(ports, "r-2")

    new = await stored_key(ports)
    assert rotated.allocation_id == allotted.allocation_id
    assert await ports.pays_for(new) == allotted.allocation_id and live_keys(ports) == {new}
    assert list(ports.secrets.values.values()) == [new]
    # the first rotation's record never finishes, as a lost rotation's: its repeat changes nothing
    later(ports)
    with pytest.raises(InvalidRequest, match="never reached this engine"):
        await rotate(ports, "r-1")
    held = await record_of(ports, "r-1")
    assert held is not None and held.state == "started" and await stored_key(ports) == new


@pytest.mark.parametrize("why", UNKEPT)
@pytest.mark.parametrize(
    ("command", "ending"), [("rotate", ROTATE), ("top_up", "/top-up"), ("status", "/status")]
)
async def test_no_error_quotes_another_allocation_s_id_that_the_engine_cannot_keep(
    ports: Ports, monkeypatch: pytest.MonkeyPatch, command: str, ending: str, why: str
) -> None:
    """Each route answers the allocation it acted on, and one that is not the allocation the command
    named is an answer the engine cannot use, which says which allocation that was. An id the engine
    cannot keep is not said: it may be of any length and hold anything."""
    unkept = UNKEPT[why]
    await allot(ports)
    names_otherwise(ports, monkeypatch, ending, allocation_id=unkept)
    commands = {
        "rotate": lambda: rotate(ports, "r-1"),
        "top_up": lambda: ports.engine.payments.top_up(
            ports.admin, ports.tenant_id, "alice", TopUpRequest(amount=usd(1), operation_ref="t-1")
        ),
        "status": lambda: ports.engine.payments.set_status(ports.admin, ports.tenant_id, "alice", "paused"),
    }
    with pytest.raises(ProviderError) as elsewhere:
        await commands[command]()
    message = str(elsewhere.value)
    assert not definite_refusal(elsewhere.value) and nowhere(unkept, message)
    assert "an id this engine cannot store" in message and "which actor 'alice' holds there now" in message


async def test_the_port_s_revoke_of_a_key_does_not_quote_an_allocation_id_the_engine_cannot_keep(
    ports: Ports, monkeypatch: pytest.MonkeyPatch
) -> None:
    allotted = await allot(ports)
    unkept = UNKEPT["201 characters"]
    names_otherwise(ports, monkeypatch, ALLOCATION, "GET", allocation_id=unkept)
    manager = ports.engine.payments._manager
    assert isinstance(manager, ClearinghouseAllocations)
    with pytest.raises(InvalidRequest) as refused:
        await manager.for_actor(ports.tenant_id, "alice").revoke_key(allotted.api_key_id)
    assert nowhere(unkept, str(refused.value)) and "an id this engine cannot store" in str(refused.value)


async def test_an_answer_whose_id_is_no_text_is_no_answer_on_the_first_run_and_on_the_resume(
    ports: Ports, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lone surrogate is no Unicode text, and the wire reads no such body as an allocation: the same
    error both times, which quotes nothing of the body."""
    names_otherwise(ports, monkeypatch, ALLOCATION, allocation_id="a\ud800b")
    with pytest.raises(ProviderError) as first:
        await allot(ports)
    later(ports)
    with pytest.raises(ProviderError) as resumed:
        await allot(ports)
    assert str(first.value) == str(resumed.value) and "not DeliveredAllocation" in str(first.value)
    assert not definite_refusal(resumed.value) and nowhere("a\ud800b", str(resumed.value))
    held = await record_of(ports, "allot-alice")
    assert held is not None and held.state == "started"
    assert await ports.engine.store.get_actor(ports.tenant_id, "alice") is None


async def test_a_clearinghouse_of_this_repository_hands_over_no_key_under_such_an_id(
    ports: Ports, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Why only another clearinghouse answers one: an engine in its ``clearinghouse`` role refuses such
    an id from its own provider before it stores or hands over anything, so the enterprise's engine is
    answered an error, and no key was handed over to be lost."""
    minted = ports.provider.create_key_for_grant

    async def under_a_long_id(*args: Any, **kwargs: Any) -> Any:
        return (await minted(*args, **kwargs)).model_copy(update={"allocation_id": UNKEPT["201 characters"]})

    monkeypatch.setattr(ports.provider, "create_key_for_grant", under_a_long_id)
    with pytest.raises(ProviderOutcomeUnknown):  # the clearinghouse's 502: applied or not, it cannot say
        await allot(ports)
    assert handed(ports, ALLOCATION) == []  # no 2xx, so no key in any answer
    assert ports.secrets is not None and ports.clearinghouse_secrets is not None
    assert ports.secrets.values == {} and ports.clearinghouse_secrets.values == {}
    assert await house(ports).store.get_actor(ports.tenant_id, "alice") is None
    assert await ports.engine.store.get_actor(ports.tenant_id, "alice") is None


async def test_an_engine_that_keeps_the_keys_itself_is_told_so_whatever_ids_it_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No key and no ``replayed``: none was handed over, now or before, so none was lost. The answer
    is the one for an engine in the wrong role, and it quotes no id either."""
    from livepeer_builder.http import create_app

    async with open_ports(
        "remote", lambda engine: create_app(engine, manage_lifecycle=False, listener="public"), BOTH_ROLE
    ) as ports:
        names_otherwise(ports, monkeypatch, ALLOCATION, allocation_id=UNKEPT["201 characters"])
        with pytest.raises(ProviderError) as unusable:
            await allot(ports)
        assert "keeps each allocation's key itself" in unusable.value.message
        assert nowhere(UNKEPT["201 characters"], str(unusable.value))


async def test_a_handed_key_the_engine_s_own_store_names_by_a_reference_no_database_keeps_is_lost(
    ports: Ports, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#158, at an engine that uses a clearinghouse: its own secret store returns a reference with a NUL
    for the key it was handed. The engine deletes that copy and fails the command with nothing noted, as
    with any provider. A clearinghouse hands a key over once, so the repeat cannot finish as it does
    with Batteries, which replays the key: it is answered as a key that never arrived, with the same
    way out."""
    assert ports.secrets is not None
    stores, values = ports.secrets.put, ports.secrets.values

    async def under_a_nul(name: str, secret: str) -> SecretRef:
        values["vault:a\x00b"] = values.pop((await stores(name, secret)).ref)
        return SecretRef(ref="vault:a\x00b")

    monkeypatch.setattr(ports.secrets, "put", under_a_nul)
    with pytest.raises(ValueError, match="the secret store returned a reference"):
        await allot(ports)
    assert values == {} and await ports.engine.store.get_actor(ports.tenant_id, "alice") is None
    held = await record_of(ports, "allot-alice")
    assert held is not None and held.state == "started"
    assert not [name for name in held.context if name.startswith("new_secret")]

    monkeypatch.setattr(ports.secrets, "put", stores)  # the store is put right
    later(ports)
    with pytest.raises(InvalidRequest, match="Revoke the allocation at the clearinghouse"):
        await allot(ports)
    assert handed(ports, ALLOCATION)[-1]["replayed"] is True and values == {}
