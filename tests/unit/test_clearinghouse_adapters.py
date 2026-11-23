"""#168: what only the remote adapters do. The contract both sets of adapters share is in
``test_payment_port_suite.py``; this file is what is true of an enterprise's engine and no other.

- A command's reference at the clearinghouse, which keys its own ledger on it: a repeat here is a repeat
  there, and a new reference here is a new command there.
- The clearinghouse's routes name an actor and no allocation id, so the allocation is checked before a
  command is sent.
- What only a clearinghouse admin does is refused in the engine, and nothing is sent.
- A tenant admin key reaches its own tenant and no other.

The key an allotment or a rotation hands over, and what the engine does without one, is
``test_clearinghouse_handed_keys.py``.
"""

import base64
import hashlib
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast

import httpx
import pytest

from livepeer_builder.adapters.clearinghouse import ClearinghouseAllocations, ClearinghousePorts
from livepeer_builder.adapters.clearinghouse_wire import ClearinghouseWire
from livepeer_builder.contracts import ActorAllocation, AllotRequest, TopUpRequest, UsageRow
from livepeer_builder.errors import AccessDenied, InvalidRequest, ProviderError, ProviderOutcomeUnknown
from livepeer_builder.protocols import ActorRepo, AllocationManager, AllocationsByActor
from livepeer_builder.services.grants import idempotency_key
from livepeer_builder.services.ledger import definite_refusal
from livepeer_builder.testing import FixedClock, MemorySecretStore
from tests.unit.payment_ports import OPERATOR, Ports, open_ports, usd

ALLOCATION = "/actors/alice/allocation"


@pytest.fixture
async def ports() -> AsyncIterator[Ports]:
    async with open_ports("remote") as opened:
        yield opened


def allot_request(
    ports: Ports, actor_id: str = "alice", amount: str = "5", ref: str | None = None, label: str = ""
) -> AllotRequest:
    return AllotRequest(
        tenant_id=ports.tenant_id,
        actor_id=actor_id,
        amount=usd(amount),
        label=label,
        operation_ref=ref or f"allot-{actor_id}",
    )


def house(ports: Ports) -> Any:
    assert ports.clearinghouse is not None
    return ports.clearinghouse


def later(ports: Ports, seconds: float = 121) -> None:
    """Past ``LPB_OPERATION_STALE_S``: a repeat may resume a command whose outcome is unknown."""
    cast(FixedClock, ports.engine.clock).advance(seconds)


def bodies(ports: Ports, ending: str) -> list[dict[str, Any]]:
    return [
        json.loads(request.content)
        for request in ports.requests
        if request.method == "POST" and request.url.path.endswith(ending)
    ]


# -- the reference a command carries to the clearinghouse ---------------------------------------------------


def folded(*parts: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(":".join(parts).encode()).digest()).decode().rstrip("=")


async def test_an_allotment_reference_names_the_engine_the_actor_and_the_admins_reference(
    ports: Ports,
) -> None:
    """The clearinghouse keys its ledger on the ``operation_ref`` it is sent, for the tenant admin key that
    sent it. The engine's own idempotency key for an allotment names the engine and the actor and no
    reference, so the admin's reference is folded into it."""
    await ports.engine.payments.allot(ports.admin, allot_request(ports))
    [allot] = bodies(ports, ALLOCATION)
    engine_key = idempotency_key(
        ports.engine.settings.engine_id, f"provision:{ports.tenant_id}:alice", "api-key"
    )
    sent_as = folded(engine_key, "allot-alice")
    assert allot == {"amount": {"amount": "5", "currency": "USD"}, "label": "", "operation_ref": sent_as}
    assert len(sent_as) == 43
    # the clearinghouse's own ledger holds the command under that reference, for the tenant's admin
    held = await house(ports).store.get_operation(ports.tenant_id, "tenant-admin", sent_as)
    assert held is not None and (held.command, held.state) == ("allot", "done")
    # a repeat here is a repeat there: one command, and nothing more is sent for it
    await ports.engine.payments.allot(ports.admin, allot_request(ports))
    assert len(bodies(ports, ALLOCATION)) == 1


