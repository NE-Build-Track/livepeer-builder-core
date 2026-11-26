"""#166: a tenant's usage route returns one tenant's payment records: ``GET /v1/clearinghouse/usage`` to the
tenant's admin or auditor, which names no tenant, and ``GET /v1/clearinghouse/tenants/{tenant_id}/usage``
to a clearinghouse admin (D87 point 3, as amended on 19 November 2026).

It is what an enterprise's engine reads in place of Batteries' usage feed (the ``UsageSource`` port, #168),
so it answers with the port's own page: the rows cost sync attributes a fee by, the rows the source set
aside, and Batteries' cursor. The clearinghouse filters by the tenant's grant, whose id is the tenant's id
(D83), with Batteries' own ``grant_id`` filter: a row is a tenant's when the payment session it was
charged to belongs to an allocation of that grant (``internal/store/manage.go`` at v0.0.3).

Batteries here is the real adapter over an in-memory HTTP transport that filters as Batteries does.
"""

import base64
import json
from collections.abc import AsyncIterator, Callable
from decimal import Decimal
from typing import Any

import httpx
import pytest

from livepeer_builder import EngineSupply
from livepeer_builder.adapters.batteries import BatteriesUsageSource
from livepeer_builder.contracts import ActorContext, Money, UsagePage
from livepeer_builder.errors import AccessDenied, CursorMismatch
from livepeer_builder.protocols import TenantUsageSource
from livepeer_builder.testing import FakeAllocationManager, FakeUsageSource
from livepeer_builder.utils import CursorTrail
from tests.conftest import EngineFactory
from tests.unit.clearinghouse_house import open_house
from tests.unit.test_provider_answers import CYCLES
from tests.unit.test_tenants import House, Tenant, allotted
from tests.unit.test_usage_set_aside import (
    BLANK_IDS,
    NOT_A_FEE_AS_BATTERIES_WRITES_ONE,
    PAST_THE_DECODER,
    STILL_NO_PAGE,
    page_text,
)

BASE = "https://batteries.test"
JSON = {"content-type": "application/json"}


def item(row_id: str, allocation_id: str | None, **changes: Any) -> dict[str, Any]:
    """One ``GET /v1/usage`` item as Batteries serves it (docs/references/batteries-api.md)."""
    row = {
        "id": row_id, "event_id": f"e-{row_id}", "status": "applied", "manifest_id": f"m-{row_id}",
        "allocation_id": allocation_id, "payment_session_id": f"s-{row_id}" if allocation_id else None,
        "request_id": f"r-{row_id}", "pipeline": "live", "computed_fee_eth": "0.000000000000000007",
        "computed_fee_usd": "0.25", "currency": "usd" if allocation_id else None,
        "created_at_ms": 1_760_000_000_000,
    }  # fmt: skip
    return row | changes


class BatteriesUsage:
    """``GET /v1/usage`` with Batteries' filter and cursor. ``grant_of`` says which grant an allocation
    was allotted from; a row with no payment session appears only in the unfiltered list. The cursor is
    bound to its filter, as Batteries' is (``internal/app/pagination.go``)."""

    def __init__(self, manager: FakeAllocationManager) -> None:
        self.manager = manager
        self.items: list[dict[str, Any]] = []
        self.requests: list[dict[str, str]] = []
        # set: every request gets this instead, or what this makes of the request's query
        self.answer: httpx.Response | Exception | Callable[[dict[str, str]], httpx.Response] | None = None

    def grant_of(self, allocation_id: str | None) -> str | None:
        held = self.manager.allocations.get(allocation_id or "")
        return None if held is None else held.grant_id

    def handle(self, request: httpx.Request) -> httpx.Response:
        assert request.method == "GET" and request.url.path == "/v1/usage"
        query = dict(request.url.params)
        self.requests.append(query)
        if isinstance(self.answer, Exception):
            raise self.answer
        if isinstance(self.answer, httpx.Response):
            return self.answer
        if self.answer is not None:
            return self.answer(query)
        grant = query.get("grant_id", "")
        rows = [row for row in self.items if not grant or self.grant_of(row["allocation_id"]) == grant]
        start = 0
        if "cursor" in query:
            cursor = json.loads(base64.urlsafe_b64decode(query["cursor"]))
            if cursor["grant_id"] != grant:
                return httpx.Response(400, json={"error": "cursor does not match resource or filters"})
            start = cursor["seq"]
        end = start + int(query["limit"])
        following = ""
        if end < len(rows):
            following = base64.urlsafe_b64encode(
                json.dumps({"grant_id": grant, "seq": end}).encode()
            ).decode()
        body = json.dumps({"items": rows[start:end], "next_cursor": following})  # an Infinity literal too
        return httpx.Response(200, content=body, headers=JSON)

    def source(self) -> BatteriesUsageSource:
        http = httpx.AsyncClient(base_url=BASE, transport=httpx.MockTransport(self.handle))
        return BatteriesUsageSource(BASE, "t", client=http)


