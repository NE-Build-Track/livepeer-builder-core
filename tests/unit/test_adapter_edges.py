"""Adapter edge cases: Batteries routes and errors, SDK error classification and parsing, secret stores."""

import asyncio
import json
import logging
import ssl
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import aiohttp
import httpx
import livepeer_gateway.discovery
import pytest
from livepeer_gateway.errors import LivepeerGatewayError, PaymentError
from livepeer_gateway.remote_signer import RemoteSignerError
from livepeer_gateway.selection import _runner_candidates_from_discovery

from livepeer_builder.adapters import sdk
from livepeer_builder.adapters.batteries import (
    BatteriesClient,
    BatteriesUsageSource,
    allowance_from_body,
    error_message,
    usage_row_from_item,
)
from livepeer_builder.adapters.sdk import (
    OrchestratorListDiscoverySource,
    SdkRunnerTransport,
    SignerDiscoverySource,
    call_error,
    network_kind,
    runners_from_entries,
)
from livepeer_builder.adapters.secrets import EncryptedDbSecretStore, RoutingSecretStore, SettingsSecretStore
from livepeer_builder.contracts import (
    DiscoveredOrchestrator,
    JobRequest,
    Money,
    NetworkRate,
    Runner,
    RunnerGPU,
    SecretRef,
)
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


async def test_signer_discovery_parses_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_discover(**kwargs: object) -> list[dict[str, object]]:
        assert kwargs["signer_headers"] == {"Authorization": "Bearer k"}
        return [
            {
                "address": "https://o",
                "runners": [{"url": "https://r", "app": "a/b", "price_info": "bad"}, "junk"],
            }
        ]

    monkeypatch.setattr(sdk, "discover_runners", fake_discover)
    source = SignerDiscoverySource(signer_url="https://s", signer_headers={"Authorization": "Bearer k"})
    (runner,) = await source.fetch()
    assert runner.rate is None and runner.orchestrator_url == "https://o"


def _write_list(tmp_path: Path, payload: object) -> str:
    path = tmp_path / "orchestrators.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def _entries(orchestrator: str, *runner_urls: str) -> list[dict[str, object]]:
    return [
        {
            "address": orchestrator,
            "runners": [
                {"url": url, "app": "a/b", "capacity": 4, "capacity_available": 4} for url in runner_urls
            ],
        }
    ]


def _untrusted(url: str) -> RemoteSignerError:
    """What ``discover_runners`` raises for a certificate the process does not trust."""
    key = SimpleNamespace(host="orch.example", port=8935, is_ssl=True, ssl=True)
    cause = aiohttp.ClientConnectorCertificateError(
        key,  # type: ignore[arg-type]
        ssl.SSLCertVerificationError(1, "certificate verify failed: self-signed certificate"),
    )
    return RemoteSignerError(url, "failed to reach endpoint", cause=cause)


def _events(caplog: pytest.LogCaptureFixture) -> list[tuple[str, str, str | None]]:
    return [
        (record.event, record.orchestrator, getattr(record, "reason", None))  # type: ignore[attr-defined]
        for record in caplog.records
        if getattr(record, "event", "").startswith("discovery.orchestrator_")
    ]


async def test_orchestrator_list_reads_a_file_and_merges_partial_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _write_list(
        tmp_path,
        [
            {"address": "https://ok.example:8935", "score": 1},
            {"address": "  "},
            {"address": ""},
            {"score": 2},
            "skip-me",
            {"address": "https://down.example:8935"},
            {"address": "https://also.example:8935"},
        ],
    )
    seen: list[dict[str, object]] = []

    async def fake_discover(**kwargs: object) -> list[dict[str, object]]:
        seen.append(kwargs)
        url = str(kwargs["discovery_url"])
        if "down.example" in url:
            raise RuntimeError("down")
        return _entries(url.removesuffix("/discovery"), url.replace("/discovery", "/runner"))

    monkeypatch.setattr(sdk, "_fetch_discovery_document", fake_discover)
    discovery = OrchestratorListDiscoverySource(source)
    runners = await discovery.fetch()
    assert [runner.url for runner in runners] == [
        "https://ok.example:8935/runner",
        "https://also.example:8935/runner",
    ]
    assert [runner.orchestrator_url for runner in runners] == [
        "https://ok.example:8935",
        "https://also.example:8935",
    ]
    # Startup capacity is stale by the next job: the total stays, what is free is unknown.
    assert [(runner.capacity, runner.capacity_available) for runner in runners] == [(4, None), (4, None)]
    # Only the discovery URL goes to an orchestrator: no signer bearer, no other headers.
    assert seen == [
        {"discovery_url": "https://ok.example:8935/discovery"},
        {"discovery_url": "https://down.example:8935/discovery"},
        {"discovery_url": "https://also.example:8935/discovery"},
    ]
    assert await discovery.fetch() == runners
    assert len(seen) == 3  # a file is probed once


