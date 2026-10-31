"""ISS-10 rates: what a livepeer-node inventory advertises, and how the catalog reports its provenance and age."""

import json
import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from livepeer_builder import EngineSettings, EngineSupply, testing
from livepeer_builder.adapters.runner_call import max_price
from livepeer_builder.adapters.sdk import runners_from_entries
from livepeer_builder.adapters.static import StaticDiscoverySource
from livepeer_builder.contracts import NetworkRate, ObservedRate, Offering, Runner
from livepeer_builder.contracts.discovery import PRICE_RULE
from livepeer_builder.services.discovery import DiscoveryService
from livepeer_builder.services.policy import DefaultPolicy
from livepeer_builder.testing import FixedClock, make_engine


def inventory(**price_info: object) -> list[dict[str, object]]:
    """One orchestrator entry with one runner advertising ``price_info``."""
    runner = {"url": "https://orch.example:8935/apps/r1/app", "app": "a/b", "price_info": price_info}
    return [{"address": "https://orch.example:8935", "runners": [runner]}]


def priced(
    amount: str,
    observed_at: datetime | None = None,
    *,
    unit: str = "seconds",
    name: str = "r1",
    currency: str = "wei",
) -> Runner:
    """A runner advertising ``amount`` ``currency`` per ``unit``, stamped ``observed_at`` by its orchestrator."""
    return Runner(
        url=f"https://o/apps/{name}/app", app="a/b", runner_id=name, mode="single-shot",
        orchestrator_url="https://o", capacity=1, capacity_available=1,
        rate=NetworkRate(amount=Decimal(amount), currency=currency, unit=unit), observed_at=observed_at,
    )  # fmt: skip


async def catalog(clock: FixedClock, *runners: Runner) -> list[Offering]:
    """The public catalog right after one poll at ``clock.now()``."""
    discovery = DiscoveryService(StaticDiscoverySource(runners), DefaultPolicy(), clock)
    await discovery.refresh()
    return discovery.offerings()


# -- parsing: a rate exactly as advertised ---------------------------------------------------------------


def test_no_price_info_is_no_rate_and_no_error() -> None:
    """Off-chain registrations advertise no price (livepeer-node omits price_info)."""
    entries = [{"address": "https://o", "runners": [
        {"url": "https://o/apps/r1/app", "app": "a/b"},
        {"url": "https://o/apps/r2/app", "app": "a/b", "price_info": None},
    ]}]  # fmt: skip
    assert [(r.rate, r.rate_error) for r in runners_from_entries(entries)] == [(None, None), (None, None)]


@pytest.mark.parametrize(
    ("price_info", "error"),
    [
        ("4492197255", "price_info is not an object"),
        ({"price": "4492197255", "unit": "seconds"}, "price_info has no currency"),  # never defaulted to usd
        ({"price": "4492197255", "currency": "wei"}, "price_info has no unit"),  # never defaulted to hour
        ({"currency": "wei", "unit": "seconds"}, PRICE_RULE),
        ({"price": "-1", "currency": "wei", "unit": "seconds"}, PRICE_RULE),
        ({"price": "NaN", "currency": "wei", "unit": "seconds"}, PRICE_RULE),
        # max_price pads the amount into a float: 1E+1000000 overflowed before the signer was asked, and
        # 1E-400 or 1E-999999 pad to 0.0, a ceiling livepeer/node's signer refuses (checkMaxPrice)
        ({"price": Decimal("1E+1000000"), "currency": "wei", "unit": "seconds"}, PRICE_RULE),
        ({"price": "1E-400", "currency": "wei", "unit": "seconds"}, PRICE_RULE),
        ({"price": "1E-999999", "currency": "wei", "unit": "seconds"}, PRICE_RULE),
    ],
)
def test_a_price_info_that_is_not_a_rate_says_why(price_info: object, error: str) -> None:
    runner = {"url": "https://o/apps/r1/app", "app": "a/b", "price_info": price_info}
    (parsed,) = runners_from_entries([{"address": "https://o", "runners": [runner]}])
    assert (parsed.rate, parsed.rate_error) == (None, error)


