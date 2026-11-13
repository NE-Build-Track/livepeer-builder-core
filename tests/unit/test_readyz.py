"""#113 C7: ``GET /readyz`` needs no credential, so its errors are short fixed codes, never exception text.

A cost sync that failed in the store served the store error's SQL and parameters to anyone, and a failed
discovery refresh served whatever its source raised. ``discovery_error`` and ``cost_sync_error`` now carry
``unavailable``, ``invalid_answer`` or ``store_error``; the full text goes to the log. The answer keeps its
shape, and the version, the running-job count and the allocation mode stay public.
"""

import asyncio
import dataclasses
import logging
from collections.abc import AsyncIterator, Sequence

import httpx
import pytest
from sqlalchemy.exc import IntegrityError

from livepeer_builder import BuilderEngine, EngineParts, EngineSupply
from livepeer_builder.contracts import Runner, UsagePage, UsageRow
from livepeer_builder.errors import CursorMismatch, ProviderError, ProviderUnavailable
from livepeer_builder.http import create_app
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
        "ready",
        "discovery_fresh",
        "discovery_observed_at",
        "discovery_error",
        "cost_sync_at",
        "cost_sync_error",
        "cost_sync_set_aside",  # a count (#114, C13)
        "running_jobs",
        "allocation_mode",
        "version",
    }
    assert (health["running_jobs"], health["allocation_mode"]) == (0, "per_actor")
    assert health["version"] == failing_engine.health().version
