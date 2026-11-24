"""A tenant's grant as Batteries serves it (#165): the read, the list with balances, and the status.

Batteries at the pin (v0.0.3) serves a grant's total and no unallocated balance. That balance is the
total less what the grant's allocations hold (``internal/store/manage.go``: creating or topping up an
allocation moves the amount out of the grant's unallocated account and adds it to the allocation's
``allocated`` amount, and a revoke moves back what was not spent and lowers ``allocated`` by it). The
adapter reads both and never turns an answer it cannot read into a zero.

The same allocations say what each still holds unspent, ``allocated`` less ``spent`` (#167): the wallet
report adds that to the unallocated balance for what a tenant may still spend. Batteries charges after
it authorizes, so an allocation can spend more than it holds (``internal/store/ingest.go``: its
``available`` balance goes below zero and its status becomes ``exhausted``). Such an allocation holds
nothing, and takes nothing off another's.
"""

import json
from decimal import Decimal
from typing import Any

import httpx
import pytest

from livepeer_builder.adapters.batteries import BatteriesClient
from livepeer_builder.contracts import Money
from livepeer_builder.errors import ProviderError

BASE = "https://batteries.test"


def grant(grant_id: str, total: str, status: str = "active", currency: str = "usd") -> dict[str, Any]:
    return {
        "id": grant_id, "name": f"name of {grant_id}", "sponsor": "s", f"total_{currency}": total,
        "currency": currency, "status": status, "metadata": "", "created_at_ms": 1,
    }  # fmt: skip


def allocation(allocation_id: str, grant_id: str, allocated: str, currency: str = "usd") -> dict[str, Any]:
    return {
        "id": allocation_id, "grant_id": grant_id, "name": "n", f"allocated_{currency}": allocated,
        "currency": currency, "status": "active", f"available_{currency}": allocated, f"spent_{currency}": "0",
    }  # fmt: skip


def charged(
    allocation_id: str,
    grant_id: str,
    allocated: str,
    spent: str,
    status: str = "active",
    currency: str = "usd",
) -> dict[str, Any]:
    """An allocation that was charged, as Batteries serves it: ``available`` is its ledger balance,
    ``allocated`` less ``spent``, which is below zero when it is overdrawn."""
    available = format(Decimal(allocated) - Decimal(spent), "f")
    return allocation(allocation_id, grant_id, allocated, currency) | {
        "status": status, f"spent_{currency}": spent, f"available_{currency}": available,
    }  # fmt: skip


class Batteries:
    """The grant and allocation routes, paged two items at a time as Batteries pages by ``seq``."""

    def __init__(self, grants: list[dict[str, Any]], allocations: list[dict[str, Any]]) -> None:
        self.grants, self.allocations = grants, allocations
        self.requests: list[tuple[str, str, dict[str, str]]] = []
        self.bodies: list[Any] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        path, query = request.url.path, dict(request.url.params)
        self.requests.append((request.method, path, query))
        if request.method == "PATCH":
            self.bodies.append(json.loads(request.content))
            return httpx.Response(200, json={"id": path.split("/")[3], "status": self.bodies[-1]["status"]})
        if path in ("/v1/grants", "/v1/allocations"):
            items = self.grants if path == "/v1/grants" else self.allocations
            if "grant_id" in query:
                items = [item for item in items if item.get("grant_id") == query["grant_id"]]
            start = int(query.get("cursor", 0))
            more = start + 2 < len(items)
            return httpx.Response(
                200, json={"items": items[start : start + 2], "next_cursor": str(start + 2) if more else ""}
            )
        found = [item for item in self.grants if item["id"] == path.rsplit("/", 1)[-1]]
        if not found:
            return httpx.Response(404, json={"error": "resource not found"})
        return httpx.Response(200, json=found[0])

    def client(self) -> BatteriesClient:
        http = httpx.AsyncClient(base_url=BASE, transport=httpx.MockTransport(self.handle))
        return BatteriesClient(BASE, "t", client=http)


def usd(amount: str) -> Money:
    return Money(amount=Decimal(amount), currency="USD")


