"""#114 C21: one strict reader for every Batteries answer.

A 2xx that was not a JSON object raised inside the adapter and became a 500, and a list page with no
``items`` read as an empty last page: a resumed ``create_grant`` that listed the grants to find its
earlier one then found none and created a second. Now a 2xx must be a JSON object, and a list page must
have ``items`` as a list of objects and ``next_cursor`` as a string or absent. Anything else is a
``ProviderError``: HTTP 502, and to the operation ledger an unknown outcome, so the reference stays held.
"""

import json
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from livepeer_builder import BuilderEngine, EngineSupply
from livepeer_builder.adapters.batteries import BatteriesClient, BatteriesUsageSource
from livepeer_builder.contracts import ActorContext, Money
from livepeer_builder.errors import OperationInProgress, ProviderError, ProviderUnavailable
from livepeer_builder.http import create_app
from livepeer_builder.services.costs import CostSyncWorker
from livepeer_builder.services.failure_codes import source_failure
from livepeer_builder.services.ledger import definite_refusal
from livepeer_builder.store import SqlStore
from livepeer_builder.store import schema as t
from livepeer_builder.testing import FixedClock
from tests.conftest import EngineFactory

USD = Money(amount=Decimal("2.5"), currency="USD")
BASE = "https://batteries.test"


def answering(*bodies: bytes | dict[str, Any], status: int = 200) -> httpx.AsyncClient:
    """A Batteries that answers each request with the next of ``bodies``; the last one repeats."""
    left = list(bodies)

    def handle(request: httpx.Request) -> httpx.Response:
        del request
        body = left.pop(0) if len(left) > 1 else left[0]
        content = body if isinstance(body, bytes) else json.dumps(body).encode()
        return httpx.Response(status, content=content, headers={"Content-Type": "application/json"})

    return httpx.AsyncClient(base_url=BASE, transport=httpx.MockTransport(handle))


NOT_AN_OBJECT = {
    "no body": b"",
    "not JSON": b"<html>bad gateway</html>",
    "a list": b"[]",
    "a string": b'"ok"',
    "null": b"null",
    "a number": b"7",
    "not UTF-8": b"\xff\xfe",
}
CALLS: dict[str, Callable[[BatteriesClient], Awaitable[object]]] = {
    "create_key_for_grant": lambda client: client.create_key_for_grant("g1", "alice", USD, "idem-1"),
    "create_key": lambda client: client.create_key("a1", "rotated", "idem-2"),
    "fund_allocation": lambda client: client.fund_allocation("a1", USD, "idem-3"),
    "fund_grant": lambda client: client.fund_grant("g1", USD, "idem-4"),
    "set_allocation_status": lambda client: client.set_allocation_status("a1", "paused"),
    "revoke_allocation": lambda client: client.revoke_allocation("a1"),
    "revoke_key": lambda client: client.revoke_key("k1"),
    "read_allocation": lambda client: client.read_allocation("a1"),
    "list_grants": lambda client: client.list_grants(),
    "grant_page": lambda client: client.grant_page(None),
    "create_grant": lambda client: client.create_grant("ops", USD),
}


@pytest.mark.parametrize("call", CALLS)
@pytest.mark.parametrize("body", NOT_AN_OBJECT)
@pytest.mark.parametrize("status", [200, 201])
async def test_a_2xx_that_is_not_a_json_object_is_a_provider_error_for_every_call(
    call: str, body: str, status: int
) -> None:
    client = BatteriesClient(BASE, "t", client=answering(NOT_AN_OBJECT[body], status=status))
    with pytest.raises(ProviderError, match="JSON object") as refused:
        await CALLS[call](client)
    assert refused.value.status_code == status  # the provider's own status: an answer the engine cannot read
    assert not definite_refusal(refused.value)  # an unknown outcome to the ledger: the reference stays held


REDIRECT_BODIES: dict[str, bytes | dict[str, Any]] = {
    "an empty object": {},
    "what the call would answer": {"id": "a1", "key": "lpk_x", "status": "active", "items": []},
    "a ServeMux redirect": b'<a href="/v1/grants/g1/fund">Moved Permanently</a>.',
}


