"""Runtime: structured logging and the BuilderEngine lifecycle (loops, retention, default adapters)."""

import asyncio
import contextlib
import dataclasses
import json
import logging
from datetime import datetime
from importlib.metadata import PackageNotFoundError
from pathlib import Path

import pytest
from sqlalchemy import text

from livepeer_builder import (
    BuilderEngine,
    EngineParts,
    EngineSettings,
    EngineSupply,
    PurgeReport,
    generate_secret_key,
)
from livepeer_builder.adapters.batteries import BatteriesClient, BatteriesUsageSource
from livepeer_builder.adapters.sdk import HttpDiscoverySource
from livepeer_builder.http import create_app
from livepeer_builder.runtime import engine as engine_module
from livepeer_builder.runtime.assembly import assemble, default_parts
from livepeer_builder.runtime.logging import JsonFormatter, configure_logging
from livepeer_builder.store import SqlStore, head_revision, upgrade
from livepeer_builder.testing import FakeUsageSource, FixedClock

MEMORY = "sqlite+aiosqlite:///:memory:"
LOOPS = {"lpb-discovery", "lpb-cost-sync", "lpb-retention"}


def running_loops() -> set[str]:
    """The engine's background loops still running, observed by their task names."""
    return {task.get_name() for task in asyncio.all_tasks() if task.get_name() in LOOPS}


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


async def test_batteries_without_an_operator_token_starts_without_the_grant_desk() -> None:
    settings = EngineSettings(
        database_url=MEMORY,
        batteries_url="https://batteries.example",
        batteries_token="t",
        secret_key=generate_secret_key(),  # per_actor with a provider needs it (#112, C2)
        log_json=False,
        discovery_url="http://127.0.0.1:9/discovery",
    )
    async with contextlib.AsyncExitStack() as stack:
        parts = await default_parts(settings, stack)
        assert isinstance(parts.allocation_manager, BatteriesClient)  # funding still works
        assert parts.grants_manager is None  # the engine token cannot list or create grants
        assert assemble(settings, parts).payments.grants is None


async def test_default_adapters_follow_settings() -> None:
    settings = EngineSettings(
        database_url=MEMORY,
        signer_url="https://signer.example",
        batteries_url="https://batteries.example",
        batteries_token="t",
        batteries_operator_token="desk",
        secret_key=generate_secret_key(),
        log_json=False,
        discovery_url="http://127.0.0.1:9/discovery",
    )
    async with contextlib.AsyncExitStack() as stack:  # closes the owned clients and store
        parts = await default_parts(settings, stack)
        assert isinstance(parts.discovery_source, HttpDiscoverySource)
        assert isinstance(parts.allocation_manager, BatteriesClient)
        assert isinstance(parts.usage_source, BatteriesUsageSource)  # unfiltered by design (iss-04)


async def test_a_discovery_url_reads_the_discovery_service() -> None:
    settings = EngineSettings(
        database_url=MEMORY, discovery_url="http://discovery:8939/discovery", log_json=False
    )
    async with contextlib.AsyncExitStack() as stack:
        parts = await default_parts(settings, stack)
        assert isinstance(parts.discovery_source, HttpDiscoverySource)


async def test_operator_token_wires_a_separate_grants_client() -> None:
    settings = EngineSettings(
        database_url=MEMORY,
        batteries_url="https://batteries.example",
        batteries_token="actor",
        batteries_operator_token="operator",
        secret_key=generate_secret_key(),
        log_json=False,
        discovery_url="http://127.0.0.1:9/discovery",
    )
    async with contextlib.AsyncExitStack() as stack:
        parts = await default_parts(settings, stack)
        assert isinstance(parts.allocation_manager, BatteriesClient)
        assert isinstance(parts.grants_manager, BatteriesClient)
        assert parts.grants_manager is not parts.allocation_manager
        assert assemble(settings, parts).payments.grants is not None


async def test_start_runs_the_loops_and_stop_ends_them(store: SqlStore, clock: FixedClock) -> None:
    usage = FakeUsageSource()
    settings = EngineSettings(
        database_url=MEMORY,
        discovery_interval_s=0.01,
        cost_sync_interval_s=0.01,
        retention_jobs_days=30,
        log_json=False,
        discovery_url="http://127.0.0.1:9/discovery",
    )
    engine = await BuilderEngine.create(
        settings,
        supply=EngineSupply(store=store, clock=clock, usage_source=usage),
    )
    await engine.start()
    async with asyncio.timeout(5):  # wait for each loop's first run, not a fixed time
        while engine.health().cost_sync_at is None:  # noqa: ASYNC110  # health exposes no event
            await asyncio.sleep(0.01)
    health = engine.health()
    assert health.ready and health.cost_sync_at is not None
    await engine.stop()
    assert running_loops() == set()


