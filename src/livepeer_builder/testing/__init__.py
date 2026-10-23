"""Test doubles shipped with the package so builders can test without a funded signer.

Imports contracts, errors and protocols only. Supply them through
``BuilderEngine.create(settings, supply=EngineSupply(...))``; persistence tests use a real SQLite store (``sqlite+aiosqlite:///:memory:``).
"""

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
    "FakeAllocationManager",
    "FakeUsageSource",
    "FixedClock",
    "MemorySecretStore",
    "ScriptedStream",
    "ScriptedStreamTransport",
    "ScriptedTransport",
    "StreamScript",
    "runner",
]
