"""Discovery contracts: runners a ``/discovery`` document advertises, and the offerings built from them."""

import re
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import model_validator

from livepeer_builder.contracts.common import Contract, SourceFailureCode
from livepeer_builder.contracts.payments import NetworkRate

ObservedBy = Literal["orchestrator", "engine"]
# Text the engine cannot keep or serve (#114, C12): a control character (NUL among them, which PostgreSQL
# refuses), or a lone surrogate, which JSON can carry as ``\udXXX`` and UTF-8 cannot, so the catalog could
# not be written.
NOT_TEXT = r"\x00-\x1f\x7f-\x9f\ud800-\udfff"  # as a character class, for a pattern that adds to it
_INVALID_TEXT = re.compile(f"[{NOT_TEXT}]")
# An optional field keeps a tab, a newline and a carriage return: they can be stored and served, metadata
# is often JSON written over several lines, and the rate reader strips a stray newline itself.
_INVALID_OPTIONAL_TEXT = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\ud800-\udfff]")


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


class Runner(Contract):
    """One runner a discovery document advertises.

    ``address`` becomes ``orchestrator_url`` only when its origin matches ``url``; otherwise payment and
    failover follow the host the engine calls.
    """

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


def is_text(value: str) -> bool:
    """Valid Unicode text with no control character: what a runner's ``app`` and URLs must be."""
    return _INVALID_TEXT.search(value) is None


def is_optional_text(value: str | None) -> bool:
    """As ``is_text``, for a field a runner may leave out: a tab and a line break are text here."""
    return value is None or _INVALID_OPTIONAL_TEXT.search(value) is None


def has_storable_text(runner: Runner) -> bool:
    """Every text field of ``runner`` is text the engine can store and serve (#114, C12).

    The default reader builds no other runner. ``DiscoveryService`` asks this of every runner a source
    returns, so a supplied source's runner is held to the same rule."""
    price, gpu = runner.price_info or PriceInfo(), runner.gpu or RunnerGPU()
    optional = (runner.mode, runner.version, runner.metadata, price.currency, price.unit, gpu.id, gpu.name)
    return (
        is_text(runner.url)
        and is_text(runner.app)
        and is_text(runner.orchestrator_url or "")
        and all(is_optional_text(value) for value in optional)
    )


class DiscoverySnapshot(Contract):
    runners: tuple[Runner, ...]
    observed_at: datetime | None  # None before the first successful poll
    last_attempt_at: datetime | None
    last_error: SourceFailureCode | None  # a code, never an error's text (#113, C7)


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
