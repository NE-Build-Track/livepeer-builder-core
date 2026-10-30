"""Adapter edge cases: Batteries routes and errors, network error classification, discovery parsing, secret stores."""

import asyncio
import json
import logging
import re
import sys
from collections.abc import AsyncIterator
from decimal import Decimal

import httpx
import pytest

from livepeer_builder import generate_secret_key
from livepeer_builder.adapters.batteries import (
    BatteriesClient,
    BatteriesUsageSource,
    allowance_from_body,
    error_message,
    usage_row_from_item,
)
from livepeer_builder.adapters.runner_call import network_kind
from livepeer_builder.adapters.sdk import HttpDiscoverySource, runners_from_entries
from livepeer_builder.adapters.secrets import EncryptedDbSecretStore, RoutingSecretStore, SettingsSecretStore
from livepeer_builder.contracts import Money, NetworkRate, SecretRef
from livepeer_builder.contracts.discovery import PRICE_RULE
from livepeer_builder.errors import (
    InvalidRequest,
    NotFound,
    ProviderError,
    ProviderUnavailable,
)
from livepeer_builder.services.discovery import DiscoveryService
from livepeer_builder.services.policy import DefaultPolicy
from livepeer_builder.store import SqlStore
from livepeer_builder.testing import FixedClock
from livepeer_builder.utils import loads_json_numbers

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


def discovery_serving(body: bytes) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://discovery:8939/discovery"
        assert "authorization" not in request.headers  # no signer, no credentials
        return httpx.Response(200, content=body)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_http_discovery_reads_only_the_discovery_url_and_parses_entries() -> None:
    body = b'[{"address":"https://o","runners":[{"url":"https://r","app":"a/b","price_info":"bad"},"junk"]}]'
    async with discovery_serving(body) as client:
        (runner,) = await HttpDiscoverySource("http://discovery:8939/discovery", client=client).fetch()
    assert runner.rate is None and runner.orchestrator_url == "https://o"


async def test_http_discovery_keeps_every_digit_of_a_node_price_usd() -> None:
    body = (
        b'[{"address":"https://o","runners":[{"url":"https://r","app":"a/b","price_info":'
        b'{"price":4492197255,"price_usd":0.0000027777777777777777777777777777,'
        b'"currency":"wei","unit":"seconds"}}]}]'
    )
    async with discovery_serving(body) as client:
        (runner,) = await HttpDiscoverySource("http://discovery:8939/discovery", client=client).fetch()
    assert runner.rate is not None
    assert runner.rate.price_usd == Decimal("0.0000027777777777777777777777777777")
    assert runner.rate.amount == Decimal(4492197255)


def discovery_streaming(chunks: AsyncIterator[bytes]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=chunks)))


async def test_http_discovery_gives_up_on_a_slow_body_at_its_deadline() -> None:
    """httpx's timeout bounds each read, not the request: a byte every 10 ms never trips it (#77 review)."""

    async def drip() -> AsyncIterator[bytes]:
        for _ in range(300):  # 3 s in all
            yield b" "
            await asyncio.sleep(0.01)

    async with discovery_streaming(drip()) as client:
        source = HttpDiscoverySource("http://discovery:8939/discovery", client=client, timeout_s=0.1)
        with pytest.raises(
            TimeoutError, match=re.escape("no inventory from http://discovery:8939/discovery within 0.1s")
        ):
            await source.fetch()


@pytest.mark.parametrize(("padding", "refused"), [((16 << 20) - 2, False), (16 << 20, True)])
async def test_http_discovery_reads_at_most_16_mib(padding: int, refused: bool) -> None:
    """A discovery service's combined inventory: livepeer/node's MaxBody (1 MiB) is per orchestrator, and
    about 2,000 runner records fill it (#77 review)."""
    body = b"[" + b" " * padding + b"]"  # exactly 16 MiB, or one byte over
    async with discovery_serving(body) as client:
        source = HttpDiscoverySource("http://discovery:8939/discovery", client=client)
        if refused:
            with pytest.raises(ValueError, match="inventory larger than 16777216 bytes"):
                await source.fetch()
        else:
            assert await source.fetch() == ()


@pytest.mark.parametrize(
    ("body", "error"),
    [(b"{}", "discovery response is not a JSON list"), (b"[NaN]", "NaN is not a JSON number")],
)
async def test_http_discovery_refuses_a_document_that_is_not_a_json_list(body: bytes, error: str) -> None:
    """The whole poll fails, so the last good snapshot stays; NaN and Infinity are not JSON."""
    async with discovery_serving(body) as client:
        with pytest.raises(ValueError, match=error):
            await HttpDiscoverySource("http://discovery:8939/discovery", client=client).fetch()


GOOD_ENTRY = {"address": "https://good", "runners": [{"url": "https://good/apps/r1/app", "app": "a/b"}]}


