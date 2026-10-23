"""Test doubles shipped with the package so builders can test without a funded signer.

The port fakes import contracts, errors and protocols only; supply them through
``BuilderEngine.create(settings, supply=EngineSupply(...))``. ``make_engine`` builds a real engine
over them (in-memory SQLite), and ``FakeEngine`` scripts the journey at the engine level.
"""

from livepeer_builder.testing.engine import make_engine
from livepeer_builder.testing.fake_engine import Fails, FakeEngine, FakeJobStream, Reply
from livepeer_builder.testing.fakes import (
    FakeAllocationManager,
    FakeUsageSource,
    FixedClock,
    MemorySecretStore,
    ScriptedStream,
    ScriptedStreamTransport,
    ScriptedTransport,
    StreamScript,
    runner,
)

__all__ = [
    "Fails",
    "FakeAllocationManager",
    "FakeEngine",
    "FakeJobStream",
    "FakeUsageSource",
    "FixedClock",
    "MemorySecretStore",
    "Reply",
    "ScriptedStream",
    "ScriptedStreamTransport",
    "ScriptedTransport",
    "StreamScript",
    "make_engine",
    "runner",
]
