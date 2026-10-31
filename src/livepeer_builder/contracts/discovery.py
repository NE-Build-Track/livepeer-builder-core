"""Discovery contracts: runners the signer advertises and the offerings built from them."""

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import field_validator, model_validator

from livepeer_builder.contracts.common import Contract

# max_price pads a rate into a float ceiling for the signer; a larger price overflows or is no float
PRICE_LIMIT = Decimal("1e300")
# ... and the signer refuses a ceiling that is not positive. Below the normal float range the padded
# float rounds to 0.0, or below the price itself (7e-324 pads to 5e-324). 0 itself is a price (free): it
# needs no ceiling, and the engine never signs for it.
PRICE_FLOOR = Decimal("1e-300")
PRICE_RULE = "price must be 0, or from 1e-300 to below 1e300"
# The most a /discovery inventory may be (LPB_DISCOVERY_MAX_BYTES). livepeer/node reads at most 1 MiB from
# one orchestrator (discovery/shared/inventory.go, MaxBody); a discovery service's combined inventory is many
# of those, and 1 MiB holds only about 2,000 runner records (#77 review)
DISCOVERY_MAX_BYTES = 16 << 20


def is_price(amount: Decimal) -> bool:
    """0 (free), or a positive price that pads into a float ceiling above it."""
    return amount.is_finite() and (amount == 0 or PRICE_FLOOR <= amount < PRICE_LIMIT)


class NetworkRate(Contract):
    """A rate exactly as advertised: a wholesale network price per unit, not a job quote or a retail price."""

    amount: Decimal  # livepeer-node's price: wei on-chain. PRICE_RULE, from any source
    currency: str  # "wei" on-chain
    unit: str  # as advertised; the engine pays only "seconds" and "fixed"
    # livepeer-node's price_usd: the runner's registered USD per the same unit. Not an engine conversion;
    # absent from go-livepeer orchestrators. The wei amount is what is paid.
    price_usd: Decimal | None = None
    # who stated the rate; a field, so a later price oracle or operator override would be explicit
    source: Literal["orchestrator"] = "orchestrator"

    @field_validator("amount")
    @classmethod
    def _a_price(cls, amount: Decimal) -> Decimal:
        if not is_price(amount):
            raise ValueError(PRICE_RULE)
        return Decimal(0) if amount == 0 else amount  # -0 and 0E-10 are 0

    @field_validator("currency", "unit")
    @classmethod
    def _normalized(cls, value: str) -> str:
        return (
            value.strip().lower()
        )  # once, here: grouping, is_payable and the signer's ceiling all compare it


ObservedBy = Literal["orchestrator", "engine"]


class ObservedRate(NetworkRate):
    """A rate as the catalog serves it: when it was last known, and whose clock says so."""

    observed_at: datetime
    observed_by: ObservedBy  # "orchestrator": its observed_at stamp; "engine": the poll that read it


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
    # why an advertised price_info is not a rate; None when it parsed or none was advertised. A bad price_usd
    # alone sets it while rate is still set and payable: only that field was dropped.
    rate_error: str | None = None
    # livepeer-node's observed_at stamp (tagged v1.0.3, released in v1.0.4): the orchestrator's response time,
    # kept by discovery caches. None from older nodes, go-livepeer, or an unreadable stamp.
    observed_at: datetime | None = None
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
    """One runner app in one (currency, unit), as a builder sees it. An app's runners with no rate are one
    more offering with no range: they still serve jobs, so the catalog lists them."""

    app: str
    modes: tuple[str, ...]
    rate_low: ObservedRate | None  # the range's currency and unit are the offering's; None: no rate
    rate_high: ObservedRate | None
    # missing: no rate (none advertised, or none readable), or the signer takes no ceiling in this currency
    # and unit (only wei per seconds or fixed), so the engine does not pay it
    rate_status: Literal["available", "missing"]
    # the snapshot is not fresh, or the oldest rate in the range is older than LPB_RATE_MAX_AGE_S
    stale: bool
    runner_count: int
    ready_count: int
    observed_at: datetime

    @model_validator(mode="after")
    def _available_has_a_range(self) -> "Offering":
        if self.rate_status == "available" and (self.rate_low is None or self.rate_high is None):
            raise ValueError("an available offering has a rate range")
        return self
