"""#168: ``GET .../actors/{actor_id}/allocation`` answers the allocation's amounts with its record.

The ISS-31 guide gives that route "allotted, spent, available and status". It answered the allocation's
record alone, so an enterprise's engine read an actor's allowance through ``GET /v1/allowance``, a work
route, which a clearinghouse that runs no jobs does not serve. The route now carries, as ``allowance``,
exactly what ``GET /v1/allowance`` answers for that actor: one read of the payment provider, by the same
code, so the two cannot mean different things.

The record does not depend on the provider. When the provider cannot be read the route still answers the
record, with ``allowance: null``: unknown, never zero.
"""

import logging
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest

from livepeer_builder.contracts import ActorAllocation, ActorContext, AllocationRead, Allowance, Money
from livepeer_builder.errors import AccessDenied, NotFound, ProviderError, ProviderUnavailable
from livepeer_builder.testing import FakeAllocationManager
from tests.conftest import EngineFactory
from tests.unit.clearinghouse_house import open_house
from tests.unit.payment_ports import OPERATOR
from tests.unit.test_tenants import House, allotted

RECORD = set(ActorAllocation.model_fields)


def usd(amount: str) -> dict[str, str]:
    return {"amount": amount, "currency": "USD"}


@pytest.fixture
async def house(make_engine: EngineFactory) -> AsyncIterator[House]:
    async with open_house(make_engine) as opened:
        yield opened


async def test_the_route_answers_the_record_and_the_amounts_the_provider_reports(
    house: House, manager: FakeAllocationManager
) -> None:
    record = await allotted(house, house.one, "alice", "5")
    manager.allocations[record["allocation_id"]].spent = Decimal("1.25")

    answer = await house.client.get(house.one.allocation("alice"), headers=house.one.admin)

    assert answer.status_code == 200
    body = answer.json()
    assert set(body) == RECORD | {"allowance"}  # additive: every field of the record is where it was
    assert {name: body[name] for name in RECORD} == {name: record[name] for name in RECORD}
    assert body["allowance"] == {
        "allocation_id": record["allocation_id"],
        "status": "active",
        "granted": usd("5"),
        "spent": usd("1.25"),
        "available": usd("3.75"),
    }


async def test_the_amounts_are_the_ones_the_allowance_route_answers_for_that_actor(
    house: House, manager: FakeAllocationManager
) -> None:
    """One meaning on both routes: an actor of the tenant asks ``GET /v1/allowance`` for itself, and the
    tenant's admin reads the allocation. Both are the provider's one read of that allocation."""
    record = await allotted(house, house.one, "alice", "9")
    manager.allocations[record["allocation_id"]].spent = Decimal("2.5")
    engine = house.engine
    assert engine.keys is not None
    issued = await engine.keys.issue(
        OPERATOR, kind="actor", actor_id="alice", tenant_id=house.one.id, operation_ref="alice-key"
    )
    own = await house.client.get("/v1/allowance", headers={"Authorization": f"Bearer {issued.token}"})
    read = await house.client.get(house.one.allocation("alice"), headers=house.one.admin)
    assert own.status_code == read.status_code == 200
    assert read.json()["allowance"] == own.json()


async def test_a_paused_and_a_revoked_allocation_are_answered_with_their_amounts(
    house: House, manager: FakeAllocationManager
) -> None:
    record = await allotted(house, house.one, "alice", "5")
    manager.allocations[record["allocation_id"]].spent = Decimal(2)
    path = house.one.allocation("alice")

    await house.client.post(path + "/status", json={"status": "paused"}, headers=house.one.admin)
    paused = (await house.client.get(path, headers=house.one.admin)).json()
    assert (paused["status"], paused["allowance"]["status"]) == ("paused", "paused")

    await house.client.post(path + "/revoke", headers=house.one.admin)
    revoked = (await house.client.get(path, headers=house.one.admin)).json()
    assert (revoked["status"], revoked["allowance"]["status"]) == ("revoked", "revoked")
    assert revoked["allowance"]["spent"] == usd("2")  # what it spent is still reported


async def test_an_auditor_and_a_clearinghouse_admin_read_it_and_another_tenant_does_not(house: House) -> None:
    await allotted(house, house.one, "alice")
    path = house.one.allocation("alice")
    engine = house.engine
    assert engine.keys is not None
    auditor = await engine.keys.issue(
        OPERATOR, kind="auditor", actor_id="audit", tenant_id=house.one.id, operation_ref="audit-key"
    )
    for headers in ({"Authorization": f"Bearer {auditor.token}"}, house.admin):
        read = await house.client.get(path, headers=headers)
        assert read.status_code == 200 and read.json()["allowance"]["granted"] == usd("5")
    other = await house.client.get(path, headers=house.two.admin)
    assert other.status_code == 403 and "allowance" not in other.text


@pytest.mark.parametrize(
    "failure",
    [ProviderUnavailable("Batteries unreachable"), ProviderError(500, "boom"), ProviderError(404, "gone")],
    ids=["unreachable", "500", "404"],
)
async def test_a_provider_that_cannot_be_read_leaves_the_record_and_an_unknown_allowance(
    house: House, manager: FakeAllocationManager, failure: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    """The record is the engine's own and was always answered without the provider. It still is: the
    amounts are unknown, which is ``null`` and never zero."""
    record = await allotted(house, house.one, "alice")
    manager.fail_once("read_allocation", failure)
    caplog.set_level(logging.WARNING)

    answer = await house.client.get(house.one.allocation("alice"), headers=house.one.admin)

    assert answer.status_code == 200
    body = answer.json()
    assert body["allowance"] is None and body["allocation_id"] == record["allocation_id"]
    assert [entry.event for entry in caplog.records if hasattr(entry, "event")] == [  # type: ignore[attr-defined]
        "payments.allocation_allowance_unread"
    ]
    again = await house.client.get(house.one.allocation("alice"), headers=house.one.admin)
    assert again.json()["allowance"]["granted"] == usd("5")


async def test_an_actor_with_no_allocation_is_404_and_the_provider_is_not_asked(
    house: House, manager: FakeAllocationManager
) -> None:
    calls = list(manager.calls)
    missing = await house.client.get(house.one.allocation("nobody"), headers=house.one.admin)
    assert missing.status_code == 404 and manager.calls == calls


async def test_the_embedded_read_is_the_same_and_the_record_read_is_as_it_was(
    house: House, manager: FakeAllocationManager
) -> None:
    record = await allotted(house, house.one, "alice")
    payments = house.engine.payments

    read = await payments.get_allocation(OPERATOR, house.one.id, "alice")

    assert isinstance(read, AllocationRead)
    assert read.allowance == Allowance(
        allocation_id=record["allocation_id"],
        status="active",
        granted=Money(amount=Decimal(5), currency="USD"),
        spent=Money(amount=Decimal(0), currency="USD"),
        available=Money(amount=Decimal(5), currency="USD"),
    )
    stored = await payments.get_actor(OPERATOR, house.one.id, "alice")
    assert type(stored) is ActorAllocation and read.allocation == stored
    with pytest.raises(NotFound):
        await payments.get_allocation(OPERATOR, house.one.id, "nobody")
    with pytest.raises(AccessDenied):
        await payments.get_allocation(
            ActorContext(actor_id="desk", tenant_id=house.two.id, credential_kind="tenant_admin"),
            house.one.id,
            "alice",
        )
