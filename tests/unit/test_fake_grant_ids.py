"""The fake payment provider gives a created grant an id no grant holds (#165 review).

``create_grant`` counted from ``grant_1`` and never looked at the grants ``add_grant`` seeded, so it could
give a created grant the id of a seeded one: that grant was replaced, its total and its allotments gone,
and the list held the id twice. A created grant takes the next ``grant_<n>`` that no grant holds.
"""

from decimal import Decimal

from livepeer_builder.contracts import Money
from livepeer_builder.testing import FakeAllocationManager

FIVE = Money(amount=Decimal(5), currency="USD")


async def test_a_created_grant_never_takes_a_seeded_grants_id() -> None:
    provider = FakeAllocationManager()
    provider.add_grant("grant_1", total=100)
    await provider.create_key_for_grant("grant_1", "n", FIVE, "k1")
    made = await provider.create_grant("made", FIVE, {"lpb_operation": "op-1"})
    assert made.id != "grant_1"
    assert [grant.id for grant in await provider.list_grants()] == ["grant_1", made.id]
    assert provider.unallocated("grant_1") == Decimal(95)  # its total of 100, less the allotment of 5
    assert (await provider.read_grant("grant_1")).total == Money(amount=Decimal(100), currency="USD")


async def test_ids_held_between_created_ones_are_skipped_and_none_is_overwritten() -> None:
    provider = FakeAllocationManager()
    provider.add_grant("default")
    provider.add_grant("grant_2", total=7)
    first = await provider.create_grant("first", FIVE)
    second = await provider.create_grant("second", FIVE)
    assert (first.id, second.id) == ("grant_1", "grant_3")
    assert (await provider.read_grant("grant_2")).total == Money(amount=Decimal(7), currency="USD")
    assert [grant.id for grant in await provider.list_grants()] == [
        "default",
        "grant_2",
        "grant_1",
        "grant_3",
    ]
