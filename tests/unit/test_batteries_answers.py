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
from livepeer_builder.adapters.batteries import BatteriesClient
from livepeer_builder.contracts import Money
from livepeer_builder.errors import ProviderError
from livepeer_builder.http import create_app
from livepeer_builder.services.ledger import definite_refusal
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