def test_a_price_of_0_is_a_free_rate() -> None:
    """0 is a price (free); only the engine's ceiling needs a positive one, and a 402 from it is refused (#77 review)."""
    free = NetworkRate(amount=Decimal(0), currency="wei", unit="seconds")
    (runner,) = runners_from_entries(inventory(price=0, currency="wei", unit="seconds"))
    assert (runner.rate, runner.rate_error) == (free, None)
    assert testing.runner(price="0").rate == free.model_copy(update={"unit": "fixed"})


def test_any_source_s_rate_is_refused_unless_a_float_ceiling_holds_it() -> None:
    """The contract, not only the JSON adapter: a custom DiscoverySource builds NetworkRate directly.

    9.99E-301 would survive as a float, but it is under the 1e-300 floor: below the normal range the
    pad can land under the price (7e-324 becomes 5e-324).
    """
    for bad in ("-1", "1E-400", "1E-999999", "9.99E-301", "1E+300", "1E+1000000"):
        with pytest.raises(ValidationError):
            NetworkRate(amount=Decimal(bad), currency="wei", unit="seconds")
    for bound in ("1E-300", "9.99E+299"):
        ceiling = max_price(NetworkRate(amount=Decimal(bound), currency="wei", unit="seconds"))
        assert ceiling is not None and math.isfinite(ceiling["price"]), bound
        assert Decimal(ceiling["price"]) > Decimal(bound), bound  # padded above the price, as a float


def test_any_source_s_currency_and_unit_are_read_lowercase_and_trimmed() -> None:
    """Normalized once, on the rate, so a custom DiscoverySource's "WEI"/" Seconds" groups and pays as wei/seconds."""
    rate = NetworkRate(amount=Decimal(2), currency=" WEI ", unit="Seconds ")
    assert (rate.currency, rate.unit) == ("wei", "seconds")


def test_a_node_on_chain_price_is_kept_as_advertised() -> None:
    """livepeer-node converts a registered 0.01 USD/hour into wei per second and keeps the USD figure per second."""
    payload = (
        '[{"address": "https://orch.example:8935", "runners": [{"url": "https://orch.example:8935/apps/r1/app",'
        ' "app": "a/b", "price_info": {"price": 4492197255, "price_usd": 0.0000027777777777777777777777777777,'
        ' "currency": "wei", "unit": "seconds"}}]}]'
    )  # livepeer-node sends both prices as JSON numbers
    (runner,) = runners_from_entries(json.loads(payload, parse_float=Decimal))
    assert runner.rate == NetworkRate(
        amount=Decimal(4492197255),
        currency="wei",
        unit="seconds",
        price_usd=Decimal("0.0000027777777777777777777777777777"),
    )


def test_a_bad_price_usd_drops_only_price_usd_and_says_so() -> None:
    for bad in ("not-a-number", "-0.01", "Infinity", Decimal("NaN"), True, Decimal("1E+1000000")):
        (runner,) = runners_from_entries(inventory(price="10", price_usd=bad, currency="wei", unit="fixed"))
        assert runner.rate == NetworkRate(amount=Decimal(10), currency="wei", unit="fixed"), bad
        assert runner.rate_error == "price_usd is not a nonnegative number", bad


def test_an_absent_price_usd_is_not_an_error() -> None:
    (runner,) = runners_from_entries(inventory(price="10", currency="wei", unit="fixed"))
    assert runner.rate_error is None


