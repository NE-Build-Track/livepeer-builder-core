"""Test doubles shipped with the package so builders can test without a funded signer.

The port fakes import contracts, errors and protocols only; supply them through
``BuilderEngine.create(settings, supply=EngineSupply(...))``. ``make_engine`` builds a real engine
over them (in-memory SQLite), and ``FakeEngine`` scripts the actor API at the engine level.
"""

from livepeer_builder.testing.engine import make_engine
from livepeer_builder.testing.fake_engine import FakeEngine
from livepeer_builder.testing.fake_stream import FakeJobStream
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
from livepeer_builder.testing.scripts import Fails, Raises, Reply

__all__ = [
    "Fails",
    "FakeAllocationManager",
    "FakeEngine",
    "FakeJobStream",
    "FakeUsageSource",
    "FixedClock",
    "MemorySecretStore",
    "Raises",
    "Reply",
    "ScriptedStream",
    "ScriptedStreamTransport",
    "ScriptedTransport",
    "StreamScript",
    "make_engine",
    "runner",
]