async def test_cost_sync_errors_surface_in_health(store: SqlStore, clock: FixedClock) -> None:
    class Broken:
        async def page(self, cursor: str | None, limit: int = 1000) -> object:
            raise RuntimeError("batteries down")

    settings = EngineSettings(
        database_url=MEMORY,
        cost_sync_interval_s=0.01,
        log_json=False,
        discovery_url="http://127.0.0.1:9/discovery",
    )
    engine = await BuilderEngine.create(
        settings,
        supply=EngineSupply(store=store, clock=clock, usage_source=Broken()),
    )
    await engine.start()
    await asyncio.sleep(0.05)
    await engine.stop()
    assert engine.health().cost_sync_error == "batteries down"


async def test_retention_failures_are_logged_not_fatal(
    store: SqlStore, clock: FixedClock, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    settings = EngineSettings(
        database_url=MEMORY,
        retention_usage_days=1,
        log_json=False,
        discovery_url="http://127.0.0.1:9/discovery",
    )
    engine = await BuilderEngine.create(settings, supply=EngineSupply(store=store, clock=clock))

    async def broken_purge() -> object:
        raise RuntimeError("disk full")

    monkeypatch.setattr(engine, "purge", broken_purge)
    with caplog.at_level(logging.ERROR):
        await engine.start()
        await asyncio.sleep(0.02)
        await engine.stop()
    assert any(getattr(r, "event", "") == "store.purge_failed" for r in caplog.records)


async def test_cleanup_failures_are_logged_not_raised(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def fails(self: SqlStore) -> None:
        raise RuntimeError("socket gone")

    # Fault injection on builder-core's own store: there is no seam for an engine-owned resource.
    monkeypatch.setattr(SqlStore, "close", fails)
    engine = await BuilderEngine.create(
        EngineSettings(database_url=MEMORY, log_json=False, discovery_url="http://127.0.0.1:9/discovery")
    )
    with caplog.at_level(logging.ERROR):
        await engine.stop()  # does not raise
    failures = [r for r in caplog.records if getattr(r, "event", "") == "engine.close_failed"]
    assert len(failures) == 1 and "SqlStore" in str(getattr(failures[0], "resource", ""))


async def test_a_failure_part_way_through_construction_closes_what_it_opened(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[SqlStore] = []
    closed: list[SqlStore] = []
    connect, close = SqlStore.connect, SqlStore.close

    async def recording_connect(url: str, *, migrate: bool = True) -> SqlStore:
        store = await connect(url, migrate=migrate)
        opened.append(store)
        return store

    async def recording_close(self: SqlStore) -> None:
        closed.append(self)
        await close(self)

    # Spies: they record and delegate, changing no behaviour (the store has no injection seam yet).
    monkeypatch.setattr(SqlStore, "connect", recording_connect)
    monkeypatch.setattr(SqlStore, "close", recording_close)
    settings = EngineSettings(
        database_url=MEMORY,
        secret_key="not-a-fernet-key",
        log_json=False,
        discovery_url="http://127.0.0.1:9/discovery",
    )
    with pytest.raises(ValueError, match="Fernet key"):
        await BuilderEngine.create(settings)
    assert len(opened) == 1 and closed == opened


async def test_stop_twice_is_safe_and_a_stopped_engine_cannot_restart() -> None:
    engine = await BuilderEngine.create(
        EngineSettings(database_url=MEMORY, log_json=False, discovery_url="http://127.0.0.1:9/discovery"),
        supply=EngineSupply(),
    )
    await engine.stop()  # before start, as CLI commands do
    await engine.stop()
    with pytest.raises(RuntimeError, match="engine is stopped"):
        await engine.start()


async def test_purge_and_revision_go_to_the_store_maintenance(clock: FixedClock) -> None:
    class Maintenance:
        def __init__(self) -> None:
            self.purges: list[tuple[datetime, int | None, int | None]] = []

        async def purge(self, now: datetime, jobs_days: int | None, usage_days: int | None) -> PurgeReport:
            self.purges.append((now, jobs_days, usage_days))
            return PurgeReport(jobs_deleted=3, usage_rows_deleted=4)

        async def schema_revision(self) -> str | None:
            return "vault-7"

    class NotSql:
        maintenance = Maintenance()

        async def close(self) -> None:
            return None

    engine = await BuilderEngine.create(
        EngineSettings(
            database_url=MEMORY,
            log_json=False,
            retention_jobs_days=30,
            retention_usage_days=7,
            discovery_url="http://127.0.0.1:9/discovery",
        ),
        supply=EngineSupply(
            store=NotSql(),  # type: ignore[arg-type]
            clock=clock,
        ),
    )
    assert await engine.purge() == PurgeReport(jobs_deleted=3, usage_rows_deleted=4)
    assert NotSql.maintenance.purges == [(clock.now(), 30, 7)]
    assert await engine.schema_revision() == "vault-7"


async def test_purge_and_revision_without_store_maintenance(clock: FixedClock) -> None:
    class NotSql:
        maintenance = None

        async def close(self) -> None:
            return None

    engine = await BuilderEngine.create(
        EngineSettings(database_url=MEMORY, log_json=False, discovery_url="http://127.0.0.1:9/discovery"),
        supply=EngineSupply(
            store=NotSql(),  # type: ignore[arg-type]
            clock=clock,
        ),
    )
    assert (await engine.purge()).jobs_deleted == 0
    assert await engine.schema_revision() is None


async def test_retention_runs_every_retention_interval(
    store: SqlStore, clock: FixedClock, caplog: pytest.LogCaptureFixture
) -> None:
    settings = EngineSettings(
        database_url=MEMORY,
        retention_jobs_days=30,
        retention_interval_s=0.02,
        log_json=False,
        discovery_url="http://127.0.0.1:9/discovery",
    )
    engine = await BuilderEngine.create(settings, supply=EngineSupply(store=store, clock=clock))
    with caplog.at_level(logging.INFO, logger="livepeer_builder.runtime.engine"):
        await engine.start()
        await asyncio.sleep(0.15)  # about seven intervals
        await engine.stop()
    purges = [r for r in caplog.records if getattr(r, "event", None) == "store.purged"]
    assert len(purges) >= 3


def record_closes(monkeypatch: pytest.MonkeyPatch) -> list[SqlStore]:
    """Spy on SqlStore.close (it records, then closes): the stores ``create`` opened that were closed."""
    closed: list[SqlStore] = []
    close = SqlStore.close

    async def recording_close(self: SqlStore) -> None:
        closed.append(self)
        await close(self)

    monkeypatch.setattr(SqlStore, "close", recording_close)
    return closed


async def database_at(path: Path, revision: str, stamp: str | None = None) -> str:
    """A database file migrated to ``revision``; ``stamp`` then records a revision this package lacks."""
    url = f"sqlite+aiosqlite:///{path / 'engine.db'}"
    store = await SqlStore.connect(url, migrate=False)
    try:
        await upgrade(store.engine, revision)
        if stamp is not None:  # a newer release migrated it
            async with store.engine.begin() as conn:
                await conn.execute(text("UPDATE lpb_alembic_version SET version_num = :v"), {"v": stamp})
    finally:
        await store.close()
    return url


def without_auto_migrate(url: str) -> EngineSettings:
    return EngineSettings(
        database_url=url, auto_migrate=False, log_json=False, discovery_url="http://127.0.0.1:9/discovery"
    )


@pytest.mark.parametrize(
    ("revision", "stamp", "database"),
    [("0010", None, "0010"), ("head", "9999", "9999")],
    ids=["behind", "ahead"],
)
async def test_without_auto_migrate_a_database_at_another_revision_stops_startup(
    tmp_path: Path, revision: str, stamp: str | None, database: str
) -> None:
    """#112 C8: the engine would start ready, pay, then fail to record. It stops at start instead, and the
    message names both revisions and the command that fixes it."""
    engine = await BuilderEngine.create(without_auto_migrate(await database_at(tmp_path, revision, stamp)))
    with pytest.raises(RuntimeError) as refused:
        await engine.start()
    message = str(refused.value)
    assert database in message and head_revision() in message
    assert str(tmp_path / "engine.db") in message  # the database that was checked, by name
    if revision == "head":  # ahead: this package cannot migrate it, so it does not say to
        assert "another release" in message and "run `lpb migrate`" not in message
    else:  # behind: migrate that database
        assert "run `lpb migrate`" in message and "LPB_DATABASE_URL" in message
    assert not engine.health().ready  # no discovery snapshot was taken
    with pytest.raises(RuntimeError, match="engine is stopped"):
        await engine.start()


async def test_without_auto_migrate_a_current_database_starts_and_is_checked_only_then(
    tmp_path: Path,
) -> None:
    """The check runs at start only: a replica already running keeps running when another one migrates."""
    url = await database_at(tmp_path, "head")
    engine = await BuilderEngine.create(without_auto_migrate(url), supply=QUIET)
    await engine.start()
    try:
        await database_at(tmp_path, "head", stamp="9999")  # a newer release migrated the database
        await engine.discovery.refresh()
        assert engine.health().ready
    finally:
        await engine.stop()


class _OverSql:
    """A supplied store over the engine's SQL store: every call goes to it, and ``maintenance`` is its own."""

    def __init__(self, inner: SqlStore, maintenance: object) -> None:
        self._inner = inner
        self.maintenance = maintenance

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


class _OwnRevisions:
    """Housekeeping of a store that keeps its own schema: its revisions are not this package's."""

    async def purge(self, now: datetime, jobs_days: int | None, usage_days: int | None) -> PurgeReport:
        return PurgeReport(jobs_deleted=0, usage_rows_deleted=0)

    async def schema_revision(self) -> str | None:
        return "vault-7"


async def test_without_auto_migrate_a_supplied_store_with_its_own_revisions_still_starts(
    tmp_path: Path,
) -> None:
    """The check is for the packaged migrations (#112, C8). A supplied store reports revisions of its own
    schema, which ``lpb migrate`` cannot change, so comparing them with this package's head would stop a
    working engine."""
    inner = await SqlStore.connect(await database_at(tmp_path, "head"), migrate=False)
    try:
        store = _OverSql(inner, _OwnRevisions())
        engine = await BuilderEngine.create(
            without_auto_migrate(MEMORY),
            supply=EngineSupply(store=store),  # type: ignore[arg-type]
        )
        await engine.start()
        try:
            assert await engine.schema_revision() == "vault-7"
        finally:
            await engine.stop()
    finally:
        await inner.close()  # a supplied store is borrowed: the engine never closes it


async def test_without_auto_migrate_a_decorated_sql_store_is_still_checked(tmp_path: Path) -> None:
    """A wrapper that keeps the SQL store's own housekeeping still runs on the packaged migrations."""
    inner = await SqlStore.connect(await database_at(tmp_path, "0010"), migrate=False)
    try:
        store = _OverSql(inner, inner.maintenance)
        engine = await BuilderEngine.create(
            without_auto_migrate(MEMORY),
            supply=EngineSupply(store=store),  # type: ignore[arg-type]
        )
        with pytest.raises(RuntimeError, match="lpb migrate"):
            await engine.start()
    finally:
        await inner.close()  # borrowed: the failed start closes only what the engine opened


async def test_the_refusal_names_a_supplied_stores_own_database_not_the_configured_one(
    tmp_path: Path,
) -> None:
    """``lpb migrate`` migrates ``LPB_DATABASE_URL``. A supplied SQL store may be another database, so the
    message names the one that was checked (review of #129)."""
    inner = await SqlStore.connect(await database_at(tmp_path, "0010"), migrate=False)
    try:
        engine = await BuilderEngine.create(
            without_auto_migrate(MEMORY),
            supply=EngineSupply(store=_OverSql(inner, inner.maintenance)),  # type: ignore[arg-type]
        )
        with pytest.raises(RuntimeError) as refused:
            await engine.start()
    finally:
        await inner.close()
    message = str(refused.value)
    assert str(tmp_path / "engine.db") in message and ":memory:" not in message


def test_the_database_named_in_the_refusal_hides_its_password() -> None:
    from sqlalchemy.ext.asyncio import create_async_engine

    from livepeer_builder.store.maintenance import SqlMaintenance

    named = SqlMaintenance(
        create_async_engine("postgresql+asyncpg://lpb:hunter2@db.internal/engine")
    ).database
    assert "hunter2" not in named and "db.internal/engine" in named


def unmigrated() -> EngineSettings:
    """No tables: ``start()`` refuses the schema (#112, C8) after ``create`` opened the store."""
    return EngineSettings(
        database_url=MEMORY, auto_migrate=False, log_json=False, discovery_url="http://127.0.0.1:9/discovery"
    )


QUIET = EngineSupply()


async def test_a_failed_start_in_async_with_closes_what_create_opened(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed = record_closes(monkeypatch)
    engine = await BuilderEngine.create(unmigrated(), supply=QUIET)
    with pytest.raises(RuntimeError, match="lpb migrate"):
        async with engine:
            pytest.fail("the block must not run when start() fails")
    assert len(closed) == 1
    with pytest.raises(RuntimeError, match="engine is stopped"):
        await engine.start()


async def test_a_failed_start_in_the_http_lifespan_closes_what_create_opened(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed = record_closes(monkeypatch)
    engine = await BuilderEngine.create(unmigrated(), supply=QUIET)
    app = create_app(engine)
    with pytest.raises(RuntimeError, match="lpb migrate"):
        async with app.router.lifespan_context(app):
            pytest.fail("the app must not serve when start() fails")
    assert len(closed) == 1


async def test_a_failure_in_assemble_closes_what_create_opened(monkeypatch: pytest.MonkeyPatch) -> None:
    closed = record_closes(monkeypatch)

    def broken(parts: EngineParts) -> EngineParts:
        return dataclasses.replace(parts, api_keys=object())  # type: ignore[arg-type]  # assemble cannot use it

    with pytest.raises(AttributeError):
        await BuilderEngine.create(
            EngineSettings(database_url=MEMORY, log_json=False, discovery_url="http://127.0.0.1:9/discovery"),
            supply=QUIET,
            decorate=broken,
        )
    assert len(closed) == 1