def test_a_per_pixel_unit_is_kept_and_has_no_ceiling() -> None:
    """go-livepeer advertises per-pixel video as wei per 720p-pixel-seconds. It is an unpaid unit, kept
    like any other: the catalog groups by unit, and the signer takes no ceiling in it."""
    (runner,) = runners_from_entries(inventory(price="10", currency="WEI", unit="720p-Pixel-Seconds"))
    assert runner.rate == NetworkRate(amount=Decimal(10), currency="wei", unit="720p-pixel-seconds")
    assert max_price(runner.rate) is None


def test_the_orchestrators_observed_at_is_read_in_utc() -> None:
    """livepeer-node stamps each runner with RFC 3339 nanoseconds (Go's RFC3339Nano) at response time."""
    stamps = [
        "2026-10-29T02:05:55.123456789Z",
        "2026-10-29T04:05:55+02:00",
        "2026-10-29T02:05:55",
        "20261029T020555Z",  # ISO 8601 basic format: fromisoformat reads it, RFC 3339 does not
        "2026-10-29 02:05:55Z",  # a space for the T: RFC 3339 allows it only by agreement, Go does not write it
        "yesterday",
        17,
    ]
    entries = [{"address": "https://o", "runners": [
        {"url": f"https://o/apps/r{i}/app", "app": "a/b", "observed_at": stamp} for i, stamp in enumerate(stamps)
    ]}]  # fmt: skip
    assert [r.observed_at for r in runners_from_entries(entries)] == [
        datetime(2026, 10, 29, 2, 5, 55, 123456, tzinfo=UTC),  # nanoseconds truncated to microseconds
        datetime(2026, 10, 29, 2, 5, 55, tzinfo=UTC),
        None,  # no offset: not RFC 3339
        None,
        None,
        None,
        None,
    ]


def test_a_stamp_outside_the_utc_range_is_unreadable_and_keeps_the_inventory() -> None:
    """``astimezone(UTC)`` raises OverflowError at the ends of the datetime range.

    One such stamp on one runner must not fail the refresh for every other runner. ``lpb discovery``
    re-serves the text: the probe only checks that it is a JSON string.
    """
    entries = [{"address": "https://o", "runners": [
        {"url": "https://o/apps/good/app", "app": "a/b", "observed_at": "2026-10-30T00:00:00Z"},
        {"url": "https://o/apps/low/app", "app": "a/b", "observed_at": "0001-01-01T00:00:00+01:00"},
        {"url": "https://o/apps/high/app", "app": "a/b", "observed_at": "9999-12-31T23:00:00-01:00"},
    ]}]  # fmt: skip
    good, low, high = runners_from_entries(entries)
    assert good.observed_at == datetime(2026, 10, 30, tzinfo=UTC)
    assert low.observed_at is None and high.observed_at is None
    assert (low.url, high.url) == ("https://o/apps/low/app", "https://o/apps/high/app")


# -- the catalog: provenance and age ---------------------------------------------------------------------


async def test_a_stamped_rate_was_observed_by_its_orchestrator(clock: FixedClock) -> None:
    stamp = clock.now() - timedelta(seconds=20)
    (offering,) = await catalog(clock, priced("10", stamp))
    assert offering.rate_low == ObservedRate(
        amount=Decimal(10), currency="wei", unit="seconds", observed_at=stamp, observed_by="orchestrator"
    )


async def test_an_unstamped_rate_was_observed_by_the_engines_poll(clock: FixedClock) -> None:
    """livepeer-node v1.0.2 (most of today's fleet, #70) and go-livepeer send no observed_at."""
    (offering,) = await catalog(clock, priced("10"))
    assert offering.rate_low is not None
    assert (offering.rate_low.observed_at, offering.rate_low.observed_by) == (clock.now(), "engine")