@pytest.mark.parametrize("call", CALLS)
@pytest.mark.parametrize("body", REDIRECT_BODIES)
@pytest.mark.parametrize("status", [301, 302, 307, 308])
async def test_a_redirect_is_no_answer_for_any_call_whatever_its_body(
    call: str, body: str, status: int
) -> None:
    """A 3xx with a JSON object in it read as the call's answer: a fund or a revoke that was only
    redirected counted as done. Go's ServeMux answers a 301 for a path it would clean."""
    client = BatteriesClient(BASE, "t", client=answering(REDIRECT_BODIES[body], status=status))
    with pytest.raises(ProviderError, match="redirect") as refused:
        await CALLS[call](client)
    assert refused.value.status_code == status
    assert not definite_refusal(refused.value)  # it may have been applied elsewhere: the reference stays held
    assert "href" not in str(refused.value)  # the body is not quoted


@pytest.mark.parametrize("status", [301, 302, 307, 308])
async def test_a_redirected_usage_page_is_no_page(status: int) -> None:
    source = BatteriesUsageSource(
        BASE, "t", client=answering({"items": [], "next_cursor": ""}, status=status)
    )
    with pytest.raises(ProviderError, match="redirect") as refused:
        await source.page(None)
    assert refused.value.status_code == status


@pytest.mark.parametrize("body", NOT_AN_OBJECT)
async def test_a_usage_answer_that_is_not_a_json_object_is_a_provider_error(body: str) -> None:
    source = BatteriesUsageSource(BASE, "t", client=answering(NOT_AN_OBJECT[body]))
    with pytest.raises(ProviderError, match="JSON object") as refused:
        await source.page(None)
    assert source_failure(refused.value) == "invalid_answer"


GRANT = {"id": "grant_1", "name": "ops", "status": "active", "currency": "usd", "total_usd": "10"}
MALFORMED_PAGES: dict[str, dict[str, Any]] = {
    "no items": {},
    "an error in a 2xx": {"error": "overloaded"},
    "items null": {"items": None, "next_cursor": ""},
    "items an object": {"items": {"0": GRANT}},
    "items a string": {"items": "none"},
    "an item that is a number": {"items": [GRANT, 7]},
    "an item that is null": {"items": [None]},
    "an item that is a list": {"items": [[GRANT]]},
    "next_cursor a number": {"items": [], "next_cursor": 5},
    "next_cursor null": {"items": [], "next_cursor": None},
    "next_cursor a list": {"items": [GRANT], "next_cursor": ["c1"]},
}


@pytest.mark.parametrize("page", MALFORMED_PAGES)
async def test_a_malformed_grant_page_is_a_provider_error_never_an_empty_page(page: str) -> None:
    client = BatteriesClient(BASE, "t", client=answering(MALFORMED_PAGES[page]))
    with pytest.raises(ProviderError) as refused:
        await client.grant_page(None)
    assert refused.value.status_code == 200 and not definite_refusal(refused.value)
    with pytest.raises(ProviderError):
        await client.list_grants()


@pytest.mark.parametrize("page", MALFORMED_PAGES)
async def test_a_malformed_usage_page_is_a_provider_error_never_an_empty_page(page: str) -> None:
    source = BatteriesUsageSource(BASE, "t", client=answering(MALFORMED_PAGES[page]))
    with pytest.raises(ProviderError) as refused:
        await source.page(None)
    assert source_failure(refused.value) == "invalid_answer"


@pytest.mark.parametrize(
    ("page", "cursor"),
    [
        ({"items": []}, None),
        ({"items": [], "next_cursor": ""}, None),
        ({"items": [GRANT], "next_cursor": "c1"}, "c1"),
    ],
    ids=["no next_cursor", "an empty next_cursor", "a next_cursor"],
)
async def test_a_well_formed_page_is_read_and_an_absent_cursor_ends_the_list(
    page: dict[str, Any], cursor: str | None
) -> None:
    grants = await BatteriesClient(BASE, "t", client=answering(page)).grant_page(None)
    assert (len(grants.grants), grants.next_cursor) == (len(page["items"]), cursor)
    usage = await BatteriesUsageSource(BASE, "t", client=answering(page | {"items": []})).page(None)
    assert (usage.items, usage.next_cursor) == ((), cursor or "")