@pytest.mark.parametrize(
    "inline",
    [" https://a.example:8935 , https://b.example:8935 ", "https://a.example:8935,https://b.example:8935"],
)
async def test_orchestrator_list_takes_addresses_inline(monkeypatch: pytest.MonkeyPatch, inline: str) -> None:
    seen: list[object] = []

    async def fake_discover(**kwargs: object) -> list[dict[str, object]]:
        seen.append(kwargs["discovery_url"])
        url = str(kwargs["discovery_url"])
        return _entries(url.removesuffix("/discovery"), url.replace("/discovery", "/runner"))

    monkeypatch.setattr(sdk, "_fetch_discovery_document", fake_discover)
    source = OrchestratorListDiscoverySource(inline)
    runners = await source.fetch()
    assert await source.fetch() == runners
    assert seen == ["https://a.example:8935/discovery", "https://b.example:8935/discovery"]
    assert [runner.url for runner in runners] == [
        "https://a.example:8935/runner",
        "https://b.example:8935/runner",
    ]


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ("{}", "must be a JSON list"),
        ("[]", "names no orchestrators"),
        ('[{"address": ""}, {"address": 7}]', "names no orchestrators"),
        ('[{"address": "ftp://orch.example"}]', "Invalid orchestrator URL"),
        ("not json", "orchestrator list"),
    ],
)
async def test_orchestrator_list_rejects_an_unusable_list(tmp_path: Path, payload: str, message: str) -> None:
    path = tmp_path / "orchestrators.json"
    path.write_text(payload, encoding="utf-8")
    with pytest.raises(LivepeerGatewayError, match=message):
        await OrchestratorListDiscoverySource(str(path)).fetch()


async def test_orchestrator_list_rejects_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(LivepeerGatewayError, match="orchestrator list"):
        await OrchestratorListDiscoverySource(str(tmp_path / "missing.json")).fetch()


async def test_orchestrator_list_raises_the_first_error_when_every_orchestrator_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _write_list(
        tmp_path, [{"address": "https://a.example:8935"}, {"address": "https://b.example:8935"}]
    )

    async def fake_discover(**kwargs: object) -> list[dict[str, object]]:
        if "a.example" in str(kwargs["discovery_url"]):
            await asyncio.sleep(0.01)  # the first error is the first in list order, not the first to finish
        raise RuntimeError(f"down {kwargs['discovery_url']}")

    monkeypatch.setattr(sdk, "_fetch_discovery_document", fake_discover)
    discovery = OrchestratorListDiscoverySource(source)
    with pytest.raises(RuntimeError, match=r"https://a\.example:8935/discovery"):
        await discovery.fetch()
    with pytest.raises(RuntimeError, match=r"https://a\.example:8935/discovery"):
        await discovery.fetch()  # a failed pass is not kept; the next interval probes again


async def test_orchestrator_list_does_not_cache_an_empty_merge_when_a_probe_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _write_list(
        tmp_path,
        [{"address": "https://idle.example:8935"}, {"address": "https://down.example:8935"}],
    )
    calls = 0

    async def fake_discover(**kwargs: object) -> list[dict[str, object]]:
        nonlocal calls
        calls += 1
        if "idle.example" in str(kwargs["discovery_url"]):
            return []
        raise RuntimeError(f"down {kwargs['discovery_url']}")

    monkeypatch.setattr(sdk, "_fetch_discovery_document", fake_discover)
    discovery = OrchestratorListDiscoverySource(source)
    with pytest.raises(RuntimeError, match=r"https://down\.example:8935/discovery"):
        await discovery.fetch()
    with pytest.raises(RuntimeError, match=r"https://down\.example:8935/discovery"):
        await discovery.fetch()
    assert calls == 4  # the empty merge is not cached, so the next interval probes again


