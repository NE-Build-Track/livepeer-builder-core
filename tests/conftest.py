"""Shared fixtures: a real SQLite store, a fixed clock, fakes, and an engine factory."""

import contextlib
import gc
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from typing import Any

import pytest
from livepeer_gateway.remote_signer import get_signer_info

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


@pytest.fixture(autouse=True)
def no_collection_inside_a_test() -> Iterator[None]:
    """Many tests measure real time in tenths of a second (stream deadlines, call timings, the engine's
    loops), and a full garbage collection stops the process for longer: 0.3 s and more late in the suite,
    about fifty times a run. One that fell inside a deadline failed the test, on whichever commit happened
    to put one there. The collector is off while any test runs, its fixtures included, and on between
    tests, where what the test made is collected."""
    gc.disable()
    try:
        yield
    finally:
        gc.enable()


@pytest.fixture(autouse=True)
def no_signer_identity_from_an_earlier_test() -> None:
    """The payment SDK remembers a signer's identity per URL for the life of the process. A test's fake
    signer listens on a port the system picks, and when that is a port an earlier test's signer had, the
    SDK does not ask who the signer is: a test that lists the calls it expects then fails, about once in
    some dozens of runs. Every test starts with nothing remembered."""
    get_signer_info.cache_clear()  # type: ignore[attr-defined]


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