@pytest.fixture
def batteries(manager: FakeAllocationManager) -> BatteriesUsage:
    return BatteriesUsage(manager)


@pytest.fixture
async def house(make_engine: EngineFactory, batteries: BatteriesUsage) -> AsyncIterator[House]:
    async with open_house(make_engine, supply=EngineSupply(usage_source=batteries.source())) as opened:
        yield opened


def usage(tenant: Tenant, **query: Any) -> str:
    """The tenant's own usage route, for its admin or auditor: the tenant is the credential's."""
    return str(httpx.URL(tenant.path("/usage"), params=query))


def named_usage(tenant: Tenant, **query: Any) -> str:
    """The route that names the tenant, for a clearinghouse admin."""
    return str(httpx.URL(tenant.named("/usage"), params=query))


async def two_tenants_with_usage(house: House, batteries: BatteriesUsage, rows: int = 4) -> tuple[str, str]:
    """An actor allotted in each tenant, and ``rows`` charges on each allocation, interleaved as they
    would arrive. Returns the two allocations."""
    alice = (await allotted(house, house.one, "alice"))["allocation_id"]
    bob = (await allotted(house, house.two, "bob"))["allocation_id"]
    for number in range(rows):
        batteries.items += [item(f"one-{number}", alice), item(f"two-{number}", bob)]
    return alice, bob


# -- one tenant's records ---------------------------------------------------------------------------------


async def test_the_usage_route_returns_one_tenants_payment_records(
    house: House, batteries: BatteriesUsage
) -> None:
    alice, _ = await two_tenants_with_usage(house, batteries, rows=2)
    answer = await house.client.get(usage(house.one), headers=house.one.admin)
    assert answer.status_code == 200, answer.text
    page = answer.json()
    assert [row["id"] for row in page["items"]] == ["one-0", "one-1"]  # oldest first, as Batteries lists
    assert page["next_cursor"] == "" and page["set_aside"] == []
    assert page["items"][0] == {
        "id": "one-0",
        "event_id": "e-one-0",
        "status": "applied",
        "manifest_id": "m-one-0",  # the payment's id at the orchestrator
        "allocation_id": alice,
        "payment_session_id": "s-one-0",  # the payment's id at Batteries
        "request_id": "r-one-0",
        "pipeline": "live",  # the capability, in Batteries' word
        "computed_fee_eth": "7E-18",  # an exact decimal as a string, as every fee the engine serves
        "computed_fee_usd": "0.25",
        "currency": "usd",
        "created_at": "2025-10-09T08:53:20Z",
    }
    # the tenant is Batteries' grant: its id is the filter, and nothing else chooses the rows
    assert batteries.requests == [{"grant_id": house.one.id, "limit": "1000"}]

    other = await house.client.get(usage(house.two), headers=house.two.admin)
    assert [row["id"] for row in other.json()["items"]] == ["two-0", "two-1"]


