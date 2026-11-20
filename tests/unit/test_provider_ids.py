"""#113 C10: ids in the payment provider's URLs.

A grant id went unescaped into ``POST /v1/grants/{grant_id}/fund``, and httpx removes dot segments:
``../allocations/a1`` funded allocation ``a1`` instead of a grant. Every id the Batteries adapter places in
a path is now percent-encoded by one helper, whatever its source, and a grant id is an id type that refuses
``/``, ``\\``, ``?``, ``#``, ``%``, control characters and the values ``.`` and ``..``, within the
200-character cap. It is not held to a pattern.

Since #165 a tenant's id is its grant's id, and no request names a grant: the id arrives as the tenant in
the path of a grant route (the grant read, its top-up, the tenant's status), which is that id type. One a
provider path cannot hold is a 422 there, before the operation ledger records anything.
"""

from collections.abc import AsyncIterator, Awaitable, Callable
from decimal import Decimal
from typing import Any

import httpx
import pytest
from pydantic import TypeAdapter, ValidationError

from livepeer_builder.adapters.batteries import BatteriesClient
from livepeer_builder.contracts import AllotRequest, Money, TopUpRequest
from livepeer_builder.contracts.common import GrantId
from livepeer_builder.http import create_app
from livepeer_builder.http.bodies import AllotBody
from livepeer_builder.testing import FakeAllocationManager
from tests.conftest import EngineFactory

USD = Money(amount=Decimal(5), currency="USD")
GRANT_ID = TypeAdapter(GrantId)
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
    " ..",  # whitespace around it is removed first, so these are "." and ".." too
    ".. ",
    "\u00a0..",
    "\u2028..",
    "\t..\n",
    " .",
    " a/b ",
    "g" * 201,
    " " + "g" * 201,
]
PADDED = [
    (" grant_1", "grant_1"),
    ("grant_1\n", "grant_1"),
    ("\u00a0grant_1\u2028", "grant_1"),
    ("   ", ""),  # nothing is left: no such tenant
    (" " * 50 + "g" * 200, "g" * 200),  # the limit is on what is kept
]
ALLOWED = ["grant_1", "550e8400-e29b-41d4-a716-446655440000", "grant.v2", "...", ".hidden", "g" * 200]


@pytest.mark.parametrize("grant_id", REFUSED)
def test_a_grant_id_a_provider_path_cannot_hold_is_refused(grant_id: str) -> None:
    with pytest.raises(ValidationError):
        GRANT_ID.validate_python(grant_id)


@pytest.mark.parametrize("grant_id", ALLOWED)
def test_any_other_grant_id_is_kept_as_written(grant_id: str) -> None:
    """No pattern such as a UUID: the provider chooses its ids."""
    assert GRANT_ID.validate_python(grant_id) == grant_id


@pytest.mark.parametrize(("written", "used"), PADDED)
def test_whitespace_around_a_grant_id_is_removed_where_the_request_is_read(written: str, used: str) -> None:
    """The value checked is the value the engine uses: it stripped the id only after the check."""
    assert GRANT_ID.validate_python(written) == used


@pytest.mark.parametrize("grant_id", ["grant_1", "", "../allocations/a1"])
def test_no_request_names_a_grant_apart_from_its_tenant(grant_id: str) -> None:
    """A tenant's id is its grant's id (D83, #165). A request that still names a grant is refused where
    it is read, whatever the grant: the field is never ignored while money moves."""
    with pytest.raises(ValidationError, match="grant_id"):
        TopUpRequest(amount=USD, operation_ref="f1", grant_id=grant_id)  # type: ignore[call-arg]
    with pytest.raises(ValidationError, match="grant_id"):
        AllotRequest(  # type: ignore[call-arg]
            tenant_id="default", actor_id="alice", amount=USD, grant_id=grant_id, operation_ref="p1"
        )
    with pytest.raises(ValidationError, match="grant_id"):
        AllotBody(amount=USD, grant_id=grant_id, operation_ref="p1")  # type: ignore[call-arg]


