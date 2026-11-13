"""#113 C10: ids in the payment provider's URLs.

``FundRequest.grant_id`` went unescaped into ``POST /v1/grants/{grant_id}/fund``, and httpx removes dot
segments: ``../allocations/a1`` funded allocation ``a1`` instead of a grant. Every id the Batteries adapter
places in a path is now percent-encoded by one helper, whatever its source, and ``grant_id`` is an id type
that refuses ``/``, ``\\``, ``?``, ``#``, ``%``, control characters and the values ``.`` and ``..``, within
the 200-character cap: a 422 before the operation ledger records anything. It is not held to a pattern.
"""

from collections.abc import AsyncIterator, Awaitable, Callable
from decimal import Decimal
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from livepeer_builder.adapters.batteries import BatteriesClient
from livepeer_builder.contracts import FundRequest, Money, ProvisionRequest
from livepeer_builder.http import create_app
from livepeer_builder.http.bodies import ProvisionBody
from livepeer_builder.testing import FakeAllocationManager
from tests.conftest import EngineFactory

USD = Money(amount=Decimal(5), currency="USD")
REFUSED = [
    "../allocations/a1",
    "a/b",
    "a\\b",
    "a?b",
    "a#b",
    "a%2Fb",
    "100%",
    "a\x00b",
    "a\nb",
    "a\x7fb",
    "a\x85b",
    ".",
    "..",
    "g" * 201,
]
ALLOWED = ["", "grant_1", "550e8400-e29b-41d4-a716-446655440000", "grant.v2", "...", ".hidden", "g" * 200]


@pytest.mark.parametrize("grant_id", REFUSED)
def test_a_grant_id_a_provider_path_cannot_hold_is_refused(grant_id: str) -> None:
    with pytest.raises(ValidationError, match="grant_id"):
        FundRequest(amount=USD, operation_ref="f1", grant_id=grant_id)
    with pytest.raises(ValidationError, match="grant_id"):
        ProvisionRequest(
            application_id="default", actor_id="alice", amount=USD, grant_id=grant_id, operation_ref="p1"
        )
    with pytest.raises(ValidationError, match="grant_id"):
        ProvisionBody(amount=USD, grant_id=grant_id, operation_ref="p1")


@pytest.mark.parametrize("grant_id", ALLOWED)
def test_any_other_grant_id_is_kept_as_written(grant_id: str) -> None:
    """No pattern such as a UUID: the provider chooses its ids."""
    assert FundRequest(amount=USD, operation_ref="f1", grant_id=grant_id).grant_id == grant_id
    request = ProvisionRequest(
        application_id="d", actor_id="a", amount=USD, grant_id=grant_id, operation_ref="p1"
    )
    assert request.grant_id == grant_id


@pytest.fixture
async def admin_client(make_engine: EngineFactory) -> AsyncIterator[tuple[httpx.AsyncClient, dict[str, str]]]:
    engine = await make_engine()
    admin = await engine.keys.bootstrap_admin()
    app = create_app(engine, manage_lifecycle=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://engine") as client:
        yield client, {"Authorization": f"Bearer {admin.token}"}


ALLOCATION = "/v1/admin/applications/default/actors/alice/allocation"
AMOUNT = {"amount": "5", "currency": "USD"}


@pytest.mark.parametrize("grant_id", ["../allocations/a1", "a/b", "..", "a%2F"])
async def test_rest_refuses_such_a_grant_id_with_422_before_the_ledger_and_the_provider(
    admin_client: tuple[httpx.AsyncClient, dict[str, str]], manager: FakeAllocationManager, grant_id: str
) -> None:
    """Each reference is still free afterwards: the ledger recorded nothing."""
    client, admin = admin_client
    commands = [
        ("/v1/admin/grant/fund", {"operation_ref": "g1", "amount": AMOUNT}, 204),
        (ALLOCATION, {"operation_ref": "p1", "amount": AMOUNT}, 201),
        (ALLOCATION + "/fund", {"operation_ref": "f1", "amount": AMOUNT}, 200),
    ]
    for path, body, accepted in commands:
        calls = list(manager.calls)
        refused = await client.post(path, json=body | {"grant_id": grant_id}, headers=admin)
        assert refused.status_code == 422, (path, refused.text)
        assert refused.json()["detail"][0]["loc"] == ["body", "grant_id"]
        assert manager.calls == calls
        retried = await client.post(path, json=body | {"grant_id": "grant_1"}, headers=admin)
        assert retried.status_code == accepted, (path, retried.text)
        assert "Idempotent-Replayed" not in retried.headers


# -- the adapter -------------------------------------------------------------------------------------------


@pytest.fixture
def batteries() -> tuple[BatteriesClient, list[str]]:
    paths: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.raw_path.decode())
        if request.method == "POST" and request.url.raw_path == b"/v1/grants":
            return httpx.Response(201, json={"id": "g/1"})
        if request.method == "GET":
            return httpx.Response(200, json={"id": "g/1", "status": "active", "currency": "usd"})
        return httpx.Response(200, json={})

    client = httpx.AsyncClient(base_url="https://batteries.test", transport=httpx.MockTransport(handle))
    return BatteriesClient("https://batteries.test", "t", client=client), paths


Call = Callable[[BatteriesClient, str], Awaitable[Any]]
CALLS: list[tuple[str, Call]] = [
    ("/v1/grants/{}/fund", lambda c, i: c.fund_grant(i, USD, "k")),
    ("/v1/allocations/{}/fund", lambda c, i: c.fund_allocation(i, USD, "k")),
    ("/v1/allocations/{}/status", lambda c, i: c.set_allocation_status(i, "paused")),
    ("/v1/allocations/{}/revoke", lambda c, i: c.revoke_allocation(i)),
    ("/v1/api-keys/{}/revoke", lambda c, i: c.revoke_key(i)),
    ("/v1/allocations/{}", lambda c, i: c.read_allocation(i)),
]


@pytest.mark.parametrize(("template", "call"), CALLS, ids=[template for template, _ in CALLS])
@pytest.mark.parametrize(
    ("value", "segment"),
    [
        ("plain-id_1.v2", "plain-id_1.v2"),
        ("../allocations/a1", "..%2Fallocations%2Fa1"),
        ("a?b#c", "a%3Fb%23c"),
        ("a%2Fb", "a%252Fb"),
        ("a b\\c", "a%20b%5Cc"),
        ("..", "%2E%2E"),
        (".", "%2E"),
    ],
)
async def test_every_id_in_a_provider_path_is_one_percent_encoded_segment(
    batteries: tuple[BatteriesClient, list[str]], template: str, call: Call, value: str, segment: str
) -> None:
    client, paths = batteries
    await call(client, value)
    assert paths == [template.format(segment)]


async def test_a_created_grant_is_read_back_by_its_encoded_id(
    batteries: tuple[BatteriesClient, list[str]],
) -> None:
    client, paths = batteries
    await client.create_grant("ops", USD)
    assert paths == ["/v1/grants", "/v1/grants/g%2F1"]