async def test_a_second_tenants_records_never_appear_on_any_page(
    house: House, batteries: BatteriesUsage
) -> None:
    """Two tenants' charges interleaved, and one that belongs to no payment session. Tenant one is read
    to its last page, three rows at a time: every row is its own, none is missing, and every request the
    clearinghouse made named tenant one's grant."""
    await two_tenants_with_usage(house, batteries, rows=10)
    batteries.items.insert(5, item("nobody", None, status="quarantined", computed_fee_eth="[error]"))
    seen: list[str] = []
    cursor, pages = "", 0
    while True:
        query = {"limit": 3} | ({"cursor": cursor} if cursor else {})
        page = (await house.client.get(usage(house.one, **query), headers=house.one.admin)).json()
        seen += [row["id"] for row in page["items"]]
        pages += 1
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert seen == [f"one-{number}" for number in range(10)] and pages == 4
    assert {request["grant_id"] for request in batteries.requests} == {house.one.id}
    # and what the clearinghouse itself reads, unfiltered, holds all 21: the filter is what kept them out
    everything = await batteries.source().page(None, limit=1000)
    assert len(everything.items) + len(everything.set_aside) == 21


async def test_a_cursor_of_one_tenant_reads_nothing_of_another(
    house: House, batteries: BatteriesUsage
) -> None:
    await two_tenants_with_usage(house, batteries, rows=3)
    first = (await house.client.get(usage(house.one, limit=1), headers=house.one.admin)).json()
    assert first["next_cursor"]
    stolen = await house.client.get(usage(house.two, cursor=first["next_cursor"]), headers=house.two.admin)
    assert stolen.status_code == 400 and stolen.json()["code"] == "cursor_mismatch"
    assert "items" not in stolen.json()


async def test_the_answer_is_the_usage_ports_own_page(house: House, batteries: BatteriesUsage) -> None:
    """What an enterprise's engine needs to run cost sync from the route: every field of a usage row, the
    rows the source set aside (one here has a status the engine does not know), and the cursor. The page
    read back is the page the Batteries adapter returns for that grant."""
    alice, _ = await two_tenants_with_usage(house, batteries, rows=2)
    batteries.items.append(item("odd", alice, status="pending"))
    answer = await house.client.get(usage(house.one, limit=2), headers=house.one.admin)
    page = UsagePage.model_validate(answer.json())
    assert [row.id for row in page.items] == ["one-0", "one-1"] and page.next_cursor
    assert page == await BatteriesUsageSource(
        BASE, "t", {"grant_id": house.one.id}, client=batteries.source()._client
    ).page(None, limit=2)

    last = await house.client.get(usage(house.one, cursor=page.next_cursor), headers=house.one.admin)
    rest = UsagePage.model_validate(last.json())
    assert rest.items == () and rest.next_cursor == ""
    assert [(row.id, row.reason) for row in rest.set_aside] == [("odd", "unknown_status")]


# -- who may read ------------------------------------------------------------------------------------------


async def test_a_tenants_admin_and_auditor_read_its_usage_and_a_clearinghouse_admin_any(
    house: House, batteries: BatteriesUsage
) -> None:
    await two_tenants_with_usage(house, batteries, rows=1)
    issued = await house.client.post(
        house.one.path("/keys"),
        json={"kind": "auditor", "actor_id": "dashboard", "operation_ref": "auditor-1"},
        headers=house.one.admin,
    )
    auditor = {"Authorization": f"Bearer {issued.json()['token']}"}
    for path, caller in (
        (usage(house.one), house.one.admin),
        (usage(house.one), auditor),
        (named_usage(house.one), house.admin),
    ):
        answer = await house.client.get(path, headers=caller)
        assert answer.status_code == 200 and [row["id"] for row in answer.json()["items"]] == ["one-0"]
    assert (await house.client.get(named_usage(house.two), headers=house.admin)).status_code == 200


