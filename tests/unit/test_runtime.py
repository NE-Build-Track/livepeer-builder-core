"""Runtime: structured logging and the BuilderEngine lifecycle (loops, retention, default adapters)."""

import asyncio
import json
import logging
from importlib.metadata import PackageNotFoundError

import pytest

from livepeer_builder import BuilderEngine, EngineSettings
from livepeer_builder.adapters.batteries import BatteriesClient, BatteriesUsageSource
from livepeer_builder.adapters.sdk import SignerDiscoverySource
from livepeer_builder.adapters.static import StaticDiscoverySource
from livepeer_builder.runtime import engine as engine_module
from livepeer_builder.runtime.logging import JsonFormatter, configure_logging
from livepeer_builder.store import SqlStore
from livepeer_builder.testing import FakeUsageSource, FixedClock

MEMORY = "sqlite+aiosqlite:///:memory:"


def test_json_formatter_emits_event_fields_and_exceptions() -> None:
    record = logging.LogRecord("lpb.test", logging.WARNING, __file__, 1, "sync %s", ("failed",), None)
    record.event = "costs.sync_failed"
    entry = json.loads(JsonFormatter().format(record))
    assert (entry["level"], entry["message"], entry["event"]) == (
        "warning",
        "sync failed",
        "costs.sync_failed",
    )
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        with_exc = logging.LogRecord("lpb.test", logging.ERROR, __file__, 1, "x", (), sys.exc_info())
    assert "ValueError: boom" in json.loads(JsonFormatter().format(with_exc))["exception"]


def test_configure_logging_installs_one_handler() -> None:
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    try:
        configure_logging(json_lines=True, level=logging.DEBUG)
        assert len(root.handlers) == 1 and isinstance(root.handlers[0].formatter, JsonFormatter)
        configure_logging(json_lines=False)
        assert not isinstance(root.handlers[0].formatter, JsonFormatter)
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)


def test_package_version_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(_: str) -> str:
        raise PackageNotFoundError("livepeer-builder")

    monkeypatch.setattr(engine_module, "version", missing)
    assert engine_module.package_version() == "0.0.0+unknown"


async def test_default_adapters_follow_settings() -> None:
    settings = EngineSettings(
        database_url=MEMORY,
        signer_url="https://signer.example",
        batteries_url="https://batteries.example",
        batteries_token="t",
        grant_id="g",
        log_json=False,
    )
    engine = await BuilderEngine.from_settings(settings)
    try:
        assert isinstance(engine.discovery._source, SignerDiscoverySource)
        assert isinstance(engine.payments._manager, BatteriesClient)
        assert isinstance(engine.costs._worker._source, BatteriesUsageSource)  # type: ignore[union-attr]
    finally:
        await engine.stop()  # closes the owned clients and store


async def test_operator_token_wires_a_separate_grants_client() -> None:
    settings = EngineSettings(
        database_url=MEMORY,
        batteries_url="https://batteries.example",
        batteries_token="actor",
        batteries_operator_token="operator",
        log_json=False,
    )
    engine = await BuilderEngine.from_settings(settings)
    try:
        assert isinstance(engine.payments._manager, BatteriesClient)
        assert isinstance(engine.payments._grants, BatteriesClient)
        assert engine.payments._grants is not engine.payments._manager
    finally:
        await engine.stop()


async def test_start_runs_the_loops_and_stop_ends_them(store: SqlStore, clock: FixedClock) -> None:
    usage = FakeUsageSource()
    settings = EngineSettings(
        database_url=MEMORY,
        discovery_interval_s=0.01,
        cost_sync_interval_s=0.01,
        retention_jobs_days=30,
        log_json=False,
    )
    engine = await BuilderEngine.from_settings(
        settings, store=store, clock=clock, discovery_source=StaticDiscoverySource(), usage_source=usage
    )
    await engine.start()
    await asyncio.sleep(0.05)  # let each loop run at least once
    health = engine.health()
    assert health.ready and health.cost_sync_at is not None
    await engine.stop()
    assert engine._tasks == []


async def test_cost_sync_errors_surface_in_health(store: SqlStore, clock: FixedClock) -> None:
    class Broken:
        async def page(self, cursor: str | None, limit: int = 1000) -> object:
            raise RuntimeError("batteries down")

    settings = EngineSettings(database_url=MEMORY, cost_sync_interval_s=0.01, log_json=False)
    engine = await BuilderEngine.from_settings(
        settings, store=store, clock=clock, discovery_source=StaticDiscoverySource(), usage_source=Broken()
    )
    await engine.start()
    await asyncio.sleep(0.05)
    await engine.stop()
    assert engine.health().cost_sync_error == "batteries down"


async def test_retention_failures_are_logged_not_fatal(
    store: SqlStore, clock: FixedClock, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    settings = EngineSettings(database_url=MEMORY, retention_usage_days=1, log_json=False)
    engine = await BuilderEngine.from_settings(
        settings, store=store, clock=clock, discovery_source=StaticDiscoverySource()
    )

    async def broken_purge() -> object:
        raise RuntimeError("disk full")

    monkeypatch.setattr(engine, "purge", broken_purge)
    with caplog.at_level(logging.ERROR):
        await engine.start()
        await asyncio.sleep(0.02)
        await engine.stop()
    assert any(getattr(r, "event", "") == "store.purge_failed" for r in caplog.records)


async def test_closer_failures_are_logged(
    store: SqlStore, clock: FixedClock, caplog: pytest.LogCaptureFixture
) -> None:
    class BadCloser:
        async def aclose(self) -> None:
            raise RuntimeError("socket gone")

    engine = await BuilderEngine.from_settings(
        EngineSettings(database_url=MEMORY, log_json=False),
        store=store,
        clock=clock,
        discovery_source=StaticDiscoverySource(),
    )
    engine._closers = (BadCloser(), object())
    with caplog.at_level(logging.ERROR):
        await engine.stop()
    assert any(getattr(r, "event", "") == "engine.close_failed" for r in caplog.records)


async def test_purge_and_revision_without_a_sql_store(clock: FixedClock) -> None:
    class NotSql:
        async def close(self) -> None:
            return None

    engine = await BuilderEngine.from_settings(
        EngineSettings(database_url=MEMORY, log_json=False),
        store=NotSql(),  # type: ignore[arg-type]
        clock=clock,
        discovery_source=StaticDiscoverySource(),
    )
    assert (await engine.purge()).jobs_deleted == 0
    assert await engine.schema_revision() is None
