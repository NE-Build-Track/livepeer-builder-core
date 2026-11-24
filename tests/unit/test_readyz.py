"""#113 C7: ``GET /readyz`` needs no credential, so its errors are short fixed codes, never exception text.

A cost sync that failed in the store served the store error's SQL and parameters to anyone, and a failed
discovery refresh served whatever its source raised. ``discovery_error`` and ``cost_sync_error`` now carry
``unavailable``, ``invalid_answer`` or ``store_error``; the full text goes to the log. The answer keeps its
shape, and the version, the running-job count and the allocation mode stay public.
"""

import asyncio
import dataclasses
import logging
from collections.abc import AsyncIterator, Callable, Sequence

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from livepeer_builder import BuilderEngine, EngineParts, EngineSupply
from livepeer_builder.adapters.batteries import BatteriesUsageSource
from livepeer_builder.adapters.sdk import HttpDiscoverySource
from livepeer_builder.contracts import (
    ActorContext,
    DiscoverySnapshot,
    EngineHealth,
    Runner,
    UsagePage,
    UsageRow,
)
from livepeer_builder.errors import AccessDenied, CursorMismatch, ProviderError, ProviderUnavailable
from livepeer_builder.http import create_app
from livepeer_builder.services import costs as costs_module
from livepeer_builder.services.costs import CostService, CostSyncWorker
from livepeer_builder.services.discovery import DiscoveryService
from livepeer_builder.services.policy import DefaultPolicy
from livepeer_builder.store import SqlStore
from livepeer_builder.testing import FakeUsageSource, FixedClock, make_engine

SECRET = "lpg_secret-in-a-parameter"
STORE_ERROR = IntegrityError(
    "INSERT INTO usage_rows (id, allocation_id) VALUES (?, ?)", ("u-1", SECRET), Exception("UNIQUE failed")
)


class Failing:
    def __init__(self, error: Exception) -> None:
        self.error = error

    async def fetch(self) -> Sequence[Runner]:
        raise self.error


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (RuntimeError(f"connect to http://discovery:8939/discovery?token={SECRET} failed"), "unavailable"),
        (TimeoutError("no inventory from http://discovery:8939/discovery within 5s"), "unavailable"),
        (ValueError("discovery response is not a JSON list"), "invalid_answer"),
    ],
)
async def test_a_failed_discovery_refresh_is_a_code_and_its_text_goes_to_the_log(
    clock: FixedClock, caplog: pytest.LogCaptureFixture, error: Exception, code: str
) -> None:
    discovery = DiscoveryService(Failing(error), DefaultPolicy(), clock)
    with caplog.at_level(logging.WARNING, logger="livepeer_builder.services.discovery"):
        snapshot = await discovery.refresh()
    assert snapshot.last_error == code
    [logged] = [r for r in caplog.records if getattr(r, "event", "") == "discovery.failed"]
    assert (logged.code, logged.error) == (code, str(error))  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("where", "error", "code"),
    [
        ("source", ProviderUnavailable("Batteries unreachable: [Errno -3] name resolution"), "unavailable"),
        ("source", ProviderError(401, "bad token"), "unavailable"),
        ("source", CursorMismatch("cursor does not match the filters"), "unavailable"),
        ("source", RuntimeError("a supplied usage source's own words"), "unavailable"),
        ("source", ProviderError(200, "unknown usage status 'x'"), "invalid_answer"),
        ("source", KeyError("created_at_ms"), "invalid_answer"),
        ("store", STORE_ERROR, "store_error"),
        ("store", ValueError("a row the store could not read"), "store_error"),
    ],
)
async def test_a_failed_cost_sync_is_a_code_and_its_text_goes_to_the_log(
    store: SqlStore,
    clock: FixedClock,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    where: str,
    error: Exception,
    code: str,
) -> None:
    stop = asyncio.Event()

    async def failing(*_: object, **__: object) -> UsagePage:
        stop.set()  # one pass of the loop
        raise error

    source = FakeUsageSource()
    monkeypatch.setattr(
        source if where == "source" else store, "page" if where == "source" else "apply_usage", failing
    )
    costs = CostService(store, DefaultPolicy(), clock, CostSyncWorker(source, store))
    with caplog.at_level(logging.WARNING, logger="livepeer_builder.services.costs"):
        await costs.run(stop)
    assert costs.last_error == code
    [logged] = [r for r in caplog.records if getattr(r, "event", "") == "costs.sync_failed"]
    assert (logged.code, logged.error) == (code, str(error))  # type: ignore[attr-defined]


