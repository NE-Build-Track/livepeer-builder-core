"""Test doubles shipped with the package so builders can test without a funded signer.

Imports contracts, errors and protocols only. Use them with ``BuilderEngine.from_settings(...)``
overrides; persistence tests use a real SQLite store (``sqlite+aiosqlite:///:memory:``).
"""

from livepeer_builder.testing.fakes import (
    FakeAllocationManager,
    FakeUsageSource,
    FixedClock,
    MemorySecretStore,
    ScriptedTransport,
    runner,
)

__all__ = [
    "FakeAllocationManager",
    "FakeUsageSource",
    "FixedClock",
    "MemorySecretStore",
    "ScriptedTransport",
    "runner",
]
