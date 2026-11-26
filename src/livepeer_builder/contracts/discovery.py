"""Discovery contracts: runners a ``/discovery`` document advertises, and the offerings built from them."""

import re
from datetime import datetime
from decimal import Decimal
from typing import Literal
from urllib.parse import urlsplit

import yarl
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
# A URL holds none of what is not text, and no space, which the HTTP client cannot send.
_UNSENDABLE = re.compile(f"[ {NOT_TEXT}]")
HTTP_SCHEMES = frozenset({"http", "https"})
MAX_APP = 200  # a job stores its app as ``store.schema.ID``, and ``JobRequest.app`` takes no longer one


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
    # None is a runner a supplied source handed over without a stamp: its stamp is not checked, and the
    # catalog times such a rate by the poll (D62 (3)). The HTTP reader skips a runner whose observed_at
    # it cannot convert, including one that omits the stamp.
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
    """Every text field of ``runner`` is text the engine can store and serve (#114, C12)."""
    price, gpu = runner.price_info or PriceInfo(), runner.gpu or RunnerGPU()
    optional = (runner.mode, runner.version, runner.metadata, price.currency, price.unit, gpu.id, gpu.name)
    return (
        is_text(runner.url)
        and is_text(runner.app)
        and is_text(runner.orchestrator_url or "")
        and all(is_optional_text(value) for value in optional)
    )


def is_callable_url(value: str) -> bool:
    """An ``http`` or ``https`` URL the engine can call as it is written.

    aiohttp, which makes every runner call, reads the URL with yarl. yarl must connect to the host
    ``urlsplit`` names, the one the payment-origin check compares, on a port from 1 to 65535. yarl folds
    a non-ASCII host, so ``①27.0.0.1`` would reach ``127.0.0.1``, and it refuses a backslash in the
    host: a host that is not ASCII is written by its IDNA name (``xn--``). The URL holds no ASCII space,
    control character or lone surrogate (``_UNSENDABLE``): it is valid text, and a space would split the
    request line. Another space character, such as U+00A0 or U+2028, is kept and percent-encoded when
    sent (``adapters/runner_address.py``). It has no userinfo, query or fragment: the engine appends
    each request's path to a runner URL, an orchestrator URL is compared with a payment's origin, and
    error text quotes both.
    """
    if not value or _UNSENDABLE.search(value):
        return False
    try:
        parts = urlsplit(value)
        host, _ = parts.hostname, parts.port  # .port raises for a port that is not a number
        dialed = yarl.URL(value)
    except ValueError:
        return False
    if parts.scheme not in HTTP_SCHEMES or not host or "@" in parts.netloc or "?" in value or "#" in value:
        return False
    return dialed.raw_host == host and 1 <= (dialed.port or 0) <= 65535


def is_app(value: str) -> bool:
    """Valid text of 1 to ``MAX_APP`` characters: an app a job can name and the store can keep."""
    return is_text(value) and 1 <= len(value) <= MAX_APP


def is_kept(runner: Runner) -> bool:
    """Whether the engine keeps ``runner``, whichever ``DiscoverySource`` handed it over (#158).

    ``DiscoveryService`` asks this of every runner a source returns. The built-in reader builds no
    runner that fails it: it asks the same functions of a record's ``url``, ``address`` and ``app``
    before it builds one, so each rule is written once. A source supplied through ``decorate`` builds
    its runners itself, and one that breaks a rule is skipped: its text (``has_storable_text``), a
    ``url`` the engine can call, an ``orchestrator_url`` that is absent or one the engine can call, and
    an ``app`` a job can name.

    Two of the reader's rules are not here, on purpose (Mike, 26 November 2026). ``observed_at`` is not
    checked: the catalog times a rate whose stamp it cannot use by the poll (D62 (3)). And
    ``orchestrator_url`` need not have the runner URL's origin: the reader drops an address that is a
    claim read off the network, and a source of the application's own is believed."""
    orchestrator = runner.orchestrator_url
    return (
        has_storable_text(runner)
        and is_callable_url(runner.url)
        and (orchestrator is None or is_callable_url(orchestrator))
        and is_app(runner.app)
    )


class UnreadableInventory(ValueError):
    """An answer with records and no runner the engine keeps (#114, C28): one it could not read, not
    an empty inventory. The refresh fails, so the last good snapshot stays. ``entries`` and ``runners``
    are what was skipped; a supplied source's answer has no entries."""

    def __init__(self, entries: int, runners: int) -> None:
        super().__init__(
            f"discovery response has no readable runner: {entries} entries and {runners} runners skipped"
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