async def test_a_stamp_from_the_future_is_trusted_only_within_30_seconds(clock: FixedClock) -> None:
    """livepeer-node's own collectors allow 30 s of clock skew (discovery/shared/inventory.go, ClockSkew)."""
    skewed = clock.now() + timedelta(seconds=30)
    too_far = clock.now() + timedelta(seconds=30, microseconds=1)
    (offering,) = await catalog(clock, priced("10", skewed, name="r1"), priced("20", too_far, name="r2"))
    assert offering.rate_low is not None and offering.rate_high is not None
    assert (offering.rate_low.observed_at, offering.rate_low.observed_by) == (skewed, "orchestrator")
    assert (offering.rate_high.observed_at, offering.rate_high.observed_by) == (clock.now(), "engine")


async def test_a_stamp_with_no_utc_offset_was_observed_by_the_engines_poll(clock: FixedClock) -> None:
    """A source other than HTTP (StaticDiscoverySource, a custom one) can hand over a naive datetime: its zone
    is unknown, so it is unreadable, not a reason to fail the catalog."""
    naive = (clock.now() - timedelta(seconds=20)).replace(tzinfo=None)
    (offering,) = await catalog(clock, priced("10", naive))
    assert offering.rate_low is not None
    assert (offering.rate_low.observed_at, offering.rate_low.observed_by) == (clock.now(), "engine")
    assert offering.stale is False


async def test_an_app_with_no_rate_is_listed_as_missing_with_no_range(clock: FixedClock) -> None:
    """A runner with no rate (off-chain, or a price_info that is not one) still serves jobs: it is listed (#77 review)."""
    unpriced = priced("10", name="free").model_copy(update={"app": "c/d", "rate": None})
    unreadable = unpriced.model_copy(
        update={"rate_error": "price_info has no currency", "url": "https://o/x"}
    )
    available, missing = await catalog(clock, priced("10"), unpriced, unreadable)
    assert (available.app, available.rate_status) == ("a/b", "available")
    assert (missing.app, missing.rate_status, missing.rate_low, missing.rate_high) == (
        "c/d",
        "missing",
        None,
        None,
    )
    assert (missing.runner_count, missing.ready_count, missing.stale) == (2, 2, False)


async def test_an_app_s_runners_with_no_rate_are_a_missing_offering_after_its_priced_ones(
    clock: FixedClock,
) -> None:
    """Every runner of the app is counted in exactly one of its offerings."""
    unpriced = priced("10", name="free").model_copy(update={"rate": None})
    seconds, missing = await catalog(clock, unpriced, priced("10", name="r1"), priced("30", name="r2"))
    assert (seconds.rate_low.amount, seconds.rate_high.amount, seconds.runner_count) == (10, 30, 2)
    assert (missing.rate_low, missing.rate_status, missing.runner_count) == (None, "missing", 1)