async def test_a_malformed_usage_page_fails_the_sync_and_keeps_the_cursor(
    make_engine: EngineFactory, store: SqlStore
) -> None:
    good = {"id": "u1", "status": "applied", "created_at_ms": 1760270400000}
    source = BatteriesUsageSource(BASE, "t", client=answering({"items": [good], "next_cursor": "p1"}, {}))
    engine = await make_engine(supply=EngineSupply(usage_source=source))
    with pytest.raises(ProviderError):
        await engine.costs.sync_once()  # the second page has no items: not an empty last page
    checkpoint = await store.cost_checkpoint()
    assert checkpoint is not None and checkpoint.resume_cursor == "p1"  # the next sync asks for it again


async def test_a_usage_item_that_is_not_an_object_fails_the_page_and_is_not_set_aside(
    make_engine: EngineFactory, store: SqlStore
) -> None:
    good = {"id": "u1", "status": "applied", "created_at_ms": 1760270400000}
    source = BatteriesUsageSource(BASE, "t", client=answering({"items": [good, "u2"], "next_cursor": ""}))
    engine = await make_engine(supply=EngineSupply(usage_source=source))
    with pytest.raises(ProviderError):
        await engine.costs.sync_once()
    assert await store.cost_checkpoint() is None and await store.count_set_aside_usage() == 0


async def test_the_rest_service_answers_a_malformed_provider_answer_with_502(
    make_engine: EngineFactory,
) -> None:
    desk = BatteriesClient(BASE, "t", client=answering(b"<html>bad gateway</html>"))
    engine = await make_engine(supply=EngineSupply(grants_manager=desk))
    key = await engine.keys.bootstrap_admin()
    transport = httpx.ASGITransport(
        app=create_app(engine, manage_lifecycle=False), raise_app_exceptions=False
    )
    headers = {"Authorization": f"Bearer {key.token}"}
    async with httpx.AsyncClient(transport=transport, base_url="http://engine", headers=headers) as client:
        listed = await client.get("/v1/admin/grants")
        created = await client.post(
            "/v1/admin/grants",
            json={"name": "ops", "amount": {"amount": "5", "currency": "USD"}, "operation_ref": "grant-1"},
        )
    assert (listed.status_code, created.status_code) == (502, 502)
    assert "bad gateway" not in listed.text + created.text


class GrantDesk:
    """Batteries' grant routes over HTTP: ``POST /v1/grants``, the read-back, and the paged list."""

    def __init__(self) -> None:
        self.grants: list[dict[str, Any]] = []
        self.lose_read_back = True  # the first read-back times out: the create's outcome is unknown
        self.list_answers: list[dict[str, Any] | None] = []  # None: the real list

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            fields = json.loads(request.content)
            grant = {"id": f"grant_{len(self.grants) + 1}", "currency": "usd", "total_usd": "5", **fields}
            self.grants.append(grant)
            return httpx.Response(201, json={"id": grant["id"]})
        if request.url.path != "/v1/grants":  # the read-back of one grant
            if self.lose_read_back:
                self.lose_read_back = False
                raise httpx.ReadTimeout("no answer")
            return httpx.Response(200, json=self.grants[-1])
        scripted = self.list_answers.pop(0) if self.list_answers else None
        real = {"items": self.grants, "next_cursor": ""}
        return httpx.Response(200, json=real if scripted is None else scripted)


