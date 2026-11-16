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
from livepeer_builder.adapters.discovery_inventory import read_inventory
from livepeer_builder.adapters.runner_call import network_kind
from livepeer_builder.adapters.sdk import HttpDiscoverySource
from livepeer_builder.adapters.secrets import EncryptedDbSecretStore, RoutingSecretStore, SettingsSecretStore
from livepeer_builder.contracts import Money, SecretRef
from livepeer_builder.contracts.payments import payable_rate
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


async def test_owned_clients_close_and_an_empty_body_is_a_provider_error() -> None:
    """Batteries answers every management route with a JSON object (v0.0.3, ``managementResult``), so a
    2xx with no body is an answer the engine cannot read (#114, C21), where it used to pass as ``{}``."""
    client = BatteriesClient("https://b", "t")
    source = BatteriesUsageSource("https://b", "t")
    await client.aclose()
    await source.aclose()
    quiet = BatteriesClient(
        "https://b",
        "t",
        client=httpx.AsyncClient(
            base_url="https://b", transport=httpx.MockTransport(lambda r: httpx.Response(204))
        ),
    )
    with pytest.raises(ProviderError, match="not a JSON object") as unread:
        await quiet.revoke_key("k")
    assert unread.value.status_code == 204


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
    body = (
        b'[{"address":"https://orch.example","runners":['
        b'{"url":"https://orch.example/app","app":"a/b","observed_at":"2026-10-22T18:05:00Z"},'
        b'"junk"]}]'
    )
    async with discovery_serving(body) as client:
        (runner,) = await HttpDiscoverySource("http://discovery:8939/discovery", client=client).fetch()
    assert runner.price_info is None and runner.orchestrator_url == "https://orch.example"


async def test_http_discovery_keeps_every_digit_of_a_node_price_usd() -> None:
    body = (
        b'[{"address":"https://orch.example","runners":[{"url":"https://orch.example/app","app":"a/b",'
        b'"observed_at":"2026-10-22T18:05:00Z","price_info":'
        b'{"price":4492197255,"price_usd":0.0000027777777777777777777777777777,'
        b'"currency":"wei","unit":"seconds"}}]}]'
    )
    async with discovery_serving(body) as client:
        (runner,) = await HttpDiscoverySource("http://discovery:8939/discovery", client=client).fetch()
    assert runner.price_info is not None
    assert runner.price_info.price_usd == Decimal("0.0000027777777777777777777777777777")
    assert runner.price_info.price == Decimal(4492197255)


async def test_http_discovery_keeps_one_client_across_polls_and_closes_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A client per poll opened a new connection every LPB_DISCOVERY_INTERVAL_S (#77 review)."""
    made: list[httpx.AsyncClient] = []
    real = httpx.AsyncClient

    def client(**kwargs: object) -> httpx.AsyncClient:
        made.append(real(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"[]"))))
        return made[-1]

    monkeypatch.setattr(httpx, "AsyncClient", client)
    source = HttpDiscoverySource("http://discovery:8939/discovery")
    assert await source.fetch() == await source.fetch() == ()
    await source.aclose()
    assert len(made) == 1 and made[0].is_closed


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


def discovery_failures(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.error for r in caplog.records if getattr(r, "event", "") == "discovery.failed"]  # type: ignore[attr-defined]


async def test_an_httpx_timeout_says_which_inventory_did_not_arrive(
    clock: FixedClock, caplog: pytest.LogCaptureFixture
) -> None:
    """httpx's ReadTimeout is not a TimeoutError and its text is empty: the log said nothing. /readyz shows
    the code, and the log the text (#113, C7)."""

    def silent(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(silent)) as client:
        source = HttpDiscoverySource("http://discovery:8939/discovery", client=client)
        with caplog.at_level(logging.WARNING, logger="livepeer_builder.services.discovery"):
            snapshot = await DiscoveryService(source, DefaultPolicy(), clock).refresh()
    assert snapshot.last_error == "unavailable"
    assert discovery_failures(caplog) == ["no inventory from http://discovery:8939/discovery within 5s"]


@pytest.mark.parametrize(
    ("body", "error"),
    [(b"{}", "discovery response is not a JSON list"), (b"[NaN]", "NaN is not a JSON number")],
)
async def test_http_discovery_refuses_a_document_that_is_not_a_json_list(body: bytes, error: str) -> None:
    """The whole poll fails, so the last good snapshot stays; NaN and Infinity are not JSON."""
    async with discovery_serving(body) as client:
        with pytest.raises(ValueError, match=error):
            await HttpDiscoverySource("http://discovery:8939/discovery", client=client).fetch()


_STAMP = "2026-10-22T18:05:00.123+01:00"
GOOD_ENTRY = {
    "address": "https://orch.example",
    "runners": [
        {
            "url": "https://orch.example/apps/r1/app",
            "app": "a/b",
            "observed_at": _STAMP,
        }
    ],
}


