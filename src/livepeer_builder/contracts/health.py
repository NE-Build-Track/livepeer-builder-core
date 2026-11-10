"""Operational contracts: ``BuilderEngine.health()`` (``GET /readyz``) and ``BuilderEngine.purge()``."""

from datetime import datetime

from livepeer_builder.contracts.common import Contract
from livepeer_builder.contracts.payments import AllocationMode


class EngineHealth(Contract):
    ready: bool
    discovery_fresh: bool
    discovery_observed_at: datetime | None
    discovery_error: str | None
    cost_sync_at: datetime | None
    cost_sync_error: str | None
    running_jobs: int
    allocation_mode: AllocationMode
    version: str


class PurgeReport(Contract):
    """What a retention purge deleted (docs/contracts/persistence.md#retention)."""

    jobs_deleted: int
    usage_rows_deleted: int
