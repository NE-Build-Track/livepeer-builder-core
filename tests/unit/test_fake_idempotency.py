"""The fake payment provider refuses another request under a saved idempotency key (#166 review).

Batteries saves a success under its ``Idempotency-Key``, replays it to the same request and answers any
other request under that key with 409 (docs/references/batteries-api.md). The fake replayed by the key
alone, whatever was sent, so a resume that sent another name passed here and would be refused in
production. What builders' tests see through ``livepeer_builder.testing`` is now what Batteries does.
"""

from collections.abc import Awaitable, Callable
from decimal import Decimal

import pytest

from livepeer_builder.contracts import Money
from livepeer_builder.errors import ProviderError
from livepeer_builder.testing import FakeAllocationManager

FIVE = Money(amount=Decimal(5), currency="USD")
SIX = Money(amount=Decimal(6), currency="USD")
REFUSED = "409: idempotency key was already used with different parameters or operation"

Call = Callable[[FakeAllocationManager], Awaitable[object]]

# Each call that takes an idempotency key, as first sent, and the same call with one thing changed.
OTHER_REQUESTS = [
    pytest.param(
        lambda p: p.create_key_for_grant("t-1", "Alice Example", FIVE, "k"),
        lambda p: p.create_key_for_grant("t-1", "t-1/alice", FIVE, "k"),
        id="an-allotment-under-another-name",
    ),
    pytest.param(
        lambda p: p.create_key_for_grant("t-1", "n", FIVE, "k"),
        lambda p: p.create_key_for_grant("t-1", "n", SIX, "k"),
        id="an-allotment-of-another-amount",
    ),
    pytest.param(
        lambda p: p.create_key_for_grant("t-1", "n", FIVE, "k"),
        lambda p: p.create_key_for_grant("t-1", "n", Money(amount=Decimal("5.0"), currency="USD"), "k"),
        id="an-allotment-with-its-amount-written-another-way",
    ),
    pytest.param(
        lambda p: p.create_key("alloc_1", "t-1/alice", "k"),
        lambda p: p.create_key("alloc_1", "Alice Example", "k"),
        id="a-key-under-another-name",
    ),
    pytest.param(
        lambda p: p.create_key("alloc_1", "n", "k"),
        lambda p: p.create_key("alloc_2", "n", "k"),
        id="a-key-on-another-allocation",
    ),
    pytest.param(
        lambda p: p.create_key("alloc_1", "n", "k"),
        lambda p: p.top_up_allocation("alloc_1", FIVE, "k"),
        id="another-command-under-a-keys-key",
    ),
    pytest.param(
        lambda p: p.top_up_allocation("alloc_1", FIVE, "k"),
        lambda p: p.top_up_allocation("alloc_1", SIX, "k"),
        id="a-top-up-of-another-amount",
    ),
    pytest.param(
        lambda p: p.top_up_allocation("alloc_1", FIVE, "k"),
        lambda p: p.top_up_allocation("alloc_2", FIVE, "k"),
        id="a-top-up-of-another-allocation",
    ),
    pytest.param(
        lambda p: p.top_up_grant("t-1", FIVE, "k"),
        lambda p: p.top_up_grant("t-1", SIX, "k"),
        id="a-grant-top-up-of-another-amount",
    ),
]


async def provider() -> FakeAllocationManager:
    """A grant of 100 and two allocations of 5 from it, ``alloc_1`` and ``alloc_2``. Batteries keeps a key
    per grant, so every request here is within one grant."""
    held = FakeAllocationManager()
    held.add_grant("t-1", total=100)
    for seeded in ("seed-1", "seed-2"):
        await held.create_key_for_grant("t-1", "seeded", FIVE, seeded)
    return held


def state(held: FakeAllocationManager) -> tuple[object, ...]:
    """Everything a call can change: the keys, each allocation's amount and the grant's balances."""
    granted = {name: allocation.granted for name, allocation in held.allocations.items()}
    return (dict(held.keys), granted, held.unallocated("t-1"), held.grants[0].total)


@pytest.mark.parametrize(("first", "other"), OTHER_REQUESTS)
async def test_the_same_request_is_replayed_and_any_other_under_its_key_is_refused(
    first: Call, other: Call
) -> None:
    held = await provider()
    answer = await first(held)
    applied = state(held)
    assert await first(held) == answer and state(held) == applied  # replayed: nothing is applied twice
    with pytest.raises(ProviderError, match=REFUSED) as refused:
        await other(held)
    assert refused.value.status_code == 409 and state(held) == applied  # refused: nothing is applied
    assert await first(held) == answer and state(held) == applied  # the saved success is still replayed


async def test_a_call_that_failed_saved_nothing_under_its_key() -> None:
    """Batteries saves successes only: a refused call's key is free for the corrected request."""
    held = await provider()
    with pytest.raises(ProviderError, match="insufficient unallocated grant balance"):
        await held.create_key_for_grant("t-1", "n", Money(amount=Decimal(500), currency="USD"), "k")
    made = await held.create_key_for_grant("t-1", "n", FIVE, "k")
    assert held.allocations[made.allocation_id].granted == 5 and held.unallocated("t-1") == 85