async def test_orchestrator_list_keeps_an_orchestrator_with_no_runners(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _write_list(tmp_path, [{"address": "https://idle.example:8935"}])

    async def fake_discover(**kwargs: object) -> list[dict[str, object]]:
        return []

    monkeypatch.setattr(sdk, "_fetch_discovery_document", fake_discover)
    assert await OrchestratorListDiscoverySource(source).fetch() == ()


async def test_orchestrator_list_excludes_an_untrusted_certificate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    source = _write_list(
        tmp_path, [{"address": "https://trusted.example"}, {"address": "https://self.example"}]
    )
    calls = 0

    async def fake_discover(**kwargs: object) -> list[dict[str, object]]:
        nonlocal calls
        calls += 1
        url = str(kwargs["discovery_url"])
        if "self.example" in url:
            raise _untrusted(url)
        return _entries(url.removesuffix("/discovery"), url.replace("/discovery", "/runner"))

    monkeypatch.setattr(sdk, "_fetch_discovery_document", fake_discover)
    discovery = OrchestratorListDiscoverySource(source)
    caplog.set_level(logging.INFO, logger=sdk.__name__)

    (runner,) = await discovery.fetch()
    assert runner.url == "https://trusted.example/runner"
    assert await discovery.fetch() == (runner,)
    assert calls == 2  # one startup pass; the second fetch reuses it
    assert _events(caplog) == [
        ("discovery.orchestrator_excluded", "https://self.example/discovery", "untrusted_certificate")
    ]


async def test_orchestrator_list_probes_each_address_once_and_each_runner_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _write_list(
        tmp_path,
        [
            {"address": "https://a.example"},
            {"address": "https://a.example/"},
            {"address": "https://b.example"},
        ],
    )
    probed: list[str] = []

    async def fake_discover(**kwargs: object) -> list[dict[str, object]]:
        url = str(kwargs["discovery_url"])
        probed.append(url)
        return _entries(url.removesuffix("/discovery"), "https://shared.example/runner")

    monkeypatch.setattr(sdk, "_fetch_discovery_document", fake_discover)
    (runner,) = await OrchestratorListDiscoverySource(source).fetch()
    assert probed == ["https://a.example/discovery", "https://b.example/discovery"]
    assert runner.orchestrator_url == "https://a.example"  # the first in list order


async def test_orchestrator_list_bounds_concurrent_probes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _write_list(tmp_path, [{"address": f"https://o{index}.example"} for index in range(20)])
    running = 0
    peak = 0

    async def fake_discover(**kwargs: object) -> list[dict[str, object]]:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.01)
        running -= 1
        return []

    monkeypatch.setattr(sdk, "_fetch_discovery_document", fake_discover)
    await OrchestratorListDiscoverySource(source).fetch()
    assert peak == sdk._PROBE_CONCURRENCY


async def test_orchestrator_list_does_not_follow_a_discovery_redirect(tmp_path: Path) -> None:
    followed = False

    async def secret(_request: aiohttp.web.Request) -> aiohttp.web.Response:
        nonlocal followed
        followed = True
        return aiohttp.web.Response(text="internal")

    async def discovery(_request: aiohttp.web.Request) -> aiohttp.web.Response:
        raise aiohttp.web.HTTPFound("/secret")

    app = aiohttp.web.Application()
    app.router.add_get("/discovery", discovery)
    app.router.add_get("/secret", secret)
    runner = aiohttp.web.AppRunner(app)
    await runner.setup()
    await aiohttp.web.TCPSite(runner, "127.0.0.1", 0).start()
    port = runner.addresses[0][1]
    try:
        source = _write_list(tmp_path, [{"address": f"http://127.0.0.1:{port}"}])
        with pytest.raises(LivepeerGatewayError, match="redirect refused"):
            await OrchestratorListDiscoverySource(source).fetch()
        assert not followed
    finally:
        await runner.cleanup()


async def test_orchestrator_list_propagates_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _write_list(tmp_path, [{"address": "https://a.example"}])

    async def fake_discover(**kwargs: object) -> list[dict[str, object]]:
        raise asyncio.CancelledError

    monkeypatch.setattr(sdk, "_fetch_discovery_document", fake_discover)
    with pytest.raises(asyncio.CancelledError):
        await OrchestratorListDiscoverySource(source).fetch()