async def test_a_grants_unallocated_balance_is_its_total_less_what_its_allocations_hold() -> None:
    batteries = Batteries(
        [grant("g1", "100"), grant("g2", "7")],
        [allocation("a1", "g1", "60"), allocation("a2", "g2", "7"), allocation("a3", "g1", "0.5"),
         allocation("a4", "g1", "9.5")],
    )  # fmt: skip
    read = await batteries.client().read_grant("g1")
    assert (read.tenant_id, read.name, read.status) == ("g1", "name of g1", "active")
    assert (read.total, read.unallocated) == (usd("100"), usd("30.0"))
    # the grant first, then its own allocations only, page by page
    assert batteries.requests == [
        ("GET", "/v1/grants/g1", {}),
        ("GET", "/v1/allocations", {"grant_id": "g1", "limit": "1000"}),
        ("GET", "/v1/allocations", {"grant_id": "g1", "limit": "1000", "cursor": "2"}),
    ]


async def test_every_tenant_is_listed_with_its_own_grants_balance() -> None:
    batteries = Batteries(
        [grant("g1", "100"), grant("g2", "7", "paused"), grant("g3", "1.5", currency="eth")],
        [allocation("a1", "g1", "60"), allocation("a2", "g2", "7"), allocation("a3", "g1", "40")],
    )
    listed = await batteries.client().grant_balances()
    assert [(t.tenant_id, t.status, t.total, t.unallocated) for t in listed] == [
        ("g1", "active", usd("100"), usd("0")),
        ("g2", "paused", usd("7"), usd("0")),
        ("g3", "active", Money(amount=Decimal("1.5"), currency="ETH"), Money(amount=Decimal("1.5"), currency="ETH")),
    ]  # fmt: skip
    assert [path for _, path, _ in batteries.requests] == [
        "/v1/grants", "/v1/grants", "/v1/allocations", "/v1/allocations"
    ]  # fmt: skip


def eth(amount: str) -> Money:
    return Money(amount=Decimal(amount), currency="ETH")


async def test_what_a_grants_allocations_still_hold_is_summed_allocation_by_allocation() -> None:
    """An overdrawn allocation adds nothing and takes nothing off another: g1's sums alone (90 allotted,
    85.25 spent) would read 4.75, where a2 still holds 19.75 and a5 its 10. Read with the requests the
    balance already made."""
    batteries = Batteries(
        [grant("g1", "100"), grant("g2", "7"), grant("g3", "1.5", currency="eth"), grant("g4", "3")],
        [charged("a1", "g1", "60", "70", "exhausted"), charged("a2", "g1", "20", "0.25"),
         charged("a3", "g2", "7", "7", "exhausted"),
         charged("a4", "g3", "1", "0.000000000000000007", currency="eth"),
         charged("a5", "g1", "10", "0", "paused"),
         charged("a6", "g3", "0.25", "0.250000000000000001", "exhausted", currency="eth")],
    )  # fmt: skip
    listed = await batteries.client().grant_balances()
    assert [(t.tenant_id, t.unallocated, t.allocated_unspent, t.spent) for t in listed] == [
        ("g1", usd("10"), usd("29.75"), usd("70.25")),  # a paused allocation can be resumed: it counts
        ("g2", usd("0"), usd("0"), usd("7")),
        ("g3", eth("0.25"), eth("0.999999999999999993"), eth("0.250000000000000008")),
        ("g4", usd("3"), usd("0"), usd("0")),  # no allocation: nothing is held in one
    ]
    assert [path for _, path, _ in batteries.requests] == [
        "/v1/grants", "/v1/grants", "/v1/allocations", "/v1/allocations", "/v1/allocations"
    ]  # fmt: skip

    batteries.requests.clear()
    one = await batteries.client().read_grant("g1")
    assert (one.unallocated, one.allocated_unspent, one.spent) == (usd("10"), usd("29.75"), usd("70.25"))
    assert [path for _, path, _ in batteries.requests] == [
        "/v1/grants/g1",
        "/v1/allocations",
        "/v1/allocations",
    ]
    assert (await batteries.client().read_grant("g4")).allocated_unspent == usd("0")