async def test_two_syncs_that_overlap_each_report_their_own_failure(
    store: SqlStore, clock: FixedClock, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The loop and an admin's sync share one worker, and nothing keeps them apart. Which failure was the
    source's was read from a field on that worker: a second sync that failed while the first counted its
    set-aside rows replaced it, and the first was logged and shown in health as a store error (John,
    #189 review). Each run is told by the error it caught."""
    answers = [ProviderError(200, "unknown usage status 'x'"), ProviderUnavailable("Batteries unreachable")]
    counting, overtaken = asyncio.Event(), asyncio.Event()
    count, counted = costs_module.count_set_aside, 0

    async def failing(*_: object, **__: object) -> UsagePage:
        raise answers.pop(0)

    async def held_the_first_time(of: object, known: int | None) -> int | None:
        nonlocal counted
        counted += 1
        if counted == 1:
            counting.set()
            await overtaken.wait()
        return await count(of, known)

    source = FakeUsageSource()
    monkeypatch.setattr(source, "page", failing)
    monkeypatch.setattr(costs_module, "count_set_aside", held_the_first_time)
    costs = CostService(store, DefaultPolicy(), clock, CostSyncWorker(source, store))
    with caplog.at_level(logging.WARNING, logger="livepeer_builder.services.costs"):
        first = asyncio.create_task(costs.sync_once())
        await asyncio.wait_for(counting.wait(), timeout=5)
        with pytest.raises(ProviderUnavailable):
            await costs.sync_once()  # fails while the first is still counting
        overtaken.set()
        with pytest.raises(ProviderError, match="unknown usage status"):
            await asyncio.wait_for(first, timeout=5)
    codes = [r.code for r in caplog.records if getattr(r, "event", "") == "costs.sync_failed"]  # type: ignore[attr-defined]
    assert codes == ["unavailable", "invalid_answer"]  # the second ended first; neither is a store error
    assert costs.last_error == "invalid_answer"


@pytest.fixture
async def failing_engine(store: SqlStore, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[BuilderEngine]:
    def failing_discovery(parts: EngineParts) -> EngineParts:
        error = RuntimeError(f"GET http://discovery:8939/discovery?token={SECRET}: 500")
        return dataclasses.replace(parts, discovery_source=Failing(error))

    stop = asyncio.Event()

    async def store_fails(rows: Sequence[UsageRow], checkpoint: object) -> list[UsageRow]:
        stop.set()  # one pass of the cost-sync loop
        raise STORE_ERROR

    async with make_engine(supply=EngineSupply(store=store), decorate=failing_discovery) as engine:
        monkeypatch.setattr(store, "apply_usage", store_fails)
        await engine.costs.run(stop)
        yield engine


async def test_readyz_without_a_credential_serves_codes_not_exception_text(
    failing_engine: BuilderEngine,
) -> None:
    app = create_app(failing_engine, manage_lifecycle=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://engine") as client:
        answer = await client.get("/readyz")
    assert answer.status_code == 200
    health = answer.json()
    assert (health["discovery_error"], health["cost_sync_error"]) == ("unavailable", "store_error")
    for leak in (SECRET, "INSERT", "discovery:8939", "UNIQUE"):
        assert leak not in answer.text
    assert set(health) == {
        "role",  # which half of the engine this deployment runs (#169)
        "ready",
        "discovery_fresh",
        "discovery_observed_at",
        "discovery_error",
        "cost_sync_at",
        "cost_sync_error",
        "cost_sync_set_aside",  # a count (#114, C13)
        "running_jobs",
        "version",
    }  # no allocation_mode: per_actor is the only one (D82, #162)
    assert health["running_jobs"] == 0
    assert health["version"] == failing_engine.health().version


# -- the shipped adapters: the code an operator sees, read where the answer is read -------------------


class NotGzip(httpx.AsyncByteStream):
    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b"this is not gzip"


def undecodable(status_code: int) -> Callable[[], httpx.Response]:
    """An answer that says gzip over a body that is not. Its body reads once, so each request builds one."""
    return lambda: httpx.Response(status_code, headers={"Content-Encoding": "gzip"}, stream=NotGzip())


Answer = httpx.Response | Exception | Callable[[], httpx.Response]


def answering(answer: Answer) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        if isinstance(answer, Exception):
            raise answer
        return answer if isinstance(answer, httpx.Response) else answer()

    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://provider.example")


NO_ANSWER: list[tuple[Answer, str]] = [
    (httpx.Response(302, headers={"Location": "/login"}), "unavailable"),  # a redirect is no answer
    (httpx.Response(503, text="down"), "unavailable"),
    (undecodable(503), "unavailable"),  # the status decides before the body is read
    (httpx.ConnectError("refused"), "unavailable"),
    (httpx.ReadTimeout("slow"), "unavailable"),
    (httpx.Response(200, text="<html>"), "invalid_answer"),
    (httpx.Response(200, json={"unexpected": 1}), "invalid_answer"),
    (undecodable(200), "invalid_answer"),  # httpx's DecodingError is no ValueError
]


@pytest.mark.parametrize(
    ("answer", "code"),
    [*NO_ANSWER, (httpx.Response(200, text="[" * 100_000), "invalid_answer")],  # nested past reading
)
async def test_the_http_discovery_source_s_failures_get_the_documented_code(
    clock: FixedClock, answer: Answer, code: str
) -> None:
    async with answering(answer) as client:
        source = HttpDiscoverySource("http://provider.example/discovery", client=client)
        snapshot = await DiscoveryService(source, DefaultPolicy(), clock).refresh()
    assert snapshot.last_error == code


def usage_page(created_at_ms: float) -> httpx.Response:
    return httpx.Response(
        200, json={"items": [{"id": "u-1", "status": "applied", "created_at_ms": created_at_ms}]}
    )


async def synced_once(store: SqlStore, clock: FixedClock, answer: Answer) -> CostService:
    stop = asyncio.Event()
    async with answering(answer) as client:
        source = BatteriesUsageSource("http://provider.example", "token", client=client)
        page = source.page

        async def one_pass(cursor: str | None, limit: int = 1000) -> UsagePage:
            stop.set()
            return await page(cursor, limit)

        source.page = one_pass  # type: ignore[method-assign]
        costs = CostService(store, DefaultPolicy(), clock, CostSyncWorker(source, store))
        await costs.run(stop)
    return costs


@pytest.mark.parametrize(("answer", "code"), NO_ANSWER)
async def test_the_batteries_usage_source_s_failures_get_the_documented_code(
    store: SqlStore, clock: FixedClock, answer: Answer, code: str
) -> None:
    assert (await synced_once(store, clock, answer)).last_error == code


@pytest.mark.parametrize("created_at_ms", [10**20, 1.5e300])
async def test_a_usage_row_whose_time_no_clock_holds_is_set_aside_and_fails_no_sync(
    store: SqlStore, clock: FixedClock, created_at_ms: float
) -> None:
    """Such a row failed its page with ``unavailable`` (an OSError or an OverflowError), as if Batteries
    were down. It is set aside (#114, C13), so the sync has no error to show."""
    costs = await synced_once(store, clock, usage_page(created_at_ms))
    assert (costs.last_error, costs.set_aside_rows) == (None, 1)


# -- the codes are a type: the schema publishes them and nothing else fits ----------------------------


def test_the_health_schema_publishes_the_codes() -> None:
    properties = EngineHealth.model_json_schema()["properties"]
    codes = {
        name: properties[name]["anyOf"][0].get("enum") for name in ("discovery_error", "cost_sync_error")
    }
    assert codes == {
        "discovery_error": ["unavailable", "invalid_answer"],
        "cost_sync_error": ["unavailable", "invalid_answer", "store_error"],
    }


def test_free_text_is_no_code(failing_engine: BuilderEngine) -> None:
    health = failing_engine.health().model_dump()
    text = "connect to http://discovery:8939 failed"
    with pytest.raises(ValidationError):
        EngineHealth(**{**health, "discovery_error": text})
    with pytest.raises(ValidationError):
        EngineHealth(**{**health, "cost_sync_error": text})
    with pytest.raises(ValidationError):
        EngineHealth(**{**health, "discovery_error": "store_error"})  # discovery writes to no store
    with pytest.raises(ValidationError):
        DiscoverySnapshot(runners=(), observed_at=None, last_attempt_at=None, last_error=text)  # type: ignore[arg-type]


# -- #158: a sync an admin asks for sets the code too -------------------------------------------------

ADMIN = ActorContext(actor_id="operator", credential_kind="clearinghouse_admin")
MANUAL_FAILURES: list[tuple[str, Exception, str]] = [
    ("source", ProviderUnavailable("Batteries unreachable: [Errno -3] name resolution"), "unavailable"),
    ("source", ProviderError(503, "overloaded"), "unavailable"),
    ("source", CursorMismatch("cursor does not match the filters"), "unavailable"),
    ("source", ProviderError(200, "the usage cursor did not advance"), "invalid_answer"),
    ("source", KeyError("created_at_ms"), "invalid_answer"),
    ("store", STORE_ERROR, "store_error"),
]


@pytest.mark.parametrize(("where", "error", "code"), MANUAL_FAILURES)
async def test_a_failed_cost_sync_an_admin_asked_for_sets_the_code_and_logs_the_text_as_the_loop_does(
    store: SqlStore,
    clock: FixedClock,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    where: str,
    error: Exception,
    code: str,
) -> None:
    """``POST /v1/costs/sync`` raised its error to the caller and left health as it was: only the
    background loop set ``cost_sync_error``, so ``/readyz`` said nothing of a sync that had just failed."""

    async def failing(*_: object, **__: object) -> UsagePage:
        raise error

    source = FakeUsageSource()
    owner, name = (source, "page") if where == "source" else (store, "apply_usage")
    working = getattr(owner, name)
    monkeypatch.setattr(owner, name, failing)
    costs = CostService(store, DefaultPolicy(), clock, CostSyncWorker(source, store))
    logs = caplog.at_level(logging.WARNING, logger="livepeer_builder.services.costs")
    with logs, pytest.raises(type(error)) as raised:
        await costs.sync_now(ADMIN)
    assert raised.value is error  # the caller still gets the error itself
    assert costs.last_error == code
    [logged] = [r for r in caplog.records if getattr(r, "event", "") == "costs.sync_failed"]
    assert (logged.code, logged.error) == (code, str(error))  # type: ignore[attr-defined]

    monkeypatch.setattr(owner, name, working)
    await costs.sync_now(ADMIN)  # and the next sync that works clears it, whoever asked
    assert costs.last_error is None


async def test_a_sync_that_never_started_sets_no_code(store: SqlStore, clock: FixedClock) -> None:
    """Refused before any sync: a caller who may not ask, and an engine with no usage source."""
    costs = CostService(store, DefaultPolicy(), clock, CostSyncWorker(FakeUsageSource(), store))
    reader = ActorContext(actor_id="alice", permissions=frozenset({"costs.read"}))
    with pytest.raises(AccessDenied):
        await costs.sync_now(reader)
    assert costs.last_error is None
    no_source = CostService(store, DefaultPolicy(), clock, None)
    with pytest.raises(ProviderUnavailable, match="no usage source"):
        await no_source.sync_now(ADMIN)
    assert no_source.last_error is None


async def test_readyz_shows_the_code_of_a_sync_asked_for_over_rest_until_one_works(store: SqlStore) -> None:
    down = True

    def handle(request: httpx.Request) -> httpx.Response:
        del request
        if down:
            return httpx.Response(503, json={"error": "overloaded"})
        return httpx.Response(200, json={"items": [], "next_cursor": ""})

    client = httpx.AsyncClient(base_url="http://provider.example", transport=httpx.MockTransport(handle))
    usage = BatteriesUsageSource("http://provider.example", "token", client=client)
    async with make_engine(supply=EngineSupply(store=store, usage_source=usage)) as engine:
        key = await engine.keys.bootstrap_admin()
        app = create_app(engine, manage_lifecycle=False)
        headers = {"Authorization": f"Bearer {key.token}"}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://engine"
        ) as http:
            assert (await http.get("/readyz")).json()["cost_sync_error"] is None
            failed = await http.post("/v1/costs/sync", headers=headers)
            assert failed.status_code == 502
            assert (await http.get("/readyz")).json()["cost_sync_error"] == "unavailable"
            down = False
            assert (await http.post("/v1/costs/sync", headers=headers)).status_code == 200
            assert (await http.get("/readyz")).json()["cost_sync_error"] is None