def test_a_malformed_entry_or_record_is_skipped_counted_and_loses_nothing_else(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A record the reader cannot use is skipped. One bad record must not drop the rest."""
    entries = [
        None,
        {"address": "https://bad.example", "runners": 5},
        {"address": "https://bad.example", "runners": "r1"},
        {"address": "https://bad.example", "runners": {"url": "https://bad.example/app"}},
        {"address": "https://none.example", "runners": None},
        {"address": "https://orch.example", "runners": [None, 7, {"app": "a/b"}, GOOD_ENTRY["runners"][0]]},
    ]
    with caplog.at_level(logging.WARNING, logger="livepeer_builder.adapters.discovery_inventory"):
        (runner,) = read_inventory(json.dumps(entries))
    [record] = [
        item for item in caplog.records if getattr(item, "event", "") == "discovery.inventory_skipped"
    ]
    assert (record.entries, record.runners) == (5, 3)
    assert runner.url == "https://orch.example/apps/r1/app"


def test_a_clean_inventory_logs_no_skip(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="livepeer_builder.adapters.discovery_inventory"):
        read_inventory(json.dumps([GOOD_ENTRY, {"nodes": []}]))
    assert not [
        item for item in caplog.records if getattr(item, "event", "") == "discovery.inventory_skipped"
    ]


async def test_a_redirect_from_the_discovery_url_is_a_failed_refresh(
    clock: FixedClock, caplog: pytest.LogCaptureFixture
) -> None:
    """Never followed, as livepeer/node's collector does: the last good snapshot stays."""
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
        with caplog.at_level(logging.WARNING, logger="livepeer_builder.services.discovery"):
            snapshot = await discovery.refresh()
    assert [runner.url for runner in snapshot.runners] == ["https://orch.example/apps/r1/app"]
    assert snapshot.last_error == "unavailable"
    [logged] = discovery_failures(caplog)
    assert "301" in logged
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


def _stamped(url: str, price: str) -> str:
    return (
        '{"url":"' + url + '","app":"a/b","observed_at":"' + _STAMP + '",'
        '"price_info":{"price":' + price + ',"currency":"wei","unit":"fixed"}}'
    )


async def test_a_number_decimal_cannot_build_keeps_the_runner() -> None:
    """A number token ``Decimal`` cannot hold still counts as a number. That runner is kept and cannot be paid."""
    huge, tiny = "1e" + "9" * 20, "1e-" + "9" * 20
    body = (
        '[{"address":"https://a.example","runners":['
        + _stamped("https://a.example/apps/good/app", "10")
        + ","
        + _stamped("https://a.example/apps/usd/app", "10").replace(
            '"unit":"fixed"', '"price_usd":' + huge + ',"unit":"fixed"'
        )
        + ']},{"address":"https://b.example","runners":['
        + _stamped("https://b.example/apps/r1/app", tiny)
        + "]}]"
    ).encode()
    async with discovery_serving(body) as client:
        good, huge_usd, tiny_price = await HttpDiscoverySource(
            "http://discovery:8939/discovery", client=client
        ).fetch()
    assert good.price_info is not None and good.price_info.price == Decimal(10)
    assert huge_usd.price_info is not None and huge_usd.price_info.price == Decimal(10)
    assert huge_usd.price_info.price_usd is None  # the token is a number Decimal cannot hold
    assert tiny_price.price_info is not None and payable_rate(tiny_price.price_info) is None


def test_an_integer_past_the_digit_limit_keeps_both_runners() -> None:
    """The token is still a JSON number past ``sys.get_int_max_str_digits()``. It does not fail the refresh."""
    huge = "9" * (sys.get_int_max_str_digits() + 1)
    body = (
        '[{"address":"https://a.example","runners":['
        + _stamped("https://a.example/apps/good/app", "10")
        + ","
        + _stamped("https://a.example/apps/big/app", huge).replace(
            '"observed_at"', '"capacity":' + huge + ',"observed_at"'
        )
        + "]}]"
    )
    good, big = read_inventory(body)
    assert good.price_info is not None and good.price_info.price == Decimal(10)
    assert big.price_info is not None and payable_rate(big.price_info) is None
    assert big.capacity is None


def test_a_price_string_is_absent_and_a_negative_price_is_kept_unpayable() -> None:
    body = (
        '[{"address":"https://orch.example","runners":['
        + '{"url":"https://orch.example/r1","app":"a","observed_at":"'
        + _STAMP
        + '","price_info":{"price":"not-a-number"}},'
        + _stamped("https://orch.example/r2", "-1")
        + "]}]"
    )
    string_price, negative = read_inventory(body)
    assert string_price.url.endswith("/r1")
    assert string_price.price_info is not None and string_price.price_info.price is None
    assert negative.url.endswith("/r2") and payable_rate(negative.price_info) is None


def test_a_non_http_url_is_skipped_and_a_non_integer_capacity_is_absent() -> None:
    """``1.5`` is a JSON number and not an integer capacity. The runner stays, with that field absent."""
    kept = {**GOOD_ENTRY["runners"][0], "capacity": 1.5}
    body = json.dumps(
        [
            {"address": "ftp://orch.example", "runners": [GOOD_ENTRY["runners"][0]]},
            {"address": "http://[::1", "runners": [GOOD_ENTRY["runners"][0]]},
            {
                "address": "https://orch.example",
                "runners": [
                    {"url": "ftp://orch.example/app", "app": "a/b", "observed_at": _STAMP},
                    {"url": "https://orch.example/empty", "app": "", "observed_at": _STAMP},
                    kept,
                ],
            },
        ]
    )
    (runner,) = read_inventory(body)
    assert runner.url == kept["url"] and runner.capacity is None


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
