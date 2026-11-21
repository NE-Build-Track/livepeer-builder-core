"""The discovery address is checked when the engine starts, not when settings are read (#154, D63).

Settings without ``LPB_DISCOVERY_URL`` are read, and an engine is built from them: ``lpb purge`` and
``lpb bootstrap-admin`` build one and never start it. ``start()`` refuses such an engine before anything
polls, whatever its discovery source is, and ``serve`` refuses before it opens anything.
"""

import dataclasses
import inspect
import traceback
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from livepeer_builder import BuilderEngine, EngineParts, EngineSettings, EngineSupply, cli
from livepeer_builder.contracts import Job, Runner
from livepeer_builder.http import build_app, create_app, serve
from livepeer_builder.protocols import DiscoverySource
from livepeer_builder.store import SqlStore, head_revision
from livepeer_builder.testing import FixedClock, runner
from tests.unit.test_runtime import record_closes, running_loops

MEMORY = "sqlite+aiosqlite:///:memory:"
CREDENTIAL = "throwaway-credential"
REFUSAL = "LPB_DISCOVERY_URL is required to start the engine"


def no_address(**values: object) -> EngineSettings:
    return EngineSettings.model_validate({"database_url": MEMORY, "log_json": False, **values})


class Counting:
    """A discovery source that counts how often it was asked: around another, or with runners of its own."""

    def __init__(self, inner: DiscoverySource | None = None) -> None:
        self.inner = inner
        self.fetches = 0

    async def fetch(self) -> tuple[Runner, ...]:
        self.fetches += 1
        return await self.inner.fetch() if self.inner is not None else (runner(),)


def test_settings_without_the_address_are_read() -> None:
    assert EngineSettings().discovery_url is None
    assert EngineSettings.model_validate({}).discovery_url is None
    assert EngineSettings.from_env({}).discovery_url is None


def test_an_address_given_as_none_in_code_is_unset() -> None:
    """An embedder that passes what it read, set or not."""
    assert EngineSettings(discovery_url=None).discovery_url is None
    assert EngineSettings.model_validate({"discovery_url": None}).discovery_url is None


def test_an_empty_address_in_the_environment_reads_as_unset() -> None:
    assert EngineSettings.from_env({"LPB_DISCOVERY_URL": ""}).discovery_url is None


@pytest.mark.parametrize("given", ["", "   ", "discovery:8939/discovery"])
def test_an_address_given_in_code_that_is_not_one_is_still_refused_when_settings_are_read(given: str) -> None:
    with pytest.raises(ValidationError, match="LPB_DISCOVERY_URL must be an http:// or https:// URL"):
        EngineSettings(discovery_url=given)


def test_from_env_takes_no_defaults_and_the_cli_holds_no_stand_in_address() -> None:
    assert list(inspect.signature(EngineSettings.from_env).parameters) == ["env"]
    assert not hasattr(cli, "NO_DISCOVERY_URL")


def test_settings_with_the_address_give_it_to_an_engine_about_to_start() -> None:
    address = "http://discovery:8939/discovery"
    assert EngineSettings(discovery_url=address).require_discovery_url() == address


def test_the_refusal_names_the_setting_and_holds_no_setting_given() -> None:
    settings = no_address(secret_key=f"key-{CREDENTIAL}", batteries_token=f"tok-{CREDENTIAL}")
    with pytest.raises(ValueError, match=REFUSAL) as refused:
        settings.require_discovery_url()
    assert type(refused.value) is ValueError  # not pydantic's: it carries no settings
    assert CREDENTIAL not in "".join(traceback.format_exception(refused.value))


async def test_an_engine_is_built_without_the_address_and_does_what_needs_no_runner() -> None:
    engine = await BuilderEngine.create(no_address())
    try:
        assert (await engine.purge()).jobs_deleted == 0
        assert engine.keys is not None
        assert (await engine.keys.bootstrap_admin()).token.startswith("lpk_")
        assert not engine.health().ready
    finally:
        await engine.stop()


async def test_a_built_engine_without_the_address_holds_a_source_that_reads_nothing() -> None:
    """Asked by hand, it fails as a source that gave no answer, and the words are the refusal's."""
    engine = await BuilderEngine.create(no_address())
    try:
        with pytest.raises(RuntimeError, match=REFUSAL):
            await engine.discovery._source.fetch()
        snapshot = await engine.discovery.refresh()
        assert (snapshot.runners, snapshot.last_error) == ((), "unavailable")
    finally:
        await engine.stop()


async def _running_job(store: SqlStore) -> Job:
    job = Job(
        id=uuid4(), actor_id="alice", tenant_id="default", operation_ref=None, mode="immediate",
        app="example/echo", capability=None, model=None, state="running", attempts=(), failure=None,
        result=None, usage=None, created_at=FixedClock().now(), completed_at=None,
    )  # fmt: skip
    await store.create_job(job, None)
    return job