async def test_a_revoked_allocation_holds_nothing_and_what_it_returned_is_counted_once() -> None:
    """A revoke moves what the allocation had not spent back to the grant and takes it off ``allocated``
    (``internal/store/manage.go``, the ``revoke:`` transfer), so a revoked allocation is served with
    ``allocated`` equal to ``spent``: the 50 that a1 returned is in the unallocated balance and in no
    allocation. A late charge raises ``spent`` only (a2), and one revoked while overdrawn returned
    nothing (a3). None of them holds anything, and none takes anything off the live one (a4)."""
    batteries = Batteries(
        [grant("g1", "100")],
        [charged("a1", "g1", "10", "10", "revoked"), charged("a2", "g1", "5", "5.5", "revoked"),
         charged("a3", "g1", "2", "3", "revoked"), charged("a4", "g1", "20", "8")],
    )  # fmt: skip
    read = await batteries.client().read_grant("g1")
    assert (read.unallocated, read.allocated_unspent, read.spent) == (usd("63"), usd("12"), usd("26.5"))
    assert [(t.unallocated, t.allocated_unspent) for t in await batteries.client().grant_balances()] == [
        (usd("63"), usd("12"))
    ]


async def test_a_tenant_is_paused_by_setting_its_grants_status() -> None:
    batteries = Batteries([grant("g/1", "1")], [])
    await batteries.client().set_grant_status("g/1", "paused")
    assert batteries.bodies == [{"status": "paused"}]
    assert batteries.requests[0][:2] == ("PATCH", "/v1/grants/g/1/status")  # one encoded segment on the wire


async def test_a_grant_the_provider_does_not_hold_is_its_404() -> None:
    with pytest.raises(ProviderError) as refused:
        await Batteries([], []).client().read_grant("nobody")
    assert refused.value.status_code == 404


async def test_a_balance_between_two_reads_is_never_below_zero() -> None:
    """An allotment made between the grant read and the allocation read is counted, and a top-up made
    between them is not yet: the difference is held at zero, where the provider holds the balance."""
    batteries = Batteries([grant("g1", "10")], [allocation("a1", "g1", "10"), allocation("a2", "g1", "5")])
    assert (await batteries.client().read_grant("g1")).unallocated == usd("0")


@pytest.mark.parametrize(
    ("grants", "allocations"),
    [
        ([grant("g1", "[error]")], []),
        ([grant("g1", "")], []),
        ([grant("g1", "NaN")], []),
        ([{k: v for k, v in grant("g1", "5").items() if k != "total_usd"}], []),
        ([grant("g1", "5") | {"total_usd": 5}], []),
        ([grant("g1", "5")], [allocation("a1", "g1", "[error]")]),
        ([grant("g1", "5")], [allocation("a1", "g1", "1") | {"allocated_usd": None}]),
    ],
    ids=["total error", "total empty", "total NaN", "no total", "total a number", "allocated error",
         "allocated null"],
)  # fmt: skip
async def test_an_amount_the_engine_cannot_read_is_never_a_zero(
    grants: list[dict[str, Any]], allocations: list[dict[str, Any]]
) -> None:
    """Read as zero, an unreadable ``allocated`` would show money to allot that is not there."""
    client = Batteries(grants, allocations).client()
    with pytest.raises(ProviderError):
        await client.read_grant("g1")
    with pytest.raises(ProviderError):
        await client.grant_balances()


async def test_an_allocation_that_names_no_grant_fails_the_list() -> None:
    nameless = {k: v for k, v in allocation("a1", "g1", "1").items() if k != "grant_id"}
    with pytest.raises(ProviderError, match="grant_id"):
        await Batteries([grant("g1", "5")], [nameless]).client().grant_balances()


async def test_a_list_whose_cursor_does_not_advance_is_refused() -> None:
    def stuck(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": [], "next_cursor": "7"})

    http = httpx.AsyncClient(base_url=BASE, transport=httpx.MockTransport(stuck))
    with pytest.raises(ProviderError, match="did not advance"):
        await BatteriesClient(BASE, "t", client=http).grant_balances()