async def test_a_top_up_and_a_rotation_carry_the_engine_key_which_names_the_admins_reference(
    ports: Ports,
) -> None:
    allotted = await ports.engine.payments.allot(ports.admin, allot_request(ports))
    engine_id = ports.engine.settings.engine_id

    top_up = TopUpRequest(amount=usd(2), operation_ref="t-1")
    await ports.engine.payments.top_up(ports.admin, ports.tenant_id, "alice", top_up)
    [sent] = bodies(ports, ALLOCATION + "/top-up")
    assert sent["operation_ref"] == idempotency_key(engine_id, f"fund:{allotted.allocation_id}:t-1", "fund")

    await ports.engine.payments.rotate_payment_key(ports.admin, ports.tenant_id, "alice", "r-1")
    [rotated] = bodies(ports, ALLOCATION + "/rotate-payment-key")
    assert rotated == {
        "operation_ref": idempotency_key(engine_id, f"rotate:{allotted.allocation_id}:r-1", "api-key")
    }


async def test_a_manager_asked_for_no_reference_sends_the_engine_key_as_it_is() -> None:
    """``for_actor`` without a reference, as a caller of the port alone makes it."""
    manager, seen = scripted({"/allocation": httpx.Response(201, json=HANDED)})
    await manager.create_key_for_grant("grant_1", "grant_1/alice", usd(5), "the-engine-key")
    assert json.loads(seen[0].content)["operation_ref"] == "the-engine-key"


async def test_a_label_is_sent_and_an_actor_with_none_is_named_by_the_clearinghouse(ports: Ports) -> None:
    await ports.engine.payments.allot(ports.admin, allot_request(ports, "bob", label="Bob B."))
    [sent] = bodies(ports, "/actors/bob/allocation")
    assert sent["label"] == "Bob B."
    there = await house(ports).store.get_actor(ports.tenant_id, "bob")
    assert there is not None and there.label == "Bob B."


# -- rows from before the engine used a clearinghouse ------------------------------------------------------


async def test_an_actor_allotted_before_the_engine_used_a_clearinghouse_is_unknown_there(
    ports: Ports,
) -> None:
    """Rows a deployment already has: the actor was allotted while the engine ran its own Batteries. The
    clearinghouse holds no allocation for it, so every command about it is refused before it is sent, and
    nothing is rewritten (T30). Its jobs still present the key the engine stored then, to the signer the
    engine now names: the clearinghouse's, which refuses a key its Batteries never minted."""
    secrets = cast(MemorySecretStore, ports.engine.payments._secrets)
    ref = await secrets.put("lpg/old/alice", "lpg_a-batteries-key")
    now = datetime(2026, 11, 1, tzinfo=UTC)
    before = ActorAllocation(
        tenant_id=ports.tenant_id,
        actor_id="alice",
        grant_id=ports.tenant_id,
        allocation_id="alloc_from_before",
        api_key_id="key_from_before",
        secret_ref=ref,
        currency="USD",
        status="active",
        created_at=now,
        updated_at=now,
    )
    await ports.engine.store.save_actor(before)

    with pytest.raises(ProviderError) as unknown:  # the clearinghouse holds no such allocation
        await ports.engine.payments.top_up(
            ports.admin, ports.tenant_id, "alice", TopUpRequest(amount=usd(1), operation_ref="t")
        )
    assert unknown.value.status_code == 404 and ports.sent() == []
    with pytest.raises(ProviderError) as rotation:
        await ports.engine.payments.rotate_payment_key(ports.admin, ports.tenant_id, "alice", "r")
    assert rotation.value.status_code == 404 and ports.sent() == []
    crossed = b"".join(request.content + str(request.headers).encode() for request in ports.requests)
    assert b"lpg_a-batteries-key" not in crossed  # no key goes to the clearinghouse's engine, ever
    assert await ports.engine.store.get_actor(ports.tenant_id, "alice") == before  # nothing is rewritten


# -- an answer that never arrives ---------------------------------------------------------------------------


async def test_a_lost_top_up_answer_is_applied_once(ports: Ports) -> None:
    allotted = await ports.engine.payments.allot(ports.admin, allot_request(ports))
    top_up = TopUpRequest(amount=usd(3), operation_ref="t-1")
    ports.lose_the_answer_to("/top-up")

    with pytest.raises(ProviderOutcomeUnknown):
        await ports.engine.payments.top_up(ports.admin, ports.tenant_id, "alice", top_up)
    later(ports)
    await ports.engine.payments.top_up(ports.admin, ports.tenant_id, "alice", top_up)

    assert ports.provider.allocations[allotted.allocation_id].granted == Decimal(8)


# -- the clearinghouse's own records come first -------------------------------------------------------------


async def test_an_actor_already_allotted_at_the_clearinghouse_is_refused_and_its_amount_not_added(
    ports: Ports,
) -> None:
    """Allotted there directly, under another reference. Nothing is applied, as for ``AlreadyAllotted``."""
    there = await house(ports).payments.allot(OPERATOR, allot_request(ports, ref="at-the-clearinghouse"))

    with pytest.raises(ProviderError) as refused:
        await ports.engine.payments.allot(ports.admin, allot_request(ports, amount="7"))

    assert refused.value.status_code == 409 and definite_refusal(refused.value)
    assert ports.provider.allocations[there.allocation_id].granted == Decimal(5)
    assert await ports.engine.store.get_actor(ports.tenant_id, "alice") is None