@pytest.mark.parametrize("page", ["no items", "an error in a 2xx", "items null"])
async def test_a_resumed_grant_create_never_creates_a_second_grant_over_a_malformed_list(
    make_engine: EngineFactory, admin: ActorContext, clock: FixedClock, page: str
) -> None:
    """The path the review traced: the create's answer is lost, the resume lists the grants to find the
    one it tagged, and the list answers 2xx without ``items``. That read as "no grants", so it created
    a second grant."""
    batteries = GrantDesk()
    client = httpx.AsyncClient(base_url=BASE, transport=httpx.MockTransport(batteries.handle))
    engine = await make_engine(supply=EngineSupply(grants_manager=BatteriesClient(BASE, "t", client=client)))
    desk = engine.payments.grants
    assert desk is not None
    usd = Money(amount=Decimal(5), currency="USD")
    with pytest.raises(ProviderUnavailable):
        await desk.create_grant(admin, "ops", usd, operation_ref="grant-1")  # created; the answer is lost
    assert len(batteries.grants) == 1

    clock.advance(120)
    batteries.list_answers = [MALFORMED_PAGES[page]]
    try:
        resumed: object = await desk.create_grant(admin, "ops", usd, operation_ref="grant-1")
    except ProviderError as unknown:
        resumed = unknown
    assert len(batteries.grants) == 1, "the resume created a second grant"
    assert isinstance(resumed, ProviderError) and not definite_refusal(resumed)
    with pytest.raises(OperationInProgress):  # the reference is still held
        await desk.create_grant(admin, "ops", usd, operation_ref="grant-1")

    clock.advance(120)
    adopted = await desk.create_grant(admin, "ops", usd, operation_ref="grant-1")  # the list answers
    assert (adopted.id, len(batteries.grants)) == ("grant_1", 1)


TAGGED = GRANT | {"metadata": '{"lpb_operation": "tag-1"}'}
# what the list serves in place of a grant: an object the engine cannot read as one
UNREADABLE_GRANTS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "no fields": lambda grant: {},
    "an error": lambda grant: {"error": "overloaded"},
    "metadata an object": lambda grant: grant | {"metadata": json.loads(grant["metadata"])},
    "metadata null": lambda grant: grant | {"metadata": None},
    "metadata a number": lambda grant: grant | {"metadata": 7},
    "no id": lambda grant: {key: value for key, value in grant.items() if key != "id"},
    "id null": lambda grant: grant | {"id": None},
    "id empty": lambda grant: grant | {"id": ""},
    "id a number": lambda grant: grant | {"id": 7},
    "name null": lambda grant: grant | {"name": None},
    "status an object": lambda grant: grant | {"status": {"is": "active"}},
    "currency a number": lambda grant: grant | {"currency": 840},
    "total a number": lambda grant: grant | {"total_usd": 10},
}


@pytest.mark.parametrize("item", UNREADABLE_GRANTS)
async def test_a_grant_the_engine_cannot_read_is_a_provider_error_never_a_grant_with_no_tag(
    item: str,
) -> None:
    page = {"items": [GRANT, UNREADABLE_GRANTS[item](TAGGED)], "next_cursor": ""}
    client = BatteriesClient(BASE, "t", client=answering(page))
    with pytest.raises(ProviderError) as refused:
        await client.grant_page(None)
    assert refused.value.status_code == 200 and not definite_refusal(refused.value)
    assert "tag-1" not in str(refused.value) and "overloaded" not in str(refused.value)  # no body quoted
    with pytest.raises(ProviderError):
        await client.list_grants()


@pytest.mark.parametrize(
    ("metadata", "read"),
    [
        (..., {}),
        ("", {}),
        ("ops team, Q4", {}),
        ('["a"]', {}),
        ('{"lpb_operation": "tag-1", "n": 1}', {"lpb_operation": "tag-1"}),
    ],
    ids=["absent", "empty", "free text", "a JSON list", "an object"],
)
async def test_a_grant_whose_metadata_is_text_is_read_and_only_an_object_of_strings_is_a_tag(
    metadata: Any, read: dict[str, str]
) -> None:
    """Batteries keeps metadata as text, ``""`` for a grant with none: such a grant is not the engine's."""
    item = GRANT if metadata is ... else GRANT | {"metadata": metadata}
    page = await BatteriesClient(BASE, "t", client=answering({"items": [item]})).grant_page(None)
    assert [(grant.id, grant.metadata) for grant in page.grants] == [("grant_1", read)]


