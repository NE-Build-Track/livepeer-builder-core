"""Discovery contracts: runners a ``/discovery`` document advertises, and the offerings built from them."""

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import field_validator, model_validator

from livepeer_builder.contracts.common import Contract

# max_price pads a rate into a float ceiling for the signer; a larger price overflows or is no float.
# These checks belong to the payment layer (D63): the discovery reader keeps price_info as advertised.
PRICE_LIMIT = Decimal("1e300")
# The signer refuses a ceiling that is not positive. Below the normal float range the padded float rounds
# to 0.0, or below the price itself (7e-324 pads to 5e-324). 0 itself is a price (free): it needs no
# ceiling, and the engine never signs for it.
PRICE_FLOOR = Decimal("1e-300")
PRICE_RULE = "price must be 0, or from 1e-300 to below 1e300"


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
    """The GPU a runner reports. The engine stores ``id``, ``name`` and ``vram_mb`` and ignores the rest."""

    id: str | None = None
    name: str | None = None
    vram_mb: int | None = None


class PriceInfo(Contract):
    """``price_info`` as advertised. ``price`` and ``price_usd`` are exact decimals, all four fields optional.

    A JSON number ``Decimal`` cannot hold is absent here. The runner is still kept, and ``payable_rate``
    reports that the price cannot be paid.
    """

    price: Decimal | None = None
    price_usd: Decimal | None = None
    currency: str | None = None
    unit: str | None = None


def payable_rate(info: PriceInfo | None) -> NetworkRate | None:
    """The advertised price as a rate the catalog and the signer can use, or None when it cannot be paid."""
    if info is None or info.price is None or not info.currency or not info.unit:
        return None
    try:
        return NetworkRate(
            amount=info.price,
            currency=info.currency,
            unit=info.unit,
            price_usd=info.price_usd,
        )
    except ValueError:
        return None


class Runner(Contract):
    """One runner a discovery document advertises. ``address`` is kept as ``orchestrator_url``."""

    url: str
    app: str
    mode: str | None = None  # "single-shot" or "persistent"; other values are forwarded unchanged
    orchestrator_url: str | None = None
    capacity: int | None = None
    capacity_used: int | None = None
    capacity_available: int | None = None
    price_info: PriceInfo | None = None  # as advertised; payable_rate decides whether it can be paid
    # None is a runner a custom source handed over without a usable stamp. The HTTP reader skips a
    # runner whose observed_at it cannot convert, including one that omits the stamp.
    observed_at: datetime | None = None
    version: str | None = None
    metadata: str | None = None
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
