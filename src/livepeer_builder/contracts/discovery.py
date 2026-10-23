"""Discovery contracts: the node's `/discover-orchestrators` document and the offerings built from it.

`DiscoveredOrchestrator` is the wire document `livepeer/node` returns from the signer's
`GET /discover-orchestrators` (`signer/service.go`) and that each orchestrator advertises, in part, from
`GET /discovery` (`orchestrator/registry.go`). `Runner` is that document reduced to what the engine
selects on.
"""

from datetime import datetime
from decimal import Decimal

from pydantic import ConfigDict, ValidationError, field_validator

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


class DiscoveredPriceInfo(Contract):
    """`price_info` as the node advertises it. `price` is a count of `currency` per `unit`.

    A blank `currency` or `unit` stays blank, as the SDK's own discovery parser leaves it, so
    `call_runner` applies its default payment type rather than one the engine guessed.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    price: Decimal
    price_usd: Decimal | None = None
    currency: str = ""
    unit: str = ""

    @field_validator("price", "price_usd", mode="before")
    @classmethod
    def _not_a_bool(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("price must be a decimal")
        return value

    @field_validator("price", "price_usd")
    @classmethod
    def _finite_non_negative(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and (not value.is_finite() or value < 0):
            raise ValueError("price must be a finite, non-negative decimal")
        return value

    @field_validator("currency", "unit", mode="before")
    @classmethod
    def _token(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else ""


class DiscoveredRunner(Contract):
    """One runner in a `/discover-orchestrators` entry (`discoveredRunner`). Unknown fields are ignored."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    url: str
    app: str
    version: str | None = None
    metadata: str | None = None
    gpu: RunnerGPU | None = None
    mode: str | None = None
    capacity: int | None = None
    capacity_used: int | None = None
    capacity_available: int | None = None
    price_info: DiscoveredPriceInfo | None = None
    runner_id: str | None = None
    session_ids: tuple[str, ...] | None = None

    @field_validator("price_info", mode="before")
    @classmethod
    def _unknown_price_is_absent(cls, value: object) -> object:
        """A price the node would not advertise is unknown, not a reason to drop the runner."""
        return _or_none(DiscoveredPriceInfo, value)

    @field_validator("gpu", mode="before")
    @classmethod
    def _unknown_gpu_is_absent(cls, value: object) -> object:
        """The GPU is descriptive: one that does not parse is left out, and the runner is kept."""
        if not isinstance(value, dict):
            return None
        return _or_none(RunnerGPU, {key: value[key] for key in ("id", "name", "vram_mb") if key in value})

    @field_validator("session_ids", mode="before")
    @classmethod
    def _session_ids(cls, value: object) -> object:
        if value is None:
            return None
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            return None
        return value


class DiscoveredOrchestrator(Contract):
    """One element of a `/discover-orchestrators` JSON list (`discoveredOrchestrator`).

    Unknown fields are ignored. A runner that does not parse is skipped and the orchestrator is kept,
    as the SDK's discovery filter skips a runner without a URL or app.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    address: str
    score: float = 1.0
    runners: tuple[DiscoveredRunner, ...] = ()
    last_seen: datetime | None = None

    @field_validator("runners", mode="before")
    @classmethod
    def _usable_runners(cls, value: object) -> list[DiscoveredRunner]:
        if not isinstance(value, list):
            return []  # the SDK drops an entry whose runners are not a list
        usable: list[DiscoveredRunner] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            url, app = item.get("url"), item.get("app")
            if not (isinstance(url, str) and url.strip() and isinstance(app, str) and app.strip()):
                continue
            runner = _or_none(DiscoveredRunner, item)
            if runner is not None:
                usable.append(runner)
        return usable


def _or_none[ModelT: Contract](model: type[ModelT], value: object) -> ModelT | None:
    if value is None:
        return None
    try:
        return model.model_validate(value)
    except ValidationError:
        return None


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