@pytest.mark.parametrize("item", UNREADABLE_GRANTS)
async def test_a_resumed_grant_create_never_creates_a_second_grant_over_a_grant_it_cannot_read(
    make_engine: EngineFactory, admin: ActorContext, clock: FixedClock, item: str
) -> None:
    """The list answers with its envelope intact and, in place of the grant the resume looks for, an
    object the engine cannot read as a grant. That read as a grant with no tag, so "no such grant", and
    the resume created a second one."""
    batteries = GrantDesk()
    client = httpx.AsyncClient(base_url=BASE, transport=httpx.MockTransport(batteries.handle))
    engine = await make_engine(supply=EngineSupply(grants_manager=BatteriesClient(BASE, "t", client=client)))
    desk = engine.payments.grants
    assert desk is not None
    usd = Money(amount=Decimal(5), currency="USD")
    with pytest.raises(ProviderUnavailable):
        await desk.create_grant(admin, "ops", usd, operation_ref="grant-1")  # created; the answer is lost
    assert len(batteries.grants) == 1

    clock.advance(120)
    batteries.list_answers = [{"items": [UNREADABLE_GRANTS[item](batteries.grants[0])], "next_cursor": ""}]
    try:
        resumed: object = await desk.create_grant(admin, "ops", usd, operation_ref="grant-1")
    except ProviderError as unknown:
        resumed = unknown
    assert len(batteries.grants) == 1, "the resume created a second grant"
    assert isinstance(resumed, ProviderError) and not definite_refusal(resumed)
    with pytest.raises(OperationInProgress):  # the reference is still held
        await desk.create_grant(admin, "ops", usd, operation_ref="grant-1")

    clock.advance(120)
    adopted = await desk.create_grant(admin, "ops", usd, operation_ref="grant-1")  # the list answers
    assert (adopted.id, len(batteries.grants)) == ("grant_1", 1)


async def ledger(store: SqlStore) -> list[tuple[str, str]]:
    """(operation_ref, state) of every admin command the ledger holds."""
    async with store.engine.connect() as conn:
        return [
            tuple(row)
            for row in await conn.execute(select(t.operations.c.operation_ref, t.operations.c.state))
        ]


def rest(engine: BuilderEngine, token: str) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(
        app=create_app(engine, manage_lifecycle=False), raise_app_exceptions=False
    )
    headers = {"Authorization": f"Bearer {token}"}
    return httpx.AsyncClient(transport=transport, base_url="http://engine", headers=headers)


READ_BACKS: dict[str, dict[str, Any]] = {
    "an error in a 2xx": {"error": "overloaded"},
    "an empty object": {},
    "null fields": {"id": None, "name": None, "status": None, "currency": None, "total_usd": None},
    "a name that is a number": GRANT | {"name": 7},
}


@pytest.mark.parametrize("read_back", READ_BACKS)
async def test_a_grant_read_back_the_engine_cannot_read_is_an_unknown_outcome_not_a_created_grant(
    make_engine: EngineFactory, store: SqlStore, read_back: str
) -> None:
    """The create answered with an id and the read-back with an object that is no grant. That was recorded
    as the finished command, a grant of empty fields or of ``"None"``, and replayed on every repeat."""

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(201, json={"id": "grant_1"})
        return httpx.Response(200, json=READ_BACKS[read_back])

    client = httpx.AsyncClient(base_url=BASE, transport=httpx.MockTransport(handle))
    engine = await make_engine(supply=EngineSupply(grants_manager=BatteriesClient(BASE, "t", client=client)))
    key = await engine.keys.bootstrap_admin()
    body = {"name": "ops", "amount": {"amount": "5", "currency": "USD"}, "operation_ref": "grant-1"}
    async with rest(engine, key.token) as http:
        created = await http.post("/v1/admin/grants", json=body)
        again = await http.post("/v1/admin/grants", json=body)
    assert (created.status_code, again.status_code) == (503, 409)  # the grant exists: unknown, and held
    assert "overloaded" not in created.text
    assert await ledger(store) == [("grant-1", "started")]


KEY = {"id": "key_1", "allocation_id": "alloc_1", "api_key": "lpg_key_1_secret"}
UNUSABLE_KEYS: dict[str, dict[str, Any]] = {
    "nulls": {"id": None, "allocation_id": None, "api_key": None},
    "empty strings": {"id": "", "allocation_id": "", "api_key": ""},
    "objects": {"id": {"x": 1}, "allocation_id": ["a"], "api_key": {"k": "v"}},
    "an error in a 2xx": {"error": "overloaded"},
    "no allocation id": {key: value for key, value in KEY.items() if key != "allocation_id"},
    "a key id that is a number": KEY | {"id": 7},
    "a secret that is null": KEY | {"api_key": None},
}


