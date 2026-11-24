"""#158, "Batteries answers": answers of the management API that became a 500, or were read loosely.

The hardening tickets (#113, #114) gave the usage feed and the two JSON readers one answer for a
provider's bad answer: a ``ProviderError`` with the answer's own status. These are the management
calls and readers they did not reach.
"""

from collections.abc import AsyncIterator, Awaitable, Callable
from decimal import Decimal

import httpx
import pytest

from livepeer_builder import BuilderEngine, EngineSupply
from livepeer_builder.adapters.batteries import BatteriesClient, BatteriesUsageSource, error_message
from livepeer_builder.contracts import ActorContext, AllotRequest, Money
from livepeer_builder.errors import ProviderError
from livepeer_builder.http import create_app
from livepeer_builder.services.ledger import definite_refusal
from livepeer_builder.utils import BODY_LIMIT
from tests.conftest import EngineFactory

USD = Money(amount=Decimal("2.5"), currency="USD")
BASE = "https://batteries.test"
CALLS: dict[str, Callable[[BatteriesClient], Awaitable[object]]] = {
    "create_key_for_grant": lambda client: client.create_key_for_grant("g1", "alice", USD, "idem-1"),
    "create_key": lambda client: client.create_key("a1", "rotated", "idem-2"),
    "top_up_allocation": lambda client: client.top_up_allocation("a1", USD, "idem-3"),
    "top_up_grant": lambda client: client.top_up_grant("g1", USD, "idem-4"),
    "set_allocation_status": lambda client: client.set_allocation_status("a1", "paused"),
    "revoke_allocation": lambda client: client.revoke_allocation("a1"),
    "revoke_key": lambda client: client.revoke_key("k1"),
    "read_allocation": lambda client: client.read_allocation("a1"),
    "list_grants": lambda client: client.list_grants(),
    "grant_page": lambda client: client.grant_page(None),
    "create_grant": lambda client: client.create_grant("ops", USD),
    "read_grant": lambda client: client.read_grant("g1"),
    "set_grant_status": lambda client: client.set_grant_status("g1", "paused"),
}


def batteries(handle: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=BASE, transport=httpx.MockTransport(handle))


def rest(engine: BuilderEngine, token: str) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(
        app=create_app(engine, manage_lifecycle=False), raise_app_exceptions=False
    )
    headers = {"Authorization": f"Bearer {token}"}
    return httpx.AsyncClient(transport=transport, base_url="http://engine", headers=headers)


class NotGzip(httpx.AsyncByteStream):
    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b"this is not gzip"


