"""Adapters: the Batteries wire format, SDK error mapping, secret stores, and settings."""

import json
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from livepeer_gateway.errors import LivepeerHTTPError

from livepeer_builder.adapters import sdk
from livepeer_builder.adapters.batteries import BatteriesClient, BatteriesUsageSource, allowance_from_body
from livepeer_builder.adapters.sdk import SdkRunnerTransport, call_error, runners_from_entries
from livepeer_builder.adapters.secrets import EncryptedDbSecretStore, RoutingSecretStore, SettingsSecretStore
from livepeer_builder.config import EngineSettings
from livepeer_builder.contracts import JobRequest, Money, Runner, SecretRef
from livepeer_builder.errors import (
    CursorMismatch,
    InvalidRequest,
    ProviderError,
    ProviderUnavailable,
    RunnerCallError,
)
from livepeer_builder.store import SqlStore
from livepeer_builder.testing import FixedClock


def mock_client(handler: httpx.MockTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url="https://batteries.test", transport=handler)


async def test_create_key_for_grant_is_one_idempotent_call() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json={"allocation_id": "alloc_1", "id": "key_1", "api_key": "lpg_secret"})

    client = BatteriesClient("https://batteries.test", "t", client=mock_client(httpx.MockTransport(handle)))
    key = await client.create_key_for_grant(
        "grant_1", "app/alice", Money(amount=Decimal("2.5"), currency="USD"), "idem-1"
    )
    assert (key.allocation_id, key.api_key_id, key.secret.get_secret_value()) == (
        "alloc_1",
        "key_1",
        "lpg_secret",
    )
    assert "lpg_secret" not in repr(key)  # SecretStr keeps it out of logs
    body = json.loads(seen[0].content)
    assert seen[0].url.path == "/v1/api-keys"
    assert body == {"grant_id": "grant_1", "name": "app/alice", "amount_usd": "2.5"}
    assert seen[0].headers["Idempotency-Key"] == "idem-1"


async def test_list_grants_keeps_grant_objects_only() -> None:
    def listed(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "grant_1",
                        "name": "ops",
                        "status": "active",
                        "currency": "usd",
                        "total_units": "10",
                    },
                    "skip",
                ]
            },
        )

    client = BatteriesClient("https://batteries.test", "t", client=mock_client(httpx.MockTransport(listed)))
    grants = await client.list_grants()
    assert [(grant.id, grant.name, grant.currency, grant.total) for grant in grants] == [
        ("grant_1", "ops", "USD", "10")
    ]

    def empty(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    client = BatteriesClient("https://batteries.test", "t", client=mock_client(httpx.MockTransport(empty)))
    assert await client.list_grants() == []


async def test_create_grant_returns_the_response_record() -> None:
    def handle(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201,
            json={"id": "grant_9", "name": "ops", "status": "active", "currency": "usd", "total_units": "10"},
        )

    client = BatteriesClient("https://batteries.test", "t", client=mock_client(httpx.MockTransport(handle)))
    grant = await client.create_grant("ops", Money(amount=Decimal("2.5"), currency="USD"))
    assert (grant.id, grant.name, grant.status, grant.currency, grant.total) == (
        "grant_9",
        "ops",
        "active",
        "USD",
        "10",
    )

    def missing_id(_: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"name": "ops"})

    client = BatteriesClient(
        "https://batteries.test", "t", client=mock_client(httpx.MockTransport(missing_id))
    )
    with pytest.raises(ProviderError, match="missing id") as error:
        await client.create_grant("ops", Money(amount=Decimal("1"), currency="USD"))
    assert error.value.status_code == 200


