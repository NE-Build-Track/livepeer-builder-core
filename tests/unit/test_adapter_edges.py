"""Adapter edge cases: Batteries routes and errors, SDK error classification and parsing, secret stores."""

from decimal import Decimal
from types import SimpleNamespace

import aiohttp
import httpx
import pytest
from livepeer_gateway.errors import PaymentError

from livepeer_builder import generate_secret_key
from livepeer_builder.adapters.batteries import (
    BatteriesClient,
    BatteriesUsageSource,
    allowance_from_body,
    error_message,
    usage_row_from_item,
)
from livepeer_builder.adapters.sdk import (
    SdkRunnerTransport,
    SignerDiscoverySource,
    call_error,
    network_kind,
    runners_from_entries,
)
from livepeer_builder.adapters.secrets import EncryptedDbSecretStore, RoutingSecretStore, SettingsSecretStore
from livepeer_builder.contracts import JobRequest, Money, NetworkRate, Runner, SecretRef
from livepeer_builder.errors import (
    InvalidRequest,
    NotFound,
    ProviderError,
    ProviderUnavailable,
    RunnerCallError,
)
from livepeer_builder.store import SqlStore
from livepeer_builder.testing import FixedClock

# -- Batteries -----------------------------------------------------------------------------------------


def recording(
    status: int = 200, body: object | None = None
) -> tuple[list[httpx.Request], httpx.MockTransport]:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json=body if body is not None else {})

    return seen, httpx.MockTransport(handle)


async def test_management_writes_use_the_documented_routes() -> None:
    seen, transport = recording(200, {"allocation_id": "a1", "id": "k2", "api_key": "lpg_new"})
    client = BatteriesClient(
        "https://b", "t", client=httpx.AsyncClient(base_url="https://b", transport=transport)
    )
    key = await client.create_key("a1", "rotated", "idem-r")
    await client.fund_allocation("a1", Money(amount=Decimal("0.5"), currency="ETH"), "idem-f")
    await client.fund_grant("g1", Money(amount=Decimal("2.5"), currency="USD"), "idem-g")
    await client.set_allocation_status("a1", "paused")
    await client.revoke_allocation("a1")
    await client.revoke_key("k1")
    await client.aclose()  # a client passed in is not ours to close
    assert key.api_key_id == "k2"
    assert [(r.method, r.url.path) for r in seen] == [
        ("POST", "/v1/api-keys"),
        ("POST", "/v1/allocations/a1/fund"),
        ("POST", "/v1/grants/g1/fund"),
        ("PATCH", "/v1/allocations/a1/status"),
        ("POST", "/v1/allocations/a1/revoke"),
        ("POST", "/v1/api-keys/k1/revoke"),
    ]
    assert seen[1].content == b'{"amount_eth":"0.5"}' and seen[1].headers["Idempotency-Key"] == "idem-f"
    assert seen[2].content == b'{"amount_usd":"2.5"}' and seen[2].headers["Idempotency-Key"] == "idem-g"
    assert "Idempotency-Key" not in seen[3].headers  # status changes are naturally repeatable


async def test_owned_clients_close_and_empty_bodies_are_fine() -> None:
    client = BatteriesClient("https://b", "t")
    source = BatteriesUsageSource("https://b", "t")
    await client.aclose()
    await source.aclose()
    _, transport = recording(204)
    quiet = BatteriesClient(
        "https://b", "t", client=httpx.AsyncClient(base_url="https://b", transport=transport)
    )
    quiet._client = httpx.AsyncClient(
        base_url="https://b", transport=httpx.MockTransport(lambda r: httpx.Response(204))
    )
    await quiet.revoke_key("k")