async def test_another_tenants_usage_is_403_before_the_provider_is_asked(
    house: House, batteries: BatteriesUsage
) -> None:
    await two_tenants_with_usage(house, batteries, rows=1)
    issued = await house.client.post(
        house.one.path("/keys"),
        json={"kind": "auditor", "actor_id": "dashboard", "operation_ref": "auditor-1"},
        headers=house.one.admin,
    )
    auditor = {"Authorization": f"Bearer {issued.json()['token']}"}
    for caller in (house.one.admin, auditor):  # a path that names a tenant, the other's or its own
        for tenant in (house.two, house.one):
            refused = await house.client.get(named_usage(tenant), headers=caller)
            assert refused.status_code == 403 and "clearinghouse admin's" in refused.json()["error"]
    assert (await house.client.get(usage(house.one))).status_code == 401  # no credential
    assert batteries.requests == []  # Batteries heard of none of it

    actor = ActorContext(actor_id="alice", tenant_id=house.one.id, permissions=frozenset({"costs.read"}))
    with pytest.raises(AccessDenied):  # an actor key reads its own job costs, never its tenant's usage
        await house.engine.payments.usage.page(actor, house.one.id)  # type: ignore[union-attr]
    assert batteries.requests == []


# -- the edges ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("limit", ["0", "1001", "ten"])
async def test_a_page_is_1_to_1000_rows_as_at_batteries(
    house: House, batteries: BatteriesUsage, limit: str
) -> None:
    refused = await house.client.get(usage(house.one, limit=limit), headers=house.one.admin)
    assert refused.status_code == 422 and batteries.requests == []


async def test_a_provider_that_fails_is_its_own_error_and_never_an_empty_page(
    house: House, batteries: BatteriesUsage
) -> None:
    batteries.answer = httpx.ConnectError("refused")
    assert (await house.client.get(usage(house.one), headers=house.one.admin)).status_code == 503
    batteries.answer = httpx.Response(500, json={"error": "boom"})
    assert (await house.client.get(usage(house.one), headers=house.one.admin)).status_code == 502
    batteries.answer = httpx.Response(200, json={"next_cursor": ""})  # a page with no items is no page
    assert (await house.client.get(usage(house.one), headers=house.one.admin)).status_code == 502


async def test_records_of_an_allocation_allotted_before_a_tenants_id_was_its_grants_id(
    house: House, batteries: BatteriesUsage
) -> None:
    """Before #165 a tenant had a free name (``default``) and its actors named the grant beside it. The
    route filters by the grant whose id the path names: the free name is no grant, so it reads an empty
    page, and the records are the grant's, read under the grant's id as the tenant it is."""
    alice, _ = await two_tenants_with_usage(house, batteries, rows=2)
    free_name = await house.client.get("/v1/clearinghouse/tenants/default/usage", headers=house.admin)
    assert free_name.status_code == 200 and free_name.json() == {
        "items": [],
        "next_cursor": "",
        "set_aside": [],
    }
    assert batteries.requests[-1] == {"grant_id": "default", "limit": "1000"}
    by_grant = await house.client.get(named_usage(house.one), headers=house.admin)
    assert {row["allocation_id"] for row in by_grant.json()["items"]} == {alice}


# -- #158: what Batteries answers oddly, on a tenant's page as on the cost-sync page ------------------------


@pytest.mark.parametrize("field", ["computed_fee_eth", "computed_fee_usd"])
@pytest.mark.parametrize("name", NOT_A_FEE_AS_BATTERIES_WRITES_ONE)
async def test_a_tenants_row_whose_fee_is_not_a_plain_decimal_string_is_set_aside_on_its_page(
    house: House, batteries: BatteriesUsage, name: str, field: str
) -> None:
    """One reader takes every Batteries usage item, so a tenant's page reads a fee as the cost-sync page
    does: the row is in ``set_aside`` with the fee the engine could read, and the page's other rows are
    served."""
    alice, _ = await two_tenants_with_usage(house, batteries, rows=1)
    bad = item("one-bad", alice, **{field: NOT_A_FEE_AS_BATTERIES_WRITES_ONE[name]})
    batteries.items += [bad, item("one-1", alice)]
    answer = await house.client.get(usage(house.one), headers=house.one.admin)
    assert answer.status_code == 200, answer.text
    page = answer.json()
    assert [row["id"] for row in page["items"]] == ["one-0", "one-1"] and page["next_cursor"] == ""
    [aside] = page["set_aside"]
    kept = (None, "0.25") if field == "computed_fee_eth" else ("7E-18", None)  # the fee it could read
    assert (aside["id"], aside["reason"]) == ("one-bad", "unreadable_fee")
    assert (aside["computed_fee_eth"], aside["computed_fee_usd"]) == kept