async def test_provider_errors_and_outages_are_distinct() -> None:
    def conflict(_: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"error": "idempotency key reused"})

    client = BatteriesClient("https://batteries.test", "t", client=mock_client(httpx.MockTransport(conflict)))
    with pytest.raises(ProviderError, match="idempotency key reused") as error:
        await client.revoke_key("key_1")
    assert error.value.status_code == 409

    def down(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    client = BatteriesClient("https://batteries.test", "t", client=mock_client(httpx.MockTransport(down)))
    with pytest.raises(ProviderUnavailable):
        await client.read_allocation("alloc_1")


def test_allowance_follows_the_allocation_currency() -> None:
    allowance = allowance_from_body(
        {
            "id": "a1",
            "status": "active",
            "currency": "usd",
            "allocated_usd": "10",
            "spent_usd": "2.5",
            "available_usd": "7.5",
        }
    )
    assert allowance.spent == Money(amount=Decimal("2.5"), currency="USD")
    assert allowance.available == Money(amount=Decimal("7.5"), currency="USD")


async def test_usage_pages_and_cursor_mismatch() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("cursor") == "bad":
            return httpx.Response(400, json={"error": "invalid cursor"})
        item = {
            "id": "u1", "event_id": "e1", "status": "applied", "allocation_id": "a1", "payment_session_id": "s1",
            "manifest_id": "m1", "computed_fee_eth": "0.000000039", "computed_fee_usd": "0.0001", "created_at_ms": 1760270400000,
        }  # fmt: skip
        return httpx.Response(200, json={"items": [item], "next_cursor": ""})

    source = BatteriesUsageSource(
        "https://batteries.test",
        "t",
        filters={"grant_id": "g"},
        client=mock_client(httpx.MockTransport(handle)),
    )
    page = await source.page(None)
    assert page.next_cursor == "" and page.items[0].computed_fee_eth == Decimal("0.000000039")
    with pytest.raises(CursorMismatch):
        await source.page("bad")


def test_sdk_errors_keep_payment_evidence() -> None:
    runner_url = "https://runner.example/run"
    refused = LivepeerHTTPError(503, runner_url, body="busy")
    mapped = call_error(refused, runner_url, payment_sent=False)
    assert (mapped.kind, mapped.payment_sent, mapped.status_code) == ("refused", False, 503)

    signer = "https://signer.example/generate-live-payment"
    params = LivepeerHTTPError(400, signer, body="incomplete params")
    assert call_error(params, runner_url, payment_sent=False).kind == "payment_refused"  # fail over
    ceiling = LivepeerHTTPError(481, signer)
    assert call_error(ceiling, runner_url, payment_sent=False).kind == "payment_refused"
    exhausted = LivepeerHTTPError(402, signer)
    assert call_error(exhausted, runner_url, payment_sent=False).kind == "payment"  # caller-side: no failover

    after_signing = call_error(
        LivepeerHTTPError(400, signer), runner_url, payment_sent=True, manifest_ids=("m-7",)
    )
    assert (after_signing.kind, after_signing.payment_sent, after_signing.manifest_ids) == (
        "payment",
        True,
        ("m-7",),
    )


class _Session:
    def __init__(self, manifest_id: str) -> None:
        self.manifest_id = manifest_id


def _runner() -> Runner:
    return Runner(
        url="https://runner.example", app="a/b", runner_id="r1", mode="single-shot",
        orchestrator_url="https://orch.example", capacity=1, capacity_available=1, rate=None,
    )  # fmt: skip


async def test_transport_collects_signed_sessions(monkeypatch: pytest.MonkeyPatch) -> None:
    async def paid_twice(url: str, **kwargs: object) -> SimpleNamespace:
        callback = kwargs["on_payment_session"]
        callback(_Session("m-1"))  # type: ignore[operator]
        callback(_Session("m-2"))  # type: ignore[operator]  # the runner answered 402 again
        callback(_Session("m-2"))  # type: ignore[operator]  # a second session, same manifest
        return SimpleNamespace(
            content=b'{"ok": 1}', data={}, content_type="application/json", session_id="m-1"
        )

    monkeypatch.setattr(sdk, "call_runner", paid_twice)
    reply = await SdkRunnerTransport("https://signer.example").call(_runner(), JobRequest(app="a/b"), "lpg_k")
    assert (reply.manifest_ids, reply.payment_sent) == (("m-1", "m-2", "m-2"), True)


async def test_transport_failures_carry_the_sessions_signed_before_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def signed_then_500(url: str, **kwargs: object) -> SimpleNamespace:
        kwargs["on_payment_session"](_Session("m-9"))  # type: ignore[operator]
        raise LivepeerHTTPError(500, url, body="boom")

    monkeypatch.setattr(sdk, "call_runner", signed_then_500)
    with pytest.raises(RunnerCallError) as failed:
        await SdkRunnerTransport("https://signer.example").call(
            _runner(), JobRequest(app="a/b", path="/x"), "lpg_k"
        )
    assert (failed.value.kind, failed.value.payment_sent, failed.value.manifest_ids) == (
        "http",
        True,
        ("m-9",),
    )

    async def refused_by_signer(url: str, **kwargs: object) -> SimpleNamespace:
        raise LivepeerHTTPError(400, "https://signer.example/generate-live-payment", body="incomplete params")

    monkeypatch.setattr(sdk, "call_runner", refused_by_signer)
    with pytest.raises(RunnerCallError) as refused:
        await SdkRunnerTransport("https://signer.example").call(_runner(), JobRequest(app="a/b"), "lpg_k")
    assert (refused.value.kind, refused.value.payment_sent) == ("payment_refused", False)


async def test_unpaid_calls_register_no_callback(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    async def free(url: str, **kwargs: object) -> SimpleNamespace:
        seen.update(kwargs)
        return SimpleNamespace(content=b"{}", data={}, content_type="application/json", session_id="")

    monkeypatch.setattr(sdk, "call_runner", free)
    reply = await SdkRunnerTransport(None).call(_runner(), JobRequest(app="a/b"), None)
    assert seen["on_payment_session"] is None and reply.payment_sent is False


def test_discovery_entries_become_runners() -> None:
    entries = [
        {
            "address": "https://orch.example",
            "runners": [
                {"url": "https://r1", "app": "a/b", "mode": "single-shot", "capacity": 2, "session_ids": ["x"],
                 "price_info": {"price": "0.5", "currency": "USD", "unit": "Request"}},
                {"app": "missing-url"},
            ],
        }
    ]  # fmt: skip
    (only,) = runners_from_entries(entries)
    assert (only.capacity_available, only.rate and only.rate.unit) == (1, "request")


async def test_secret_stores(store: SqlStore, clock: FixedClock) -> None:
    configured = SettingsSecretStore({"shared": "lpg_shared"})
    assert await configured.get(configured.ref("shared")) == "lpg_shared"
    with pytest.raises(InvalidRequest):
        await configured.put("x", "y")

    key = EncryptedDbSecretStore.generate_key()
    database = EncryptedDbSecretStore(store, key, clock)
    ref = await database.put("lpg/default/alice", "lpg_alice")
    assert await database.get(ref) == "lpg_alice"
    with pytest.raises(InvalidRequest, match="decrypted"):
        await EncryptedDbSecretStore(store, EncryptedDbSecretStore.generate_key(), clock).get(ref)

    routing = RoutingSecretStore(configured, database)
    assert await routing.get(SecretRef(ref="settings:shared")) == "lpg_shared"
    assert await routing.get(ref) == "lpg_alice"
    with pytest.raises(InvalidRequest, match="LPB_SECRET_KEY"):
        await RoutingSecretStore(configured, None).put("x", "y")


def test_settings_from_environment() -> None:
    settings = EngineSettings.from_env(
        {
            "LPB_ALLOCATION_MODE": "per_application",
            "LPB_MAX_CONCURRENT_JOBS": "3",
            "LPB_SHARED_PAYMENT_KEY": "lpg_x",
        }
    )
    assert settings.allocation_mode == "per_application" and settings.max_concurrent_jobs == 3
    assert settings.shared_payment_key is not None and "lpg_x" not in repr(settings)
    assert settings.database_url.startswith("sqlite+aiosqlite:///")  # durable default, never in-memory