async def test_a_command_about_an_allocation_the_actor_no_longer_holds_is_refused_before_it_is_sent(
    ports: Ports,
) -> None:
    """The routes act on whichever allocation the actor holds at the clearinghouse. Revoked and allotted
    again there, behind the engine's back, the actor holds another: a top-up must not land on it."""
    mine = await ports.engine.payments.allot(ports.admin, allot_request(ports))
    await house(ports).payments.revoke(OPERATOR, ports.tenant_id, "alice")
    theirs = await house(ports).payments.allot(OPERATOR, allot_request(ports, ref="again", amount="9"))
    assert theirs.allocation_id != mine.allocation_id
    sent = len(ports.sent())
    payments = ports.engine.payments

    with pytest.raises(ProviderError) as top_up:
        await payments.top_up(
            ports.admin, ports.tenant_id, "alice", TopUpRequest(amount=usd(1), operation_ref="t")
        )
    with pytest.raises(ProviderError) as status:
        await payments.set_status(ports.admin, ports.tenant_id, "alice", "paused")
    with pytest.raises(ProviderError) as rotation:
        await payments.rotate_payment_key(ports.admin, ports.tenant_id, "alice", "r")
    with pytest.raises(ProviderError) as allowance:
        await ports.allowances.read_allowance(mine.allocation_id)

    assert [error.value.status_code for error in (top_up, status, rotation, allowance)] == [404] * 4
    assert len(ports.sent()) == sent  # reads only
    held = ports.provider.allocations[theirs.allocation_id]
    assert (held.granted, held.status) == (Decimal(9), "active")

    # the engine's own allocation has ended there: revoking it is done, and the other one is untouched
    revoked = await payments.revoke(ports.admin, ports.tenant_id, "alice")
    assert revoked.status == "revoked" and len(ports.sent()) == sent
    assert ports.provider.allocations[theirs.allocation_id].status == "active"


async def test_an_allocation_no_actor_holds_now_cannot_be_read(ports: Ports) -> None:
    """The clearinghouse reads an actor's current allocation. An earlier one has no route."""
    await ports.engine.payments.allot(ports.admin, allot_request(ports))
    sent = len(ports.requests)
    with pytest.raises(ProviderError) as unknown:
        await ports.allowances.read_allowance("alloc_earlier")
    assert unknown.value.status_code == 404 and len(ports.requests) == sent


# -- what only a clearinghouse admin does -------------------------------------------------------------------


async def test_what_only_a_clearinghouse_admin_does_is_refused_plainly_and_nothing_is_sent(
    ports: Ports,
) -> None:
    manager = ports.manager
    refusals = [
        manager.top_up_grant(ports.tenant_id, usd(5), "k"),
        manager.set_grant_status(ports.tenant_id, "paused"),
        manager.create_grant("another", usd(5)),
        manager.list_grants(),
        manager.grant_page(None),
        manager.grant_balances(),
    ]
    for refusal in refusals:
        with pytest.raises(AccessDenied) as refused:
            await refusal
        assert "only a clearinghouse admin" in str(refused.value) and definite_refusal(refused.value)
    assert ports.requests == []

    # through the engine: a grant top-up is refused, and its reference is free again
    top_up = TopUpRequest(amount=usd(5), operation_ref="more")
    for _ in range(2):
        with pytest.raises(AccessDenied):
            await ports.engine.payments.top_up_grant(ports.admin, ports.tenant_id, top_up)
    desk = ports.engine.payments.tenants
    assert desk is not None
    with pytest.raises(AccessDenied):
        await desk.onboard(ports.admin, "new", usd(5), operation_ref="onboard")
    with pytest.raises(AccessDenied):
        await desk.list_tenants(ports.admin)
    assert ports.requests == [] and ports.provider.unallocated(ports.tenant_id) == Decimal(100)
    assert (await desk.grant(ports.admin, ports.tenant_id)).total == usd(100)  # its own grant is read


async def test_a_member_that_names_an_allocation_needs_its_actor(ports: Ports) -> None:
    manager = ports.manager
    assert isinstance(manager, AllocationsByActor)
    needing = [
        manager.create_key_for_grant(ports.tenant_id, "n", usd(1), "k"),
        manager.create_key("alloc_1", "n", "k"),
        manager.top_up_allocation("alloc_1", usd(1), "k"),
        manager.set_allocation_status("alloc_1", "paused"),
        manager.revoke_allocation("alloc_1"),
        manager.read_allocation("alloc_1"),
    ]
    for call in needing:
        with pytest.raises(InvalidRequest, match="for_actor"):
            await call
    assert ports.requests == []