@pytest.mark.parametrize("row_id", BLANK_IDS.values(), ids=BLANK_IDS)
async def test_a_tenants_row_whose_id_is_blank_is_set_aside_on_its_page(
    house: House, batteries: BatteriesUsage, row_id: str
) -> None:
    alice, _ = await two_tenants_with_usage(house, batteries, rows=1)
    batteries.items.append(item(row_id, alice))
    page = (await house.client.get(usage(house.one), headers=house.one.admin)).json()
    assert [row["id"] for row in page["items"]] == ["one-0"]
    [aside] = page["set_aside"]
    assert aside["id"].startswith("sha256:") and aside["reason"] == "missing_id"


@pytest.mark.parametrize("name", PAST_THE_DECODER)
async def test_a_tenants_item_the_json_decoder_cannot_read_is_set_aside_and_the_rest_of_its_page_is_served(
    house: House, batteries: BatteriesUsage, name: str
) -> None:
    """An integer past the decoder's digit limit, or a field nested past its depth, in one item of a
    tenant's page: that item is set aside under a digest of its text, with no fee, and the page is 200
    with its other rows and its cursor."""
    bad = PAST_THE_DECODER[name]
    body = page_text(item("one-0", "a1"), bad, item("one-1", "a1"), next_cursor="next")
    batteries.answer = httpx.Response(200, content=body.encode(), headers=JSON)
    answer = await house.client.get(usage(house.one), headers=house.one.admin)
    assert answer.status_code == 200, answer.text
    page = answer.json()
    assert [row["id"] for row in page["items"]] == ["one-0", "one-1"] and page["next_cursor"] == "next"
    [aside] = page["set_aside"]
    assert aside["id"].startswith("sha256:") and len(aside["id"]) == 71  # nothing of it is read
    assert (aside["reason"], aside["computed_fee_eth"], aside["computed_fee_usd"]) == (
        "unreadable_row",
        None,
        None,
    )
    assert aside["raw"] == bad[:2000]  # the start of the item's own text, for the reader's log
    assert batteries.requests == [{"grant_id": house.one.id, "limit": "1000"}]


@pytest.mark.parametrize("name", STILL_NO_PAGE)
async def test_a_tenants_page_that_is_malformed_is_still_502_whatever_its_items(
    house: House, batteries: BatteriesUsage, name: str
) -> None:
    batteries.answer = httpx.Response(200, content=STILL_NO_PAGE[name].encode(), headers=JSON)
    refused = await house.client.get(usage(house.one), headers=house.one.admin)
    assert refused.status_code == 502 and "items" not in refused.json()


def going_round(route: dict[str, str]) -> Callable[[dict[str, str]], httpx.Response]:
    """A Batteries whose usage list names the next cursor by ``route``, with one row a page."""

    def answer(query: dict[str, str]) -> httpx.Response:
        cursor = query.get("cursor", "")
        row = item(f"u-{cursor}", "a1")
        return httpx.Response(200, json={"items": [row], "next_cursor": route[cursor]})

    return answer


async def test_a_tenants_page_that_names_its_own_cursor_is_502_and_none_of_it_is_served(
    house: House, batteries: BatteriesUsage
) -> None:
    """Cost sync refuses such a page the first time it reads it. The route reads one page for each
    request, so it refuses the page itself: a reader that followed its cursor would ask without end."""
    batteries.answer = going_round({"": "c1", "c1": "c2", "c2": "c2"})
    first = await house.client.get(usage(house.one), headers=house.one.admin)
    assert first.status_code == 200 and first.json()["next_cursor"] == "c1"  # a page that advances
    stuck = await house.client.get(usage(house.one, cursor="c2"), headers=house.one.admin)
    assert stuck.status_code == 502 and "did not advance" in stuck.json()["error"]
    assert "items" not in stuck.json()
    assert [request.get("cursor") for request in batteries.requests] == [None, "c2"]  # asked for once