def undecodable(status: int) -> httpx.AsyncClient:
    """A Batteries that says gzip over a body that is not. A body reads once, so each request builds one."""

    def handle(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(status, headers={"Content-Encoding": "gzip"}, stream=NotGzip())

    return batteries(handle)


@pytest.mark.parametrize("call", CALLS)
@pytest.mark.parametrize("status", [200, 201, 400, 503])
async def test_a_management_answer_whose_content_encoding_does_not_decode_is_a_provider_error(
    call: str, status: int
) -> None:
    """httpx's ``DecodingError`` is no ``TransportError``: it left the client as it was, a 500. The usage
    feed and the discovery source had the rule already (#113, C7): it answered, and its status says what
    that was."""
    client = BatteriesClient(BASE, "t", client=undecodable(status))
    with pytest.raises(ProviderError, match="did not decode") as refused:
        await CALLS[call](client)
    assert refused.value.status_code == status
    assert definite_refusal(refused.value) is (status == 400)  # a 2xx or a 5xx keeps the reference held


async def test_the_rest_service_answers_an_undecodable_management_answer_with_502(
    make_engine: EngineFactory,
) -> None:
    desk = BatteriesClient(BASE, "t", client=undecodable(200))
    engine = await make_engine(supply=EngineSupply(grants_manager=desk))
    key = await engine.keys.bootstrap_admin()
    async with rest(engine, key.token) as http:
        listed = await http.get("/v1/clearinghouse/tenants")
    assert listed.status_code == 502
    assert "did not decode" in listed.json()["error"]


DEEP = "[" * 100_000 + "]" * 100_000
DEEP_METADATA = {
    "a deep list": DEEP,
    "a deep value under the engine's own key": '{"lpb_operation": ' + DEEP + "}",
}


@pytest.mark.parametrize("metadata", DEEP_METADATA.values(), ids=DEEP_METADATA)
async def test_a_grant_whose_metadata_is_json_nested_past_the_decoder_is_a_grant_the_engine_did_not_tag(
    metadata: str,
) -> None:
    """``metadata`` is the creator's own text. Reading it raised ``RecursionError``, a 500 from every grant
    list; text that is not JSON already read as no tag, and so does a number past the digit limit."""
    grant = {"id": "g1", "name": "ops", "status": "active", "currency": "usd", "total_usd": "10"}
    tagged = grant | {"id": "g2", "metadata": '{"lpb_operation": "k1"}'}

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/grants/g1":
            return httpx.Response(200, json=grant | {"metadata": metadata})
        return httpx.Response(200, json={"items": [grant | {"metadata": metadata}, tagged]})

    client = BatteriesClient(BASE, "t", client=batteries(handle))
    listed = await client.list_grants()
    assert [(item.id, item.metadata) for item in listed] == [("g1", {}), ("g2", {"lpb_operation": "k1"})]
    page = await client.grant_page(None)
    assert [item.metadata for item in page.grants] == [{}, {"lpb_operation": "k1"}]


ALLOCATION = {
    "id": "a1", "status": "active", "currency": "usd",
    "allocated_usd": "5", "spent_usd": "1.25", "available_usd": "3.75",
}  # fmt: skip
NOT_FINITE = ["NaN", "sNaN", "Infinity", "-Infinity"]


def allocation_desk(allocation: dict[str, object]) -> httpx.AsyncClient:
    """A Batteries that issues one key and reads its allocation back as ``allocation``."""

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(201, json={"id": "k1", "allocation_id": "a1", "api_key": "lpg_k1_x"})
        return httpx.Response(200, json=allocation)

    return batteries(handle)


@pytest.mark.parametrize("amount", NOT_FINITE)
@pytest.mark.parametrize("field", ["allocated_usd", "spent_usd", "available_usd"])
async def test_an_allocation_read_with_an_amount_that_is_no_finite_number_is_a_provider_error(
    field: str, amount: str
) -> None:
    """``Money`` takes no ``NaN``: its pydantic ``ValidationError`` left the adapter, a 500. A grant's
    total that is no finite number was a ``ProviderError`` already (#114, C21)."""
    client = BatteriesClient(BASE, "t", client=allocation_desk(ALLOCATION | {field: amount}))
    with pytest.raises(ProviderError, match=f"no usable {field}") as refused:
        await client.read_allocation("a1")
    assert refused.value.status_code == 200 and not definite_refusal(refused.value)
    assert amount not in str(refused.value)  # the body is not quoted


async def test_an_allocation_read_still_gives_each_amount_and_none_for_one_batteries_did_not_state() -> None:
    stated = await BatteriesClient(BASE, "t", client=allocation_desk(ALLOCATION)).read_allocation("a1")
    assert (stated.granted, stated.spent, stated.available) == (
        Money(amount=Decimal(5), currency="USD"),
        Money(amount=Decimal("1.25"), currency="USD"),
        Money(amount=Decimal("3.75"), currency="USD"),
    )
    unstated = ALLOCATION | {"allocated_usd": None, "spent_usd": "", "available_usd": "[error]"}
    unknown = await BatteriesClient(BASE, "t", client=allocation_desk(unstated)).read_allocation("a1")
    assert (unknown.granted, unknown.spent, unknown.available) == (None, None, None)  # unknown, not zero


async def test_the_rest_service_answers_an_allowance_that_is_no_finite_number_with_502(
    make_engine: EngineFactory, admin: ActorContext
) -> None:
    desk = BatteriesClient(BASE, "t", client=allocation_desk(ALLOCATION | {"spent_usd": "NaN"}))
    engine = await make_engine(supply=EngineSupply(allocation_manager=desk))
    request = AllotRequest(operation_ref="allot-alice", tenant_id="default", actor_id="alice", amount=USD)
    await engine.payments.allot(admin, request)
    key = await engine.keys.bootstrap_admin()
    async with rest(engine, key.token) as http:
        allowance = await http.get("/v1/allowance", params={"actor_id": "alice"})
        reconciled = await http.get(
            "/v1/costs/reconciliation", params={"tenant_id": "default", "actor_id": "alice"}
        )
    assert (allowance.status_code, reconciled.status_code) == (502, 502)


LONG_ERRORS = {
    "text that is not JSON": ("<html>" + "x" * 100_000).encode(),
    "an error text": b'{"error": "' + b"y" * 100_000 + b'"}',
    "JSON with no error text": b"[" + b'"z",' * 30_000 + b'"z"]',
}


@pytest.mark.parametrize("body", LONG_ERRORS.values(), ids=LONG_ERRORS)
@pytest.mark.parametrize("status", [400, 502])
async def test_a_provider_s_error_text_is_kept_up_to_the_limit_an_error_body_has(
    body: bytes, status: int
) -> None:
    """The whole body was the error's text, whatever its size: it went into the 502 a caller reads and
    into the log. A runner's and a signer's error body were cut at ``BODY_LIMIT`` already."""

    def handle(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(status, content=body)

    text = error_message(httpx.Response(status, content=body))
    assert len(text) == BODY_LIMIT and text[:1] in ("<", "y", "[")  # its start, as the provider wrote it
    with pytest.raises(ProviderError) as refused:
        await BatteriesClient(BASE, "t", client=batteries(handle)).top_up_grant("g1", USD, "idem-1")
    assert (refused.value.status_code, refused.value.message) == (status, text)
    with pytest.raises(ProviderError) as failed:
        await BatteriesUsageSource(BASE, "t", client=batteries(handle)).page(None)
    assert (failed.value.status_code, failed.value.message) == (status, text)


def test_a_provider_s_short_error_text_is_kept_whole() -> None:
    assert error_message(httpx.Response(409, json={"error": "idempotency key reused"})) == (
        "idempotency key reused"
    )
    assert error_message(httpx.Response(503, text="x" * BODY_LIMIT)) == "x" * BODY_LIMIT


async def test_the_rest_service_s_502_for_a_provider_error_is_bounded(make_engine: EngineFactory) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(500, content=b"x" * 1_000_000)

    desk = BatteriesClient(BASE, "t", client=batteries(handle))
    engine = await make_engine(supply=EngineSupply(grants_manager=desk))
    key = await engine.keys.bootstrap_admin()
    async with rest(engine, key.token) as http:
        listed = await http.get("/v1/clearinghouse/tenants")
    assert listed.status_code == 502
    assert len(listed.json()["error"]) < BODY_LIMIT + 100  # the status prefix and the text