def test_a_malformed_entry_or_record_is_skipped_counted_and_loses_nothing_else(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """livepeer/node's Decode skips a bad entry and keeps the rest; one bad entry must not stale discovery."""
    entries = [
        None,  # an entry that is not an object
        {"address": "https://bad", "runners": 5},  # runners that is not a list ...
        {"address": "https://bad", "runners": "r1"},
        {"address": "https://bad", "runners": {"url": "https://bad/apps/r1/app", "app": "a/b"}},
        {"address": "https://none", "runners": None},  # a Go nil slice: no runners, nothing wrong
        {"address": "https://good", "runners": [None, 7, {"app": "a/b"}, GOOD_ENTRY["runners"][0]]},  # type: ignore[index]
    ]
    with caplog.at_level(logging.WARNING, logger="livepeer_builder.adapters.sdk"):
        (runner,) = runners_from_entries(entries)
    [record] = [
        item for item in caplog.records if getattr(item, "event", "") == "discovery.inventory_skipped"
    ]
    assert (record.entries, record.runners) == (4, 3)  # two non-object records and one with no url
    assert runner.url == "https://good/apps/r1/app"


def test_a_clean_inventory_logs_no_skip(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="livepeer_builder.adapters.sdk"):
        runners_from_entries([GOOD_ENTRY, {"address": "https://none", "runners": None}])
    assert not [
        item for item in caplog.records if getattr(item, "event", "") == "discovery.inventory_skipped"
    ]


async def test_a_redirect_from_the_discovery_url_is_a_failed_refresh(clock: FixedClock) -> None:
    """Never followed, as lpb discovery's prober and livepeer/node do: the last good snapshot stays."""
    seen: list[str] = []
    answers = [httpx.Response(200, content=json.dumps([GOOD_ENTRY]).encode()),
               httpx.Response(301, headers={"Location": "http://elsewhere/discovery"})]  # fmt: skip

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return answers.pop(0)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = HttpDiscoverySource("http://discovery:8939/discovery", client=client)
        discovery = DiscoveryService(source, DefaultPolicy(), clock)
        await discovery.refresh()
        snapshot = await discovery.refresh()
    assert [runner.url for runner in snapshot.runners] == ["https://good/apps/r1/app"]
    assert snapshot.last_error is not None and "301" in snapshot.last_error
    assert seen == ["http://discovery:8939/discovery"] * 2


async def test_an_injected_client_that_follows_redirects_does_not_follow_the_discovery_url_s() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.host == "elsewhere":
            return httpx.Response(200, content=json.dumps([GOOD_ENTRY]).encode())
        return httpx.Response(301, headers={"Location": "http://elsewhere/discovery"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
        source = HttpDiscoverySource("http://discovery:8939/discovery", client=client)
        with pytest.raises(httpx.HTTPStatusError, match="301"):
            await source.fetch()
    assert seen == ["http://discovery:8939/discovery"]


async def test_a_number_decimal_cannot_build_fails_one_runner_not_the_refresh() -> None:
    """livepeer-node's discovery cache passes price_usd through as received: one such token must not stop every
    orchestrator from updating (#77 review)."""
    huge, tiny = "1e" + "9" * 20, "1e-" + "9" * 20
    body = (
        '[{"address":"https://a","runners":['
        '{"url":"https://a/apps/good/app","app":"a/b","price_info":{"price":10,"currency":"wei","unit":"fixed"}},'
        '{"url":"https://a/apps/usd/app","app":"a/b","price_info":{"price":10,"price_usd":' + huge + ","
        '"currency":"wei","unit":"fixed"}}]},'
        '{"address":"https://b","runners":['
        '{"url":"https://b/apps/r1/app","app":"a/b","price_info":{"price":'
        + tiny
        + ',"currency":"wei","unit":"fixed"}}]}]'
    ).encode()
    async with discovery_serving(body) as client:
        good, bad_usd, bad_price = await HttpDiscoverySource(
            "http://discovery:8939/discovery", client=client
        ).fetch()
    wei = NetworkRate(amount=Decimal(10), currency="wei", unit="fixed")
    assert (good.rate, good.rate_error) == (wei, None)
    assert (bad_usd.rate, bad_usd.rate_error) == (wei, "price_usd is not a nonnegative number")
    assert (bad_price.rate, bad_price.rate_error) == (None, PRICE_RULE)


def test_an_integer_past_the_digit_limit_fails_one_runner_not_the_refresh() -> None:
    """``json.loads`` raises ValueError past ``sys.get_int_max_str_digits()`` (4300). One such price must not
    stop the rest of the inventory from updating."""
    huge = "9" * (sys.get_int_max_str_digits() + 1)
    body = (
        '[{"address":"https://a","runners":['
        '{"url":"https://a/apps/good/app","app":"a/b","price_info":{"price":10,"currency":"wei","unit":"fixed"}},'
        '{"url":"https://a/apps/big/app","app":"a/b","price_info":{"price":'
        + huge
        + ',"currency":"wei","unit":"fixed"}}]}]'
    )
    good, bad = runners_from_entries(loads_json_numbers(body))
    assert (good.rate, good.rate_error) == (
        NetworkRate(amount=Decimal(10), currency="wei", unit="fixed"),
        None,
    )
    assert (bad.rate, bad.rate_error) == (None, PRICE_RULE)


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