async def test_start_refuses_without_the_address_before_anything_polls_or_is_written(
    store: SqlStore, clock: FixedClock
) -> None:
    """D63: an engine does not start without the address. The refusal comes first: no poll, no loop, and
    a job another process is running is not resolved as interrupted."""
    job = await _running_job(store)
    asked: list[Counting] = []

    def counting(parts: EngineParts) -> EngineParts:
        asked.append(Counting(parts.discovery_source))
        return dataclasses.replace(parts, discovery_source=asked[0])

    engine = await BuilderEngine.create(
        no_address(), supply=EngineSupply(store=store, clock=clock), decorate=counting
    )
    with pytest.raises(ValueError, match=REFUSAL):
        await engine.start()
    assert asked[0].fetches == 0 and running_loops() == set()
    assert not engine.health().ready
    stored = await store.get_job(job.id)
    assert stored is not None and stored.state == "running"


async def test_start_refuses_without_the_address_whatever_the_discovery_source_is() -> None:
    """A ``decorate`` stage can put a source of its own in place of the built-in reader. Such an engine
    needs the address to start too, as it did while settings required it (#154)."""
    own = Counting()
    engine = await BuilderEngine.create(
        no_address(), decorate=lambda parts: dataclasses.replace(parts, discovery_source=own)
    )
    with pytest.raises(ValueError, match=REFUSAL):
        await engine.start()
    assert own.fetches == 0


async def test_a_replaced_source_with_the_address_set_starts_as_before() -> None:
    own = Counting()
    engine = await BuilderEngine.create(
        no_address(discovery_url="http://127.0.0.1:9/discovery"),
        decorate=lambda parts: dataclasses.replace(parts, discovery_source=own),
    )
    await engine.start()
    try:
        assert own.fetches == 1 and engine.health().ready and engine.health().discovery_error is None
    finally:
        await engine.stop()


async def test_a_refused_start_stops_the_engine_and_closes_what_create_opened(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed = record_closes(monkeypatch)
    engine = await BuilderEngine.create(no_address())
    with pytest.raises(ValueError, match=REFUSAL):
        async with engine:
            pytest.fail("the block must not run when start() refuses")
    assert len(closed) == 1
    with pytest.raises(RuntimeError, match="engine is stopped"):
        await engine.start()


async def test_the_http_lifespan_refuses_without_the_address_and_closes_what_create_opened(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed = record_closes(monkeypatch)
    engine = await BuilderEngine.create(no_address())
    app = create_app(engine)
    with pytest.raises(ValueError, match=REFUSAL):
        async with app.router.lifespan_context(app):
            pytest.fail("the app must not serve when start() refuses")
    assert len(closed) == 1


def record_connects(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Spy on SqlStore.connect: the databases that were opened, and migrated when the settings say so."""
    opened: list[str] = []
    connect = SqlStore.connect

    async def recording_connect(url: str, **options: bool) -> SqlStore:
        opened.append(url)
        return await connect(url, **options)

    monkeypatch.setattr(SqlStore, "connect", recording_connect)
    return opened


async def test_serve_refuses_without_the_address_before_it_opens_anything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``serve`` always starts its engine, so it asks first: no store is opened or migrated, no engine is
    built and no server runs. The stock server's own failed start would exit with uvicorn's code."""
    import uvicorn

    async def never(self: uvicorn.Server) -> None:
        pytest.fail("the server must not run without the address")

    opened = record_connects(monkeypatch)
    monkeypatch.setattr(uvicorn.Server, "serve", never)
    with pytest.raises(ValueError, match=REFUSAL):
        await serve(no_address())
    assert opened == []


async def test_build_app_refuses_without_the_address_before_it_opens_anything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Its app starts the engine in its lifespan, always. ``create`` opens the database and migrates it,
    so the refusal comes before ``create``, not when the host enters the lifespan (review of #188)."""
    opened = record_connects(monkeypatch)
    with pytest.raises(ValueError, match=REFUSAL):
        await build_app(no_address())
    assert opened == []


async def test_create_opens_and_migrates_the_database_before_start_can_refuse(tmp_path: Path) -> None:
    """What an application that starts its own engine must know (review of #188): ``create`` builds with
    no address, for the commands that never start, so by the time ``start()`` refuses, the database is
    at this package's revision. Asking the settings before ``create`` refuses first."""
    database = tmp_path / "engine.db"
    settings = no_address(database_url=f"sqlite+aiosqlite:///{database}")
    with pytest.raises(ValueError, match=REFUSAL):
        settings.require_discovery_url()
    assert not database.exists()
    engine = await BuilderEngine.create(settings)
    assert await engine.schema_revision() == head_revision()
    with pytest.raises(ValueError, match=REFUSAL):
        await engine.start()