# -- one tenant, and no other -------------------------------------------------------------------------------


async def test_the_tenant_admin_key_reaches_no_other_tenant(ports: Ports) -> None:
    other = ports.other_tenant
    with pytest.raises(ProviderError) as grant:
        await ports.manager.read_grant(other)
    theirs = cast(AllocationsByActor, ports.manager).for_actor(other, "mallory")
    with pytest.raises(ProviderError) as allot:
        await theirs.create_key_for_grant(other, "mallory", usd(1), "k")
    with pytest.raises(ProviderError) as allowance:
        await theirs.read_allocation("alloc_1")

    assert [grant.value.status_code, allot.value.status_code] == [403, 403]
    assert "another tenant" in grant.value.message and definite_refusal(allot.value)
    assert allowance.value.status_code == 403  # the allocation route names the tenant, as every other does
    assert ports.provider.unallocated(other) == Decimal(100) and not ports.provider.allocations


async def test_the_usage_is_the_tenant_own_and_none_of_another_tenant(ports: Ports) -> None:
    mine = await ports.engine.payments.allot(ports.admin, allot_request(ports))
    theirs = await house(ports).payments.allot(
        OPERATOR,
        AllotRequest(tenant_id=ports.other_tenant, actor_id="bob", amount=usd(5), operation_ref="theirs"),
    )

    def row(number: int, allocation_id: str) -> UsageRow:
        return UsageRow(
            id=f"row-{number}",
            event_id=None,
            status="applied",
            manifest_id=f"m-{number}",
            allocation_id=allocation_id,
            payment_session_id=None,
            request_id=None,
            pipeline=None,
            computed_fee_eth=None,
            computed_fee_usd=Decimal("0.5"),
            currency="usd",
            created_at=datetime(2026, 11, 19, 12, number, tzinfo=UTC),
        )

    ports.provider_usage.rows = [
        row(n, mine.allocation_id if n % 2 else theirs.allocation_id) for n in range(1, 8)
    ]
    ports.provider_usage.grant_of = {
        mine.allocation_id: ports.tenant_id,
        theirs.allocation_id: ports.other_tenant,
    }

    read: list[UsageRow] = []
    cursor: str | None = None
    while True:
        page = await ports.usage.page(cursor)
        read += page.items
        cursor = page.next_cursor
        if not cursor:
            break

    assert [item.id for item in read] == ["row-1", "row-3", "row-5", "row-7"]
    assert {item.allocation_id for item in read} == {mine.allocation_id}


async def test_a_clearinghouse_admin_key_is_refused_on_the_public_listener(ports: Ports) -> None:
    """The key an enterprise's engine holds is a tenant admin's. A clearinghouse admin's does nothing on
    the listener an enterprise reaches (#167)."""
    keys = house(ports).keys
    admin_key = (await keys.bootstrap_admin()).token
    client = cast(httpx.AsyncClient, ports.manager._wire._client)  # type: ignore[attr-defined]
    wire = ClearinghouseWire("http://clearinghouse.test", admin_key, ports.tenant_id, client)
    with pytest.raises(ProviderError) as refused:
        await ClearinghouseAllocations(wire, ports.engine.store, MemorySecretStore()).read_grant(
            ports.tenant_id
        )
    assert refused.value.status_code == 403 and "public listener" in refused.value.message


# -- answers the engine cannot use --------------------------------------------------------------------------


