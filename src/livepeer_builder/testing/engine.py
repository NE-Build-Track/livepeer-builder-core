"""``make_engine``: a real engine over in-memory parts, for integration-level tests.

The real services run (money rules, idempotency, admission, failover, streaming lifecycle) over an
in-memory SQLite store and the shipped port fakes. Use it when the engine's rules matter to the test;
use ``FakeEngine`` for fast application unit tests that only need the actor API's shape.
"""

import dataclasses
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

from livepeer_builder.config import EngineSettings, generate_secret_key
from livepeer_builder.contracts import Runner
from livepeer_builder.runtime.assembly import UNSET, Decorate, EngineParts, EngineSupply
from livepeer_builder.runtime.engine import BuilderEngine
from livepeer_builder.testing.discovery import FAKE_DISCOVERY_URL, serve
from livepeer_builder.testing.fakes import (
    FakeAllocationManager,
    FakeUsageSource,
    FixedClock,
    ScriptedStreamTransport,
    runner,
)

MEMORY_URL = "sqlite+aiosqlite:///:memory:"


@asynccontextmanager
async def make_engine(
    *,
    runners: Sequence[Runner] = (),
    supply: EngineSupply | None = None,
    decorate: Decorate | None = None,
    **settings: Any,
) -> AsyncIterator[BuilderEngine]:
    """A real engine, not started, with one discovery snapshot taken; stopped on exit.

    Defaults: an in-memory store, ``per_actor`` payments with a fresh ``LPB_SECRET_KEY``, a
    ``FixedClock``, the given runners (one ``runner()`` if none), a ``ScriptedStreamTransport``, one
    ``FakeAllocationManager`` for allocations and the grant desk, and a ``FakeUsageSource``. ``supply``
    replaces any of them; ``**settings`` override ``EngineSettings`` fields. The background loops are not
    started, so tests stay deterministic.
    """
    values: dict[str, Any] = {
        "database_url": MEMORY_URL,
        "allocation_mode": "per_actor",
        "secret_key": generate_secret_key(),
        "log_json": False,
        "discovery_url": FAKE_DISCOVERY_URL,
        **settings,
    }
    manager = FakeAllocationManager()
    listed = tuple(runners) or (runner(),)
    install, client = serve(listed)

    def with_runners(parts: EngineParts) -> EngineParts:
        installed = install(parts)
        return decorate(installed) if decorate is not None else installed

    base = EngineSupply(
        clock=FixedClock(),
        transport=ScriptedStreamTransport(),
        allocation_manager=manager,
        grants_manager=manager,
        usage_source=FakeUsageSource(),
    )
    engine = await BuilderEngine.create(
        EngineSettings.model_validate(values), supply=overlay(base, supply), decorate=with_runners
    )
    try:
        await engine.discovery.refresh()
        yield engine
    finally:
        await client.aclose()
        await engine.stop()


def overlay(base: EngineSupply, override: EngineSupply | None) -> EngineSupply:
    """``base`` with every field ``override`` supplies (anything not ``UNSET``)."""
    if override is None:
        return base
    given = {field.name: getattr(override, field.name) for field in dataclasses.fields(override)}
    return dataclasses.replace(base, **{name: value for name, value in given.items() if value is not UNSET})
