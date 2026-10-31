"""Shared fixtures: a real SQLite store, a fixed clock, fakes, and an engine factory."""

import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import pytest

from livepeer_builder import BuilderEngine, EngineSupply
from livepeer_builder.contracts import ActorContext, Runner
from livepeer_builder.store import SqlStore
from livepeer_builder.testing import (
    FakeAllocationManager,
    FakeUsageSource,
    FixedClock,
    ScriptedStreamTransport,
    runner,
)
from livepeer_builder.testing import make_engine as make_shared_engine
from livepeer_builder.testing.engine import overlay

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
    """Builds engines with livepeer_builder.testing.make_engine over this test's shared fakes."""
    async with contextlib.AsyncExitStack() as engines:

        async def factory(
            runners: tuple[Runner, ...] = (runner(),), *, supply: EngineSupply | None = None, **settings: Any
        ) -> BuilderEngine:
            shared = EngineSupply(
                store=store,
                clock=clock,
                transport=transport,
                allocation_manager=manager,
                grants_manager=manager,
                usage_source=usage,
            )
            built = make_shared_engine(runners=runners, supply=overlay(shared, supply), **settings)
            return await engines.enter_async_context(built)

        yield factory