def test_untrusted_certificate_follows_the_cause_chain() -> None:
    assert sdk._untrusted_certificate(_untrusted("https://o/discovery"))
    wrapped = LivepeerGatewayError("failed to reach endpoint")
    wrapped.__cause__ = ssl.SSLCertVerificationError(1, "hostname mismatch")
    assert sdk._untrusted_certificate(wrapped)
    loop = RuntimeError("a")
    loop.__context__ = RuntimeError("b")
    loop.__context__.__context__ = loop  # a cycle ends the walk
    assert not sdk._untrusted_certificate(loop)
    assert not sdk._untrusted_certificate(
        RemoteSignerError("https://o", "refused", cause=ConnectionRefusedError())
    )


# -- /discover-orchestrators contract (LPB_DISCOVERY_URL, the signer, and each probe) -----------------

_CATALOG = Path(__file__).resolve().parents[1] / "fixtures" / "discover-orchestrators.json"
_CATALOG_APPS = {
    "comfystream/fal-flux-schnell",
    "comfystream/sd-turbo",
    "transcode/ffmpeg",
    "transcoding-with-captions/whisper",
    "blueclaw/chat/qwen3.8-27b",
    "comfystream/ffmpeg-scale-video",
}


def _catalog() -> list[dict[str, object]]:
    payload: list[dict[str, object]] = json.loads(_CATALOG.read_text(encoding="utf-8"))
    return payload


def test_the_fixture_is_a_discover_orchestrators_document() -> None:
    discovered = [DiscoveredOrchestrator.model_validate(item) for item in _catalog()]
    assert [item.address for item in discovered] == [
        "https://ai1.eliteencoder.net:8936",
        "https://lpt.sundara.tv:8935",
        "https://hky.eliteencoder.net:8936",
    ]
    assert discovered[0].last_seen is not None  # Go's RFC3339Nano timestamp
    by_app = {runner.app: runner for runner in runners_from_entries(_catalog())}
    assert set(by_app) == _CATALOG_APPS
    flux = by_app["comfystream/fal-flux-schnell"]
    assert flux.gpu is not None and flux.gpu.name == "NVIDIA GeForce RTX 3090 Ti"
    assert flux.rate is not None and (flux.rate.amount, flux.rate.currency, flux.rate.unit) == (
        Decimal("1258792844887"),
        "wei",
        "fixed",
    )
    assert flux.capacity_available == 4  # a live catalog's capacity is kept
    qwen = by_app["blueclaw/chat/qwen3.8-27b"]
    assert qwen.metadata is not None and "Qwen3.8-27B" in qwen.metadata


async def test_a_discovery_url_reads_the_document_through_the_sdk_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real ``discover_runners`` runs; only its HTTP GET is replaced."""
    seen: list[tuple[str, object]] = []
    document = [
        *_catalog(),
        {"address": "https://not-a-list.example", "runners": {"url": "https://r", "app": "a"}},
        {"address": "https://idle.example", "runners": []},
    ]

    async def fake_get_json(url: str, *, headers: object = None, **kwargs: object) -> object:
        seen.append((url, headers))
        return document

    monkeypatch.setattr(livepeer_gateway.discovery, "get_json", fake_get_json)
    source = SignerDiscoverySource(discovery_url="https://discovery.example/discover-orchestrators")
    runners = await source.fetch()
    assert {runner.app for runner in runners} == _CATALOG_APPS
    assert seen == [("https://discovery.example/discover-orchestrators", None)]  # no signer bearer


def test_the_engine_parses_runners_as_the_sdk_does() -> None:
    """Parity with the SDK's own discovery parser (private, so only this test touches it)."""
    entries = [
        *_catalog(),
        {"address": "https://o", "runners": [{"url": "https://r", "app": "a", "price_info": {"price": 5}}]},
    ]
    ours = [
        (r.url, r.app, r.runner_id or "", r.mode or "", r.orchestrator_url or "",
         None if r.rate is None else (r.rate.amount, r.rate.currency, r.rate.unit))
        for r in runners_from_entries(entries)
    ]  # fmt: skip
    theirs = [
        (c.url, c.app, c.runner_id, c.mode, c.orchestrator_url,
         None if c.price_info is None else (Decimal(str(c.price_info.price)), c.price_info.currency, c.price_info.unit))
        for c in _runner_candidates_from_discovery(entries)
    ]  # fmt: skip
    assert ours == theirs
    assert ours[-1][-1] == (Decimal(5), "", "")  # no unit stays blank: call_runner picks its default