async def test_an_offering_builds_two_observed_rates_however_many_runners_it_has(
    clock: FixedClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """10(c): the catalog is public and built on every request, so its cost must not grow with the fleet."""
    built: list[ObservedRate] = []

    def counted(**fields: object) -> ObservedRate:
        built.append(ObservedRate(**fields))  # type: ignore[arg-type]
        return built[-1]

    monkeypatch.setattr("livepeer_builder.services.discovery.ObservedRate", counted)
    runners = [priced(str(10 + n), name=f"r{n}") for n in range(50)]
    (offering,) = await catalog(clock, *runners)
    assert offering.runner_count == 50
    assert built == [offering.rate_low, offering.rate_high]


def test_an_offering_is_not_available_without_a_rate() -> None:
    with pytest.raises(ValidationError, match="an available offering has a rate range"):
        Offering(
            app="a/b", modes=(), rate_low=None, rate_high=None, rate_status="available", stale=False,
            runner_count=1, ready_count=1, observed_at=datetime(2026, 10, 30, tzinfo=UTC),
        )  # fmt: skip


async def test_a_free_runner_is_a_rate_of_0_in_the_catalog(clock: FixedClock) -> None:
    (offering,) = await catalog(clock, priced("0"))
    assert (offering.rate_low.amount, offering.rate_high.amount, offering.rate_status) == (0, 0, "available")


async def test_every_offering_is_stale_once_the_snapshot_is(clock: FixedClock) -> None:
    """The snapshot counts as stale after three missed polls (15 s each here), whatever the rates say."""
    other = priced("10").model_copy(update={"app": "c/d"})
    discovery = DiscoveryService(StaticDiscoverySource([priced("10"), other]), DefaultPolicy(), clock, 15.0)
    await discovery.refresh()
    clock.advance(45)
    assert [offering.stale for offering in discovery.offerings()] == [False, False]
    clock.advance(0.001)
    assert [offering.stale for offering in discovery.offerings()] == [True, True]


async def test_an_offering_is_stale_when_its_oldest_rate_is_older_than_the_max_age(clock: FixedClock) -> None:
    """A discovery cache keeps the orchestrator's stamp, so an old stamp means an inventory it could not refresh."""
    max_age = 600.0
    at_limit = priced("10", clock.now() - timedelta(seconds=max_age), name="r1")
    past_limit = priced("20", clock.now() - timedelta(seconds=max_age, milliseconds=1), name="r2")

    async def stale(*runners: Runner) -> bool:
        source = StaticDiscoverySource(runners)
        discovery = DiscoveryService(source, DefaultPolicy(), clock, rate_max_age_s=max_age)
        await discovery.refresh()
        (offering,) = discovery.offerings()
        return offering.stale

    assert await stale(at_limit, priced("30", clock.now(), name="r3")) is False
    assert await stale(past_limit, priced("30", clock.now(), name="r3")) is True


async def test_each_unit_of_an_app_is_its_own_offering(clock: FixedClock) -> None:
    fixed, seconds = await catalog(
        clock, priced("10", name="r1"), priced("30", name="r2"), priced("1", unit="fixed", name="r3")
    )
    assert (seconds.rate_low.unit, seconds.rate_low.amount, seconds.rate_high.amount) == ("seconds", 10, 30)
    assert (fixed.rate_low.unit, fixed.rate_low.amount, fixed.runner_count) == ("fixed", 1, 1)


async def test_an_unpaid_unit_does_not_hide_a_paid_one(clock: FixedClock) -> None:
    hourly, seconds = await catalog(
        clock,
        priced("1", unit="hour", name="h1"),
        priced("2", unit="hour", name="h2"),
        priced("10", name="s1"),
    )
    assert (seconds.rate_low.unit, seconds.rate_status, seconds.runner_count) == ("seconds", "available", 1)
    assert (hourly.rate_low.unit, hourly.rate_status, hourly.runner_count) == ("hour", "missing", 2)


@pytest.mark.parametrize(
    ("currency", "unit", "status"),
    [
        ("wei", "seconds", "available"),  # livepeer/node's signer takes a wei ceiling (checkMaxPrice) ...
        ("wei", "fixed", "available"),
        ("wei", "hour", "missing"),  # ... per second for a live payment, per request for a fixed one
        ("usd", "seconds", "missing"),
    ],
)
async def test_an_offering_is_available_only_in_a_currency_and_unit_the_signer_takes(
    clock: FixedClock, currency: str, unit: str, status: str
) -> None:
    (offering,) = await catalog(clock, priced("10", currency=currency, unit=unit))
    assert offering.rate_status == status


async def test_lpb_rate_max_age_s_sets_the_max_age(clock: FixedClock) -> None:
    assert (
        EngineSettings.from_env({}).rate_max_age_s == 600
    )  # two of livepeer-node discovery's 5-minute refreshes
    assert EngineSettings.from_env({"LPB_RATE_MAX_AGE_S": "60"}).rate_max_age_s == 60
    stamped = priced("10", clock.now() - timedelta(seconds=61))
    async with make_engine(runners=[stamped], supply=EngineSupply(clock=clock), rate_max_age_s=60) as engine:
        (offering,) = engine.discovery.offerings()
    assert offering.stale
