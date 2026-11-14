"""Adapters: the Batteries wire format, runner discovery, secret stores, and settings."""

import asyncio
import json
from decimal import Decimal

import httpx
import pytest
from pydantic import ValidationError

from livepeer_builder import generate_secret_key
from livepeer_builder.adapters.batteries import (
    TOKEN_HEADER,
    BatteriesClient,
    BatteriesUsageSource,
    allowance_from_body,
    batteries_http_client,
)
from livepeer_builder.adapters.discovery_inventory import read_inventory
from livepeer_builder.adapters.secrets import EncryptedDbSecretStore, RoutingSecretStore, SettingsSecretStore
from livepeer_builder.config import EngineSettings
from livepeer_builder.contracts import Money, SecretRef
from livepeer_builder.errors import (
    CursorMismatch,
    InvalidRequest,
    ProviderError,
    ProviderUnavailable,
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


async def test_list_grants_reads_each_grant_object_and_refuses_any_other_item() -> None:
    """A grant list of objects is read. An item that is not an object, or an answer with no ``items``,
    used to read as fewer grants or none; each is now a provider error (#114, C21)."""
    grant = {"id": "grant_1", "name": "ops", "status": "active", "currency": "usd", "total_usd": "10"}
    answers: list[dict[str, object]] = [{"items": [grant]}, {"items": [grant, "skip"]}, {}]

    def listed(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=answers.pop(0))

    client = BatteriesClient("https://batteries.test", "t", client=mock_client(httpx.MockTransport(listed)))
    grants = await client.list_grants()
    assert [(grant.id, grant.name, grant.currency, grant.total) for grant in grants] == [
        ("grant_1", "ops", "USD", "10")
    ]
    for _ in range(2):
        with pytest.raises(ProviderError, match="items are not a list of objects"):
            await client.list_grants()


async def test_create_grant_returns_the_response_record() -> None:
    def handle(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201,
            json={"id": "grant_9", "name": "ops", "status": "active", "currency": "usd", "total_usd": "10"},
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


async def test_a_created_grant_is_read_back_since_batteries_answers_with_its_id_only() -> None:
    """At ca09e47, ``POST /v1/grants`` answers ``{"id": ...}`` (``management.go``); the record the engine
    returns, and the ledger replays (D57), is the grant as Batteries stored it."""
    requests: list[tuple[str, str]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.method == "POST":
            return httpx.Response(201, json={"id": "grant_9"})
        stored = {"id": "grant_9", "name": "ops", "status": "active", "currency": "usd", "total_usd": "2.5"}
        return httpx.Response(200, json=stored)

    client = BatteriesClient("https://batteries.test", "t", client=mock_client(httpx.MockTransport(handle)))
    grant = await client.create_grant("ops", Money(amount=Decimal("2.5"), currency="USD"))
    assert (grant.id, grant.name, grant.status, grant.currency, grant.total) == (
        "grant_9",
        "ops",
        "active",
        "USD",
        "2.5",
    )
    assert requests == [("POST", "/v1/grants"), ("GET", "/v1/grants/grant_9")]


async def test_a_grant_created_but_not_read_back_is_an_unknown_outcome_not_a_refusal() -> None:
    """The grant exists once the create answered, so a failed read must not free the reference (a 4xx is a
    definite refusal to the ledger, D57): it is unavailable, and a resume adopts the grant by its tag."""

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(201, json={"id": "grant_9"})
        return httpx.Response(404, json={"error": "not found"})

    client = BatteriesClient("https://batteries.test", "t", client=mock_client(httpx.MockTransport(handle)))
    with pytest.raises(ProviderUnavailable, match="grant_9"):
        await client.create_grant("ops", Money(amount=Decimal("2.5"), currency="USD"))


async def test_a_grant_carries_the_engines_tag_as_batteries_metadata() -> None:
    """D57, Q5: Batteries takes string fields only, so the tag travels as a JSON object in ``metadata``
    (at ca09e47, ``managementFields`` refuses any non-string value), and grant reads parse it back."""
    sent: list[dict[str, str]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            sent.append(json.loads(request.content))
            return httpx.Response(201, json={"id": "grant_9"})
        tagged = {"id": "grant_9", "name": "ops", "metadata": '{"lpb_operation": "abc"}'}
        free_text = {"id": "grant_1", "name": "legacy", "metadata": "set up by hand"}
        if request.url.path != "/v1/grants":  # the read-back of the one grant
            return httpx.Response(200, json=tagged)
        return httpx.Response(200, json={"items": [free_text, tagged], "next_cursor": ""})

    client = BatteriesClient("https://batteries.test", "t", client=mock_client(httpx.MockTransport(handle)))
    await client.create_grant("ops", Money(amount=Decimal("2.5"), currency="USD"), {"lpb_operation": "abc"})
    assert json.loads(sent[0]["metadata"]) == {"lpb_operation": "abc"}
    assert [grant.metadata for grant in await client.list_grants()] == [{}, {"lpb_operation": "abc"}]


async def test_the_grant_list_reads_every_page() -> None:
    """Batteries pages grant lists (100 by default, at most 1000, at ca09e47); a resumed grant create must
    see every grant to find its tag."""
    seen: list[dict[str, str]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.url.params))
        if "cursor" not in request.url.params:
            return httpx.Response(200, json={"items": [{"id": "grant_1"}], "next_cursor": "c1"})
        return httpx.Response(200, json={"items": [{"id": "grant_2"}], "next_cursor": ""})

    client = BatteriesClient("https://batteries.test", "t", client=mock_client(httpx.MockTransport(handle)))
    assert [grant.id for grant in await client.list_grants()] == ["grant_1", "grant_2"]
    assert seen == [{"limit": "1000"}, {"limit": "1000", "cursor": "c1"}]


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


def test_discovery_entries_become_runners() -> None:
    body = json.dumps(
        [
            {
                "address": "https://orch.example",
                "runners": [
                    {
                        "url": "https://orch.example/r1",
                        "app": "a/b",
                        "mode": "single-shot",
                        "observed_at": "2026-10-22T18:05:00Z",
                        "capacity": 2,
                        "capacity_available": 1,
                        "price_info": {"price": 0.5, "currency": "USD", "unit": "Request"},
                    },
                    {"app": "missing-url"},
                ],
            }
        ]
    )
    (only,) = read_inventory(body)
    assert only.capacity_available == 1
    assert only.price_info is not None and (only.price_info.currency, only.price_info.unit) == (
        "USD",
        "Request",
    )


async def test_secret_stores(store: SqlStore, clock: FixedClock) -> None:
    configured = SettingsSecretStore({"shared": "lpg_shared"})
    assert await configured.get(configured.ref("shared")) == "lpg_shared"
    with pytest.raises(InvalidRequest):
        await configured.put("x", "y")

    key = generate_secret_key()
    database = EncryptedDbSecretStore(store, key, clock)
    ref = await database.put("lpg/default/alice", "lpg_alice")
    assert await database.get(ref) == "lpg_alice"
    with pytest.raises(InvalidRequest, match="decrypted"):
        await EncryptedDbSecretStore(store, generate_secret_key(), clock).get(ref)

    routing = RoutingSecretStore(configured, database)
    assert await routing.get(SecretRef(ref="settings:shared")) == "lpg_shared"
    assert await routing.get(ref) == "lpg_alice"
    with pytest.raises(InvalidRequest, match="LPB_SECRET_KEY"):
        await RoutingSecretStore(configured, None).put("x", "y")
    # #112 C2: the optional check provisioning asks before the provider is called
    assert await routing.writable() and not await RoutingSecretStore(configured, None).writable()


def test_settings_from_environment() -> None:
    settings = EngineSettings.from_env(
        {
            "LPB_DISCOVERY_URL": "http://127.0.0.1:9/discovery",
            "LPB_ALLOCATION_MODE": "per_application",
            "LPB_MAX_CONCURRENT_JOBS": "3",
            "LPB_SHARED_PAYMENT_KEY": "lpg_x",
        }
    )
    assert settings.allocation_mode == "per_application" and settings.max_concurrent_jobs == 3
    assert settings.shared_payment_key is not None and "lpg_x" not in repr(settings)
    assert settings.database_url.startswith("sqlite+aiosqlite:///")  # durable default, never in-memory
    ignored = EngineSettings.from_env(
        {"LPB_DISCOVERY_URL": "http://127.0.0.1:9/discovery", "LPB_GRANT_ID": "grant_old"}
    )
    assert "grant_id" not in ignored.model_dump()


def test_tuning_values_are_settings_with_todays_defaults() -> None:
    defaults = EngineSettings(discovery_url="http://127.0.0.1:9/discovery")
    assert (defaults.stream_open_grace_s, defaults.batteries_timeout_s, defaults.retention_interval_s) == (
        5.0,
        30.0,
        6 * 3600,
    )
    tuned = EngineSettings.from_env(
        {
            "LPB_DISCOVERY_URL": "http://127.0.0.1:9/discovery",
            "LPB_STREAM_OPEN_GRACE_S": "0.5",
            "LPB_BATTERIES_TIMEOUT_S": "7",
            "LPB_RETENTION_INTERVAL_S": "60",
        }
    )
    assert (tuned.stream_open_grace_s, tuned.batteries_timeout_s, tuned.retention_interval_s) == (
        0.5,
        7.0,
        60.0,
    )
    for field in ("stream_open_grace_s", "batteries_timeout_s", "retention_interval_s"):
        with pytest.raises(ValidationError):
            EngineSettings.model_validate({field: 0, "discovery_url": "http://127.0.0.1:9/discovery"})


def test_the_paid_attempt_cap_is_an_operator_setting() -> None:
    assert EngineSettings(discovery_url="http://127.0.0.1:9/discovery").max_paid_attempts == 2  # D55
    assert (
        EngineSettings.from_env(
            {"LPB_DISCOVERY_URL": "http://127.0.0.1:9/discovery", "LPB_MAX_PAID_ATTEMPTS": "3"}
        ).max_paid_attempts
        == 3
    )
    for value in (0, 11):
        with pytest.raises(ValidationError):
            EngineSettings.model_validate(
                {"max_paid_attempts": value, "discovery_url": "http://127.0.0.1:9/discovery"}
            )


def test_discovery_url_is_required() -> None:
    with pytest.raises(ValidationError):
        EngineSettings()


@pytest.mark.parametrize(
    "url",
    ["discovery:8939/discovery", "ftp://discovery/discovery", "http:///discovery", "http://[::1/discovery"],
)
def test_a_discovery_url_without_an_http_scheme_and_host_is_refused_at_startup(url: str) -> None:
    """The SDK used to add a scheme to host:port; httpx does not, so every poll would fail (#77 review)."""
    with pytest.raises(
        ValidationError, match="LPB_DISCOVERY_URL must be an http:// or https:// URL with a host"
    ):
        EngineSettings.from_env({"LPB_DISCOVERY_URL": url})


@pytest.mark.parametrize(
    "url",
    [
        "https://user:secret@discovery.example/discovery",
        "https://token@discovery.example/discovery",
        "//user:secret@discovery.example/discovery",
        "https://discovery.example/discovery?token=secret",
        "https://discovery.example/discovery#secret",
    ],
)
def test_a_discovery_url_with_userinfo_a_query_or_a_fragment_is_refused_at_startup(url: str) -> None:
    """A refresh error quotes the URL, /readyz serves it unauthenticated, and httpx would send the userinfo as
    Basic auth. The refusal names no part of the URL (D58, as for signer_url)."""
    with pytest.raises(ValidationError) as refused:
        EngineSettings.from_env({"LPB_DISCOVERY_URL": url})
    assert "must not carry userinfo, a query or a fragment" in str(refused.value)
    assert "secret" not in str(refused.value) and "token" not in str(refused.value)


def test_a_plain_discovery_url_is_kept() -> None:
    url = "https://discovery.example:8939/path/discovery"
    assert EngineSettings.from_env({"LPB_DISCOVERY_URL": url}).discovery_url == url


@pytest.mark.parametrize("padded", ["http://discovery:8939/discovery\n", " http://discovery:8939/discovery "])
def test_a_padded_discovery_url_is_read_without_its_padding(padded: str) -> None:
    """urlsplit ignores the padding; httpx refuses the newline and sends the space as /discovery%20 (#77 review)."""
    assert (
        EngineSettings.from_env({"LPB_DISCOVERY_URL": padded}).discovery_url
        == "http://discovery:8939/discovery"
    )


@pytest.mark.parametrize(
    "url", ["http://discovery:8939/disc overy", "http://discovery:8939/\tdiscovery", "http://disc\novery/x"]
)
def test_a_discovery_url_with_whitespace_inside_is_refused_at_startup(url: str) -> None:
    with pytest.raises(
        ValidationError, match="LPB_DISCOVERY_URL must be an http:// or https:// URL with a host"
    ):
        EngineSettings.from_env({"LPB_DISCOVERY_URL": url})


def test_an_http_or_https_discovery_url_is_accepted() -> None:
    for url in ("http://discovery:8939/discovery", "https://orch.example:8935/discovery"):
        assert EngineSettings.from_env({"LPB_DISCOVERY_URL": url}).discovery_url == url


def test_batteries_http_client_uses_the_configured_timeout() -> None:
    client = batteries_http_client("https://batteries.example/", "t", timeout_s=7.0)
    assert client.timeout == httpx.Timeout(7.0)
    assert str(client.base_url) == "https://batteries.example" and client.headers[TOKEN_HEADER] == "t"


# -- streaming (docs/contracts/engine.md#streaming) --------------------------------------------------


def test_runners_from_entries_keeps_version_metadata_and_gpu() -> None:
    entries = [
        {
            "address": "https://orch.example:8935",
            "runners": [
                {
                    "url": "https://orch.example:8935/apps/r/app",
                    "app": "blueclaw/chat/qwen3.8-27b",
                    "version": "1.2.0",
                    "metadata": '{"model_id":"Qwen3.8-27B","max_context":131072}',
                    "gpu": {"id": "0", "name": "H100", "vram_mb": 81920},
                    "mode": "single-shot",
                    "observed_at": "2026-10-22T18:05:00Z",
                    "capacity": 8,
                    "capacity_used": 3,
                    "capacity_available": 5,
                    "price_info": {"price": 4634466808, "currency": "wei", "unit": "seconds"},
                },
                {
                    "url": "https://orch.example:8935/apps/q/app",
                    "app": "x/y",
                    "observed_at": "2026-10-22T18:05:00Z",
                    "capacity": 1,
                    "capacity_used": 1,
                },
            ],
        }
    ]
    first, second = read_inventory(json.dumps(entries))
    assert first.version == "1.2.0" and first.metadata is not None and "max_context" in first.metadata
    assert first.gpu is not None and (first.gpu.name, first.gpu.vram_mb) == ("H100", 81920)
    assert first.capacity_available == 5
    assert (second.version, second.metadata, second.gpu) == (None, None, None)
    assert second.capacity == 1 and second.capacity_used == 1 and second.capacity_available is None


async def test_usage_feed_is_unfiltered_by_default() -> None:
    seen: list[dict[str, str]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.url.params))
        return httpx.Response(200, json={"items": [], "next_cursor": ""})

    source = BatteriesUsageSource(
        "https://batteries.test", "t", client=mock_client(httpx.MockTransport(handle))
    )
    await source.page(None, limit=10)
    assert seen == [{"limit": "10"}]  # iss-04: no filters, so desk-created grants still reconcile


async def test_a_batteries_call_has_one_deadline_for_the_whole_request() -> None:
    """Cursor, #76 review: httpx applies the timeout to each phase (connect, write, read, pool) in turn, so
    one call could take several times LPB_BATTERIES_TIMEOUT_S. A resume is a lease timed in those calls
    (D57), so each call is bounded as a whole, and running out is unavailable, an unknown outcome."""

    async def slow(_: httpx.Request) -> httpx.Response:
        await asyncio.sleep(1)
        return httpx.Response(200, json={"items": [], "next_cursor": ""})

    client = BatteriesClient(
        "https://batteries.test", "t", client=mock_client(httpx.MockTransport(slow)), timeout_s=0.05
    )
    with pytest.raises(ProviderUnavailable, match=r"0\.05 s"):
        await client.list_grants()