@pytest.fixture
async def admin_client(
    make_engine: EngineFactory, manager: FakeAllocationManager
) -> AsyncIterator[tuple[httpx.AsyncClient, dict[str, str]]]:
    manager.add_grant("grant_1")
    engine = await make_engine()
    admin = await engine.keys.bootstrap_admin()
    app = create_app(engine, manage_lifecycle=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://engine") as client:
        yield client, {"Authorization": f"Bearer {admin.token}"}


TENANT = "/v1/clearinghouse/tenants/{}"
ALLOCATION = "/v1/clearinghouse/tenants/default/actors/alice/allocation"
AMOUNT = {"amount": "5", "currency": "USD"}


# Each as it travels in a URL: the path is decoded before the route reads the tenant from it.
@pytest.mark.parametrize(
    "written", ["%2E%2E", "%2E", "a%3Fb", "a%23b", "100%25", "a%5Cb", "%20..", "..%C2%A0", "%20.", "a%0Ab"]
)
async def test_rest_refuses_such_a_tenant_id_with_422_before_the_ledger_and_the_provider(
    admin_client: tuple[httpx.AsyncClient, dict[str, str]], manager: FakeAllocationManager, written: str
) -> None:
    """Each reference is still free afterwards: the ledger recorded nothing."""
    client, admin = admin_client
    commands = [
        ("POST", "/grant/top-up", {"operation_ref": "g1", "amount": AMOUNT}, 204),
        ("POST", "/status", {"operation_ref": "s1", "status": "paused"}, 200),
        ("GET", "/grant", None, 200),
    ]
    for method, route, body, accepted in commands:
        calls = list(manager.calls)
        refused = await client.request(method, TENANT.format(written) + route, json=body, headers=admin)
        assert refused.status_code == 422, (route, refused.text)
        assert refused.json()["detail"][0]["loc"] == ["path", "tenant_id"]
        assert manager.calls == calls
        retried = await client.request(method, TENANT.format("grant_1") + route, json=body, headers=admin)
        assert retried.status_code == accepted, (route, retried.text)
        assert "Idempotent-Replayed" not in retried.headers


async def test_rest_uses_a_padded_tenant_id_without_its_padding(
    admin_client: tuple[httpx.AsyncClient, dict[str, str]], manager: FakeAllocationManager
) -> None:
    client, admin = admin_client
    body = {"operation_ref": "g1", "amount": AMOUNT}
    funded = await client.post(TENANT.format("%20grant_1%0A") + "/grant/top-up", json=body, headers=admin)
    assert funded.status_code == 204, funded.text
    assert [grant for grant, _, _ in manager.topped_up_grants] == ["grant_1"]
    read = await client.get(TENANT.format("%C2%A0grant_1%20") + "/grant", headers=admin)
    assert (read.status_code, read.json()["tenant_id"]) == (200, "grant_1")


async def test_rest_refuses_a_body_that_still_names_a_grant(
    admin_client: tuple[httpx.AsyncClient, dict[str, str]], manager: FakeAllocationManager
) -> None:
    """Ignored, the field would let a caller believe it chose the grant the money came from."""
    client, admin = admin_client
    commands = [
        (TENANT.format("grant_1") + "/grant/top-up", {"operation_ref": "g1", "amount": AMOUNT}, 204),
        (ALLOCATION, {"operation_ref": "p1", "amount": AMOUNT}, 201),
        (ALLOCATION + "/top-up", {"operation_ref": "f1", "amount": AMOUNT}, 200),
    ]
    for path, body, accepted in commands:
        calls = list(manager.calls)
        refused = await client.post(path, json=body | {"grant_id": "grant_1"}, headers=admin)
        assert refused.status_code == 422, (path, refused.text)
        assert refused.json()["detail"][0]["loc"] == ["body", "grant_id"]
        assert manager.calls == calls
        retried = await client.post(path, json=body, headers=admin)
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
    ("/v1/grants/{}/fund", lambda c, i: c.top_up_grant(i, USD, "k")),
    ("/v1/grants/{}/status", lambda c, i: c.set_grant_status(i, "paused")),
    ("/v1/allocations/{}/fund", lambda c, i: c.top_up_allocation(i, USD, "k")),
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
