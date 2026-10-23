"""Shared fixtures: a real SQLite store, a fixed clock, fakes, and an engine factory."""

import dataclasses
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import pytest

from livepeer_builder import UNSET, BuilderEngine, EngineSettings, EngineSupply
from livepeer_builder.adapters.secrets import EncryptedDbSecretStore
from livepeer_builder.adapters.static import StaticDiscoverySource
from livepeer_builder.contracts import ActorContext, Runner
from livepeer_builder.store import SqlStore
from livepeer_builder.testing import (
    FakeAllocationManager,
    FakeUsageSource,
    FixedClock,
    ScriptedStreamTransport,
    runner,
)

MEMORY_URL = "sqlite+aiosqlite:///:memory:"


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock()


@pytest.fixture
async def store() -> AsyncIterator[SqlStore]:
    opened = await SqlStore.connect(MEMORY_URL)
    yield opened
    await opened.close()


@pytest.fixture
def transport() -> ScriptedStreamTransport:
    return ScriptedStreamTransport()


@pytest.fixture
def manager() -> FakeAllocationManager:
    return FakeAllocationManager()


@pytest.fixture
def usage() -> FakeUsageSource:
    return FakeUsageSource()


@pytest.fixture
def admin() -> ActorContext:
    return ActorContext(actor_id="operator", credential_kind="admin")


@pytest.fixture
def alice() -> ActorContext:
    return ActorContext(
        actor_id="alice", permissions=frozenset({"discover", "jobs.run", "jobs.read", "costs.read"})
    )


@pytest.fixture
def bob() -> ActorContext:
    return ActorContext(
        actor_id="bob", permissions=frozenset({"discover", "jobs.run", "jobs.read", "costs.read"})
    )


EngineFactory = Callable[..., Awaitable[BuilderEngine]]


@pytest.fixture
async def make_engine(
    store: SqlStore,
    clock: FixedClock,
    transport: ScriptedStreamTransport,
    manager: FakeAllocationManager,
    usage: FakeUsageSource,
) -> AsyncIterator[EngineFactory]:
    engines: list[BuilderEngine] = []

    async def factory(
        runners: tuple[Runner, ...] = (runner(),), *, supply: EngineSupply | None = None, **settings: Any
    ) -> BuilderEngine:
        """An engine over in-memory parts; ``supply`` replaces any of them for one test."""
        values: dict[str, Any] = {
            "database_url": MEMORY_URL,
            "allocation_mode": "per_actor",
            "secret_key": EncryptedDbSecretStore.generate_key(),
            "log_json": False,
        }
        values.update(settings)
        engine = await BuilderEngine.create(
            EngineSettings.model_validate(values),
            supply=_overlay(
                EngineSupply(
                    store=store,
                    clock=clock,
                    discovery_source=StaticDiscoverySource(runners),
                    transport=transport,
                    allocation_manager=manager,
                    grants_manager=manager,
                    usage_source=usage,
                ),
                supply,
            ),
        )
        await engine.discovery.refresh()
        engines.append(engine)
        return engine

    yield factory
    for engine in engines:
        await engine.stop()


def _overlay(base: EngineSupply, override: EngineSupply | None) -> EngineSupply:
    """``base`` with every field ``override`` supplies (anything not UNSET)."""
    if override is None:
        return base
    given = {f.name: getattr(override, f.name) for f in dataclasses.fields(override)}
    return dataclasses.replace(base, **{k: v for k, v in given.items() if v is not UNSET})