@pytest.mark.parametrize("call", ["create_key_for_grant", "create_key"])
@pytest.mark.parametrize("answer", UNUSABLE_KEYS)
async def test_a_key_answer_without_its_required_values_is_a_provider_error(call: str, answer: str) -> None:
    client = BatteriesClient(BASE, "t", client=answering(UNUSABLE_KEYS[answer], status=201))
    with pytest.raises(ProviderError) as refused:
        await CALLS[call](client)
    assert refused.value.status_code == 200 and not definite_refusal(refused.value)
    assert "secret" not in str(refused.value) and "overloaded" not in str(refused.value)  # no value quoted


@pytest.mark.parametrize("answer", UNUSABLE_KEYS)
async def test_provisioning_over_a_key_answer_it_cannot_read_is_an_unknown_outcome_not_a_done_command(
    make_engine: EngineFactory, store: SqlStore, answer: str
) -> None:
    """``POST /v1/api-keys`` answered 201 with null, empty or object values. They were turned into text:
    provisioning answered 201 with ``allocation_id: "None"`` and the ledger recorded it done. An answer
    that lacked a key raised ``KeyError`` and answered 500."""
    manager = BatteriesClient(BASE, "t", client=answering(UNUSABLE_KEYS[answer], status=201))
    engine = await make_engine(supply=EngineSupply(allocation_manager=manager))
    key = await engine.keys.bootstrap_admin()
    body = {"amount": {"amount": "5", "currency": "USD"}, "grant_id": "g1", "operation_ref": "p-1"}
    async with rest(engine, key.token) as http:
        created = await http.post("/v1/admin/applications/default/actors/alice/allocation", json=body)
        again = await http.post("/v1/admin/applications/default/actors/alice/allocation", json=body)
    assert (created.status_code, again.status_code) == (502, 409)  # the key may exist: unknown, and held
    assert await ledger(store) == [("p-1", "started")]


ALLOCATION = {"id": "alloc_1", "status": "active", "currency": "usd", "allocated_usd": "5"}
UNUSABLE_ALLOCATIONS: dict[str, dict[str, Any]] = {
    "an error in a 2xx": {"error": "overloaded"},
    "id null": ALLOCATION | {"id": None},
    "id a number": ALLOCATION | {"id": 7},
    "status null": ALLOCATION | {"status": None},
    "currency an object": ALLOCATION | {"currency": {"code": "usd"}},
}


@pytest.mark.parametrize("answer", UNUSABLE_ALLOCATIONS)
async def test_an_allocation_answer_without_its_required_values_is_a_provider_error(answer: str) -> None:
    client = BatteriesClient(BASE, "t", client=answering(UNUSABLE_ALLOCATIONS[answer]))
    with pytest.raises(ProviderError) as refused:
        await client.read_allocation("alloc_1")
    assert refused.value.status_code == 200 and not definite_refusal(refused.value)


NO_GRANT_ID = {
    "null": {"id": None},
    "empty": {"id": ""},
    "a number": {"id": 7},
    "an object": {"id": {"x": 1}},
}


@pytest.mark.parametrize("created", [*NO_GRANT_ID.values(), {"error": "x"}], ids=[*NO_GRANT_ID, "an error"])
async def test_a_grant_create_answer_without_a_usable_id_is_a_provider_error_and_nothing_is_read_back(
    created: dict[str, Any],
) -> None:
    asked: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        asked.append(f"{request.method} {request.url.path}")
        return httpx.Response(201, json=created)

    client = httpx.AsyncClient(base_url=BASE, transport=httpx.MockTransport(handle))
    with pytest.raises(ProviderError) as refused:
        await BatteriesClient(BASE, "t", client=client).create_grant("ops", USD)
    assert refused.value.status_code == 200 and not definite_refusal(refused.value)
    assert asked == ["POST /v1/grants"]  # no read-back of a grant named "None" or "7"