@pytest.mark.parametrize("route", CYCLES.values(), ids=CYCLES)
async def test_a_tenants_usage_whose_cursor_comes_back_is_served_page_by_page_and_its_reader_finds_it(
    house: House, batteries: BatteriesUsage, route: dict[str, str]
) -> None:
    """The route keeps nothing between two requests, so it cannot know a cursor was named before: each
    page of a list that goes round (A, B, A) is the page Batteries served, 200. The reader sees every
    cursor of its pass. One that follows them with the trail cost sync keeps stops within a few turns."""
    batteries.answer = going_round(route)
    trail, cursor = CursorTrail(None), ""
    while len(batteries.requests) <= 40:  # a reader that never stopped fails the test instead of hanging it
        query = {"cursor": cursor} if cursor else {}
        answer = await house.client.get(usage(house.one, **query), headers=house.one.admin)
        assert answer.status_code == 200 and [row["id"] for row in answer.json()["items"]] == [f"u-{cursor}"]
        if trail.repeats(answer.json()["next_cursor"]):
            break
        cursor = answer.json()["next_cursor"]
    assert len(batteries.requests) <= 3 * len(route)


class EveryTenant:
    """A supplied ``UsageSource`` with the port's one method: it cannot tell one tenant's rows."""

    async def page(self, cursor: str | None, limit: int = 1000) -> UsagePage:
        return UsagePage(items=())


@pytest.mark.parametrize("source", [None, EveryTenant()], ids=["no usage source", "the port alone"])
async def test_without_a_source_that_reads_one_tenants_usage_the_route_is_absent(
    make_engine: EngineFactory, source: EveryTenant | None
) -> None:
    async with open_house(make_engine, supply=EngineSupply(usage_source=source)) as house:
        assert house.engine.payments.usage is None
        missing = await house.client.get(named_usage(house.one), headers=house.admin)
        assert missing.status_code == 404
        assert (await house.client.get(usage(house.one), headers=house.one.admin)).status_code == 404


# -- the shipped fake --------------------------------------------------------------------------------------


async def test_the_shipped_fake_pages_one_tenants_rows(manager: FakeAllocationManager) -> None:
    source = BatteriesUsage(manager).source()
    assert isinstance(source, TenantUsageSource)
    manager.add_grant("other")
    mine = (await manager.create_key_for_grant("default", "a", _usd("1"), "k1")).allocation_id
    theirs = (await manager.create_key_for_grant("other", "b", _usd("1"), "k2")).allocation_id
    rows = [
        (await _row(BatteriesUsage(manager), item(f"u{number}", mine if number % 2 else theirs)))
        for number in range(6)
    ]
    fake = FakeUsageSource(rows, page_size=2, grant_of={mine: "default", theirs: "other"})
    assert isinstance(fake, TenantUsageSource)
    first = await fake.tenant_page("default", None)
    assert [row.id for row in first.items] == ["u1", "u3"] and first.next_cursor
    last = await fake.tenant_page("default", first.next_cursor)
    assert [row.id for row in last.items] == ["u5"] and last.next_cursor == ""
    assert (await fake.tenant_page("nobody", None)).items == ()
    with pytest.raises(CursorMismatch):  # a cursor is bound to the tenant it was issued for
        await fake.tenant_page("other", first.next_cursor)
    with pytest.raises(CursorMismatch):  # and to the tenant's list: the unfiltered feed does not take it
        await fake.page(first.next_cursor)
    assert len((await fake.page(None, limit=1000)).items) == 2  # the feed cost sync reads is as it was


def _usd(amount: str) -> Money:
    return Money(amount=Decimal(amount), currency="USD")


async def _row(batteries: BatteriesUsage, served: dict[str, Any]) -> Any:
    batteries.items = [served]
    [row] = (await batteries.source().page(None)).items
    return row