async def test_usage_feed_errors() -> None:
    def down(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    source = BatteriesUsageSource(
        "https://b", "t", client=httpx.AsyncClient(base_url="https://b", transport=httpx.MockTransport(down))
    )
    with pytest.raises(ProviderUnavailable):
        await source.page(None)

    _, forbidden = recording(403, {"error": "forbidden"})
    source = BatteriesUsageSource(
        "https://b", "t", client=httpx.AsyncClient(base_url="https://b", transport=forbidden)
    )
    with pytest.raises(ProviderError) as error:
        await source.page(None)
    assert error.value.status_code == 403
    with pytest.raises(ProviderError, match="unknown usage status"):
        usage_row_from_item({"id": "1", "status": "mystery", "created_at_ms": 0})


def test_wire_format_edges() -> None:
    assert error_message(httpx.Response(500, text="plain text failure")) == "plain text failure"
    assert error_message(httpx.Response(500, json=["not", "an", "object"])) == '["not","an","object"]'
    eth = allowance_from_body(
        {"id": "a", "status": "active", "currency": "eth", "spent_eth": "[error]", "available_eth": "x"}
    )
    assert (eth.spent, eth.available, eth.granted) == (
        None,
        None,
        None,
    )  # unreadable amounts are unknown, not zero


# -- SDK -----------------------------------------------------------------------------------------------


async def test_signer_discovery_parses_entries() -> None:
    async def fake_discover(**kwargs: object) -> list[dict[str, object]]:
        assert kwargs["signer_headers"] == {"Authorization": "Bearer k"}
        return [
            {
                "address": "https://o",
                "runners": [{"url": "https://r", "app": "a/b", "price_info": "bad"}, "junk"],
            }
        ]

    source = SignerDiscoverySource(
        signer_url="https://s", signer_headers={"Authorization": "Bearer k"}, discover=fake_discover
    )
    (runner,) = await source.fetch()
    assert runner.rate is None and runner.orchestrator_url == "https://o"


def test_rates_reject_bad_prices() -> None:
    entries = [
        {"address": "https://o", "runners": [
            {"url": "https://r1", "app": "a", "price_info": {"price": "not-a-number"}},
            {"url": "https://r2", "app": "a", "price_info": {"price": "-1"}},
            {"url": "https://r3", "app": "a", "price_info": {"price": "NaN"}},
        ]},
    ]  # fmt: skip
    assert [r.rate for r in runners_from_entries(entries)] == [None, None, None]


def test_network_errors_are_classified_through_their_causes() -> None:
    timeout = RuntimeError("wrapped")
    timeout.__cause__ = TimeoutError()
    refused = RuntimeError("wrapped")
    refused.__cause__ = ConnectionRefusedError()
    assert (network_kind(timeout), network_kind(refused), network_kind(RuntimeError("other"))) == (
        "timeout",
        "unreachable",
        "http",
    )
    assert call_error(PaymentError("signer said no"), "https://r", payment_sent=False).kind == "payment"


async def test_transport_maps_client_errors_and_passes_rates() -> None:
    runner = Runner(
        url="https://r", app="a", runner_id=None, mode=None, orchestrator_url=None, capacity=None,
        capacity_available=None, rate=NetworkRate(amount=Decimal("0.1"), currency="usd", unit="request"),
    )  # fmt: skip
    seen: dict[str, object] = {}

    async def unreachable(url: str, **kwargs: object) -> SimpleNamespace:
        seen.update(kwargs)
        raise aiohttp.ClientConnectionError("no route")

    with pytest.raises(RunnerCallError) as failed:
        await SdkRunnerTransport("https://signer", call=unreachable).call(
            runner, JobRequest(app="a"), "lpg_k"
        )
    assert (failed.value.kind, failed.value.payment_sent) == ("unreachable", False)
    assert seen["runner"].price_info.price == "0.1"  # type: ignore[attr-defined]


# -- secret stores ---------------------------------------------------------------------------------------


async def test_secret_store_edges(store: SqlStore, clock: FixedClock) -> None:
    configured = SettingsSecretStore({"k": "v"})
    with pytest.raises(NotFound):
        configured.ref("missing")
    with pytest.raises(NotFound):
        await configured.get(SecretRef(ref="db:other"))
    await configured.delete(configured.ref("k"))  # configuration owns it: a no-op
    assert await configured.get(configured.ref("k")) == "v"

    database = EncryptedDbSecretStore(store, generate_secret_key(), clock)
    with pytest.raises(NotFound):
        await database.get(SecretRef(ref="db:nope"))

    no_key = RoutingSecretStore(configured, None)
    with pytest.raises(InvalidRequest, match="LPB_SECRET_KEY"):
        await no_key.get(SecretRef(ref="db:x"))
    await no_key.delete(SecretRef(ref="db:x"))  # nothing to delete without a database store

    routed = RoutingSecretStore(configured, database)
    ref = await routed.put("n", "secret")
    await routed.delete(ref)
    with pytest.raises(NotFound):
        await routed.get(ref)
