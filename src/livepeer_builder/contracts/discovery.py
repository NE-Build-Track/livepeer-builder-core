"""Discovery contracts: runners the signer advertises and the offerings built from them."""

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import Field

from livepeer_builder.contracts.common import Contract

# max_price pads a rate into a float ceiling for the signer; a larger price overflows or is no float
PRICE_LIMIT = Decimal("1e300")
# ... and the signer refuses a ceiling that is not positive. Below the normal float range the padded
# float rounds to 0.0, or below the price itself (7e-324 pads to 5e-324).
PRICE_FLOOR = Decimal("1e-300")


class NetworkRate(Contract):
    """A rate exactly as advertised: a wholesale network price per unit, not a job quote or a retail price."""

    amount: Decimal = Field(ge=PRICE_FLOOR, lt=PRICE_LIMIT)  # livepeer-node's price: wei on-chain
    currency: str  # "wei" on-chain
    unit: str  # "seconds" or "fixed"; go-livepeer's "720p-pixel-seconds" is kept but cannot be paid
    # livepeer-node's price_usd: the runner's registered USD per the same unit. Not an engine conversion;
    # absent from go-livepeer orchestrators. The wei amount is what is paid.
    price_usd: Decimal | None = None
    # who stated the rate; a field, so a later price oracle or operator override would be explicit
    source: Literal["orchestrator"] = "orchestrator"


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
    """One runner app in one (currency, unit), as a builder sees it. Runners with no rate are not listed."""

    app: str
    modes: tuple[str, ...]
    rate_low: ObservedRate  # the range's currency and unit are the offering's
    rate_high: ObservedRate
    # missing: the signer takes no ceiling in this currency and unit (only wei per seconds or fixed), so the
    # engine does not pay it
    rate_status: Literal["available", "missing"]
    # the snapshot is not fresh, or the oldest rate in the range is older than LPB_RATE_MAX_AGE_S
    stale: bool
    runner_count: int
    ready_count: int
    observed_at: datetime
