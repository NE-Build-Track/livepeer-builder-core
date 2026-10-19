"""Discovery contracts: runners the signer advertises and the offerings built from them."""

from datetime import datetime
from decimal import Decimal

from livepeer_builder.contracts.common import Contract


class NetworkRate(Contract):
    """A rate as the orchestrator advertises it (wholesale network price, not retail)."""

    amount: Decimal
    currency: str  # "usd" today
    unit: str  # "hour" or "request"


class RunnerGPU(Contract):
    """The GPU a runner reports in its registration, as the node echoes it."""

    id: str | None = None
    name: str | None = None
    vram_mb: int | None = None


class Runner(Contract):
    url: str
    app: str
    runner_id: str | None
    mode: str | None  # "single-shot" or "persistent"
    orchestrator_url: str | None
    capacity: int | None
    capacity_available: int | None
    rate: NetworkRate | None
    # Echoed from the runner's registration (node discovery entry). The engine never interprets them.
    version: str | None = None
    metadata: str | None = None  # up to 1 KB of app-controlled text, often JSON (model limits, labels)
    gpu: RunnerGPU | None = None


class DiscoverySnapshot(Contract):
    runners: tuple[Runner, ...]
    observed_at: datetime | None  # None before the first successful poll
    last_attempt_at: datetime | None
    last_error: str | None


class Offering(Contract):
    """One runner app, as a builder sees it."""

    app: str
    modes: tuple[str, ...]
    rate_low: NetworkRate | None
    rate_high: NetworkRate | None
    runner_count: int
    ready_count: int
    observed_at: datetime