def test_one_bad_orchestrator_does_not_drop_the_rest(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger=sdk.__name__)
    entries = [
        {"address": "https://bad.example", "score": "high", "runners": [{"url": "https://bad/r", "app": "a"}]},
        {"address": "https://late.example", "last_seen": "yesterday", "runners": [{"url": "https://l/r", "app": "a"}]},
        *_entries("https://ok.example", "https://ok.example/runner"),
    ]  # fmt: skip
    (runner,) = runners_from_entries(entries)
    assert runner.orchestrator_url == "https://ok.example"
    invalid = [
        record.orchestrator  # type: ignore[attr-defined]
        for record in caplog.records
        if getattr(record, "event", "") == "discovery.orchestrator_invalid"
    ]
    assert invalid == ["https://bad.example", "https://late.example"]


def test_one_bad_runner_does_not_drop_its_orchestrator() -> None:
    entries = [
        {
            "address": "https://o",
            "added_by_a_newer_node": True,
            "runners": [
                {"url": "https://r1", "app": "a", "capacity": "lots"},
                {"url": "https://r2", "app": "a", "gpu": {"name": "L40S", "vram_mb": "big", "uuid": "x"}},
                {"url": "https://r3", "app": "a", "added_by_a_newer_node": 1, "gpu": "L40S"},
                {"url": "https://r4", "app": "a", "gpu": {"name": "H100", "uuid": "x"}},
            ],
        }
    ]
    runners = runners_from_entries(entries)
    assert [(runner.url, runner.gpu) for runner in runners] == [
        ("https://r2", None),  # a GPU that does not parse is left out
        ("https://r3", None),
        ("https://r4", RunnerGPU(name="H100")),  # fields the node may add later are ignored
    ]


def test_optional_runner_fields_degrade_to_unknown() -> None:
    entries = [
        {"address": "https://o", "runners": [
            {"url": "https://r1", "app": "a", "price_info": None, "session_ids": None},
            {"url": "https://r2", "app": "a", "price_info": {"price": True}, "session_ids": [1, 2]},
            {"url": "https://r3", "app": "a", "capacity": 3, "session_ids": ["s1"]},  # the older form
        ]},
    ]  # fmt: skip
    runners = runners_from_entries(entries)
    assert [(runner.rate, runner.capacity_available) for runner in runners] == [
        (None, None),
        (None, None),
        (None, 2),
    ]


@pytest.mark.parametrize("runners", [{"url": "https://r", "app": "a"}, "r", None])
def test_runners_that_are_not_a_list_are_no_runners(runners: object) -> None:
    assert runners_from_entries([{"address": "https://o", "runners": runners}]) == []


def test_a_document_with_no_valid_orchestrator_raises() -> None:
    entries = [{"address": "https://bad.example", "score": "high"}, {"address": ""}, "skip-me"]
    with pytest.raises(LivepeerGatewayError, match="no orchestrator entry matches the discovery contract"):
        runners_from_entries(entries)
    assert runners_from_entries([{"address": ""}, "skip-me"]) == []  # names none: nothing to raise about


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


async def test_transport_maps_client_errors_and_passes_rates(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = Runner(
        url="https://r", app="a", runner_id=None, mode=None, orchestrator_url=None, capacity=None,
        capacity_available=None, rate=NetworkRate(amount=Decimal("0.1"), currency="usd", unit="request"),
    )  # fmt: skip
    seen: dict[str, object] = {}

    async def unreachable(url: str, **kwargs: object) -> SimpleNamespace:
        seen.update(kwargs)
        raise aiohttp.ClientConnectionError("no route")

    monkeypatch.setattr(sdk, "call_runner", unreachable)
    with pytest.raises(RunnerCallError) as failed:
        await SdkRunnerTransport("https://signer").call(runner, JobRequest(app="a"), "lpg_k")
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

    database = EncryptedDbSecretStore(store, EncryptedDbSecretStore.generate_key(), clock)
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