def scripted(
    answers: dict[str, httpx.Response], actors: ActorRepo | None = None
) -> tuple[AllocationManager, list[httpx.Request]]:
    """The manager for alice over a clearinghouse that answers each path ending as the test says."""
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        for ending, answer in answers.items():
            if request.url.path.endswith(ending):
                return answer
        return httpx.Response(404, json={"error": "no such route"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    wire = ClearinghouseWire("http://clearinghouse.test", "lpk_made-up", "grant_1", client)
    whole = ClearinghouseAllocations(wire, cast(ActorRepo, actors or NoActors()), MemorySecretStore())
    return whole.for_actor("grant_1", "alice"), seen


class NoActors:
    """An engine that has no record of any actor."""

    async def get_actor(
        self, tenant_id: str, actor_id: str
    ) -> ActorAllocation | None:  # NOSONAR - async by protocol
        del tenant_id, actor_id
        return None


ALLOTTED = {
    "tenant_id": "grant_1",
    "actor_id": "alice",
    "grant_id": "grant_1",
    "allocation_id": "alloc_1",
    "api_key_id": "key_1",
    "secret_ref": {"ref": "delivered:x"},
    "currency": "USD",
    "status": "active",
    "created_at": "2026-11-19T12:00:00Z",
    "updated_at": "2026-11-19T12:00:00Z",
    "label": "",
    "credential_custody": "work_engine",
}
KEY = "lpg_a-throwaway-key"
HANDED = ALLOTTED | {"allocation_api_key": KEY, "replayed": False, "resumed": False}


async def test_the_handed_key_is_the_port_new_key() -> None:
    manager, _ = scripted({"/allocation": httpx.Response(201, json=HANDED)})
    key = await manager.create_key_for_grant("grant_1", "grant_1/alice", usd(5), "k")
    assert (key.allocation_id, key.api_key_id) == ("alloc_1", "key_1")
    assert key.secret.get_secret_value() == KEY and KEY not in repr(key)


async def test_a_clearinghouse_that_answers_the_allocation_alone_is_no_refusal() -> None:
    """The answer of routes mounted to keep each key with the clearinghouse's own engine: no key, and
    nothing that says a repeat. The allocation exists there."""
    manager, _ = scripted(
        {"/allocation": httpx.Response(201, json=ALLOTTED | {"credential_custody": "secret_store"})}
    )
    with pytest.raises(ProviderError) as unusable:
        await manager.create_key_for_grant("grant_1", "grant_1/alice", usd(5), "k")
    assert unusable.value.status_code == 200 and not definite_refusal(unusable.value)
    assert "keeps each allocation's key itself" in unusable.value.message


async def test_an_answer_that_says_it_is_a_repeat_is_never_a_key_even_when_it_carries_one() -> None:
    """``replayed`` is checked before anything is stored (payments.md, "Payment keys"). An answer that
    says both is one the clearinghouse never sends, and the engine keeps nothing of it."""
    manager, _ = scripted({"/allocation": httpx.Response(201, json=HANDED | {"replayed": True})})
    with pytest.raises(InvalidRequest) as refused:
        await manager.create_key_for_grant("grant_1", "grant_1/alice", usd(5), "k")
    assert KEY not in str(refused.value) and "never reached this engine" in str(refused.value)


async def test_a_rotation_answered_for_another_allocation_is_refused_and_its_key_not_kept() -> None:
    """Revoked and allotted again at the clearinghouse between the adapter's read and its command: the
    key is another allocation's, and the engine's record of this one is never pointed at it."""
    other = HANDED | {"allocation_id": "alloc_2", "api_key_id": "key_9"}
    manager, seen = scripted(
        {
            "/rotate-payment-key": httpx.Response(200, json=other),
            "/allocation": httpx.Response(200, json=ALLOTTED),
        }
    )
    with pytest.raises(ProviderError) as refused:
        await manager.create_key("alloc_1", "n", "k")
    assert refused.value.status_code == 404 and "another allocation" in refused.value.message
    assert KEY not in str(refused.value) and len(seen) == 2


async def test_an_allotment_from_another_grant_than_the_tenant_is_refused_before_it_is_sent() -> None:
    manager, seen = scripted({})
    with pytest.raises(InvalidRequest):
        await manager.create_key_for_grant("grant_2", "n", usd(5), "k")
    assert seen == []


async def test_the_key_of_an_answer_the_engine_cannot_read_is_not_quoted() -> None:
    broken = HANDED | {"status": "no-such-status"}
    manager, _ = scripted({"/allocation": httpx.Response(201, json=broken)})
    with pytest.raises(ProviderError) as unreadable:
        await manager.create_key_for_grant("grant_1", "grant_1/alice", usd(5), "k")
    assert KEY not in str(unreadable.value) and unreadable.value.__cause__ is None
    assert unreadable.value.__context__ is None  # hidden is not enough: pydantic's error holds the body
    assert not definite_refusal(unreadable.value)


# -- the adapters as one set --------------------------------------------------------------------------------


async def test_the_three_adapters_are_built_over_one_wire(ports: Ports) -> None:
    wire = ClearinghouseWire("http://clearinghouse.test", "lpk_made-up", "grant_1")
    built = ClearinghousePorts.over(wire, ports.engine.store, MemorySecretStore())
    assert isinstance(built.allocations, AllocationsByActor)
    assert not hasattr(built, "credentials")  # the engine resolves its actors' keys itself
    assert "lpk_made-up" not in repr(built)
    await wire.aclose()