def stuck(item: dict[str, Any], asked: list[str | None]) -> httpx.AsyncClient:
    """A Batteries whose list always answers ``next_cursor`` ``c1``, whatever cursor it is asked with. It
    goes away after five pages, so a loop that never ends fails its test instead of hanging it."""

    def handle(request: httpx.Request) -> httpx.Response:
        asked.append(request.url.params.get("cursor"))
        if len(asked) > 5:
            raise httpx.ConnectError("the list never ended")
        return httpx.Response(200, json={"items": [item], "next_cursor": "c1"})

    return httpx.AsyncClient(base_url=BASE, transport=httpx.MockTransport(handle))


USAGE_ITEM = {"id": "u1", "status": "applied", "created_at_ms": 1760270400000}


async def test_a_grant_list_whose_cursor_does_not_advance_is_a_provider_error_not_an_endless_loop() -> None:
    asked: list[str | None] = []
    client = BatteriesClient(BASE, "t", client=stuck(GRANT, asked))
    with pytest.raises(ProviderError, match="did not advance") as refused:
        await client.list_grants()
    assert refused.value.status_code == 200 and not definite_refusal(refused.value)
    assert asked == [None, "c1"]  # the page asked with c1 answered c1: asking again would never end


async def test_a_usage_feed_whose_cursor_does_not_advance_fails_the_sync_as_an_invalid_answer(
    store: SqlStore,
) -> None:
    asked: list[str | None] = []
    worker = CostSyncWorker(BatteriesUsageSource(BASE, "t", client=stuck(USAGE_ITEM, asked)), store)
    with pytest.raises(ProviderError, match="did not advance") as refused:
        await worker.sync_once()
    assert asked == [None, "c1"]
    assert worker.source_error is refused.value and source_failure(refused.value) == "invalid_answer"
    checkpoint = await store.cost_checkpoint()
    assert checkpoint is not None and checkpoint.resume_cursor == "c1"  # the next sync asks for it again


async def test_the_rest_service_answers_a_list_that_never_advances_with_502(
    make_engine: EngineFactory,
) -> None:
    """``GET /v1/admin/grants`` and ``POST /v1/admin/costs/sync`` asked for the same page without end and
    never answered."""
    desk = BatteriesClient(BASE, "t", client=stuck(GRANT, []))
    usage = BatteriesUsageSource(BASE, "t", client=stuck(USAGE_ITEM, []))
    engine = await make_engine(supply=EngineSupply(grants_manager=desk, usage_source=usage))
    key = await engine.keys.bootstrap_admin()
    async with rest(engine, key.token) as http:
        listed = await http.get("/v1/admin/grants")
        synced = await http.post("/v1/admin/costs/sync")
    assert (listed.status_code, synced.status_code) == (502, 502)


async def test_a_resumed_grant_create_over_a_list_that_never_advances_is_an_unknown_outcome(
    make_engine: EngineFactory, admin: ActorContext, clock: FixedClock
) -> None:
    batteries = GrantDesk()
    client = httpx.AsyncClient(base_url=BASE, transport=httpx.MockTransport(batteries.handle))
    engine = await make_engine(supply=EngineSupply(grants_manager=BatteriesClient(BASE, "t", client=client)))
    desk = engine.payments.grants
    assert desk is not None
    usd = Money(amount=Decimal(5), currency="USD")
    with pytest.raises(ProviderUnavailable):
        await desk.create_grant(admin, "ops", usd, operation_ref="grant-1")  # created; the answer is lost

    clock.advance(120)
    batteries.list_answers = [{"items": [], "next_cursor": "c1"}] * 5  # then the real list
    with pytest.raises(ProviderError, match="did not advance") as refused:
        await desk.create_grant(admin, "ops", usd, operation_ref="grant-1")
    assert not definite_refusal(refused.value) and len(batteries.list_answers) == 3  # two pages asked
    assert len(batteries.grants) == 1  # never "no such grant"
    with pytest.raises(OperationInProgress):  # the reference is still held
        await desk.create_grant(admin, "ops", usd, operation_ref="grant-1")


@pytest.mark.parametrize("status", [301, 302, 307, 308])
async def test_a_redirected_usage_page_reads_as_unavailable(status: int) -> None:
    source = BatteriesUsageSource(
        BASE, "t", client=answering({"items": [], "next_cursor": ""}, status=status)
    )
    with pytest.raises(ProviderError) as refused:
        await source.page(None)
    assert source_failure(refused.value) == "unavailable"  # as a redirect from the discovery source (C7)
