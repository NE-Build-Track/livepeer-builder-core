"""ISS-10 rates: what a livepeer-node inventory advertises, and how the catalog reports its provenance and age."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from livepeer_builder import EngineSettings, EngineSupply
from livepeer_builder.adapters.sdk import runners_from_entries
from livepeer_builder.adapters.static import StaticDiscoverySource
from livepeer_builder.contracts import NetworkRate, ObservedRate, Offering, Runner
from livepeer_builder.services.discovery import DiscoveryService
from livepeer_builder.services.policy import DefaultPolicy
from livepeer_builder.testing import FixedClock, make_engine


def inventory(**price_info: object) -> list[dict[str, object]]:
    """One orchestrator entry with one runner advertising ``price_info``."""
    runner = {"url": "https://orch.example:8935/apps/r1/app", "app": "a/b", "price_info": price_info}
    return [{"address": "https://orch.example:8935", "runners": [runner]}]


def priced(
    amount: str, observed_at: datetime | None = None, *, unit: str = "seconds", name: str = "r1"
) -> Runner:
    """A runner advertising ``amount`` wei per ``unit``, stamped ``observed_at`` by its orchestrator."""
    return Runner(
        url=f"https://o/apps/{name}/app", app="a/b", runner_id=name, mode="single-shot",
        orchestrator_url="https://o", capacity=1, capacity_available=1,
        rate=NetworkRate(amount=Decimal(amount), currency="wei", unit=unit), observed_at=observed_at,
    )  # fmt: skip


async def catalog(clock: FixedClock, *runners: Runner) -> list[Offering]:
    """The public catalog right after one poll at ``clock.now()``."""
    discovery = DiscoveryService(StaticDiscoverySource(runners), DefaultPolicy(), clock)
    await discovery.refresh()
    return discovery.offerings()


# -- parsing: a rate exactly as advertised ---------------------------------------------------------------


def test_a_price_with_no_currency_is_not_a_rate() -> None:
    (runner,) = runners_from_entries(inventory(price="4492197255", unit="seconds"))
    assert (runner.rate, runner.rate_error) == (None, "price_info has no currency")


def test_a_price_with_no_unit_is_not_a_rate() -> None:
    (runner,) = runners_from_entries(inventory(price="4492197255", currency="wei"))
    assert (runner.rate, runner.rate_error) == (None, "price_info has no unit")


def test_no_price_info_is_no_rate_and_no_error() -> None:
    """Off-chain registrations advertise no price (livepeer-node omits price_info)."""
    entries = [{"address": "https://o", "runners": [
        {"url": "https://o/apps/r1/app", "app": "a/b"},
        {"url": "https://o/apps/r2/app", "app": "a/b", "price_info": None},
    ]}]  # fmt: skip
    assert [(r.rate, r.rate_error) for r in runners_from_entries(entries)] == [(None, None), (None, None)]


def test_an_unreadable_price_says_why() -> None:
    entries = [{"address": "https://o", "runners": [
        {"url": "https://o/apps/r1/app", "app": "a/b", "price_info": "4492197255"},
        {"url": "https://o/apps/r2/app", "app": "a/b", "price_info": {"price": "-1", "currency": "wei", "unit": "seconds"}},
        {"url": "https://o/apps/r3/app", "app": "a/b", "price_info": {"price": "NaN", "currency": "wei", "unit": "seconds"}},
        {"url": "https://o/apps/r4/app", "app": "a/b", "price_info": {"currency": "wei", "unit": "seconds"}},
    ]}]  # fmt: skip
    assert [(r.rate, r.rate_error) for r in runners_from_entries(entries)] == [
        (None, "price_info is not an object"),
        (None, "price is not a nonnegative number"),
        (None, "price is not a nonnegative number"),
        (None, "price is not a nonnegative number"),
    ]


def test_a_price_too_large_for_a_float_ceiling_is_not_a_rate() -> None:
    """max_price pads the amount into a float; 1E+1000000 overflowed before the signer was asked."""
    huge = Decimal("1E+1000000")
    (runner,) = runners_from_entries(inventory(price=huge, currency="wei", unit="seconds"))
    assert (runner.rate, runner.rate_error) == (None, "price is not a nonnegative number")
    (runner,) = runners_from_entries(inventory(price="10", price_usd=huge, currency="wei", unit="seconds"))
    assert runner.rate is not None and runner.rate.price_usd is None


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
    for bad in ("not-a-number", "-0.01", "Infinity", Decimal("NaN"), True):
        (runner,) = runners_from_entries(inventory(price="10", price_usd=bad, currency="wei", unit="fixed"))
        assert runner.rate == NetworkRate(amount=Decimal(10), currency="wei", unit="fixed"), bad
        assert runner.rate_error == "price_usd is not a nonnegative number", bad


def test_an_absent_price_usd_is_not_an_error() -> None:
    (runner,) = runners_from_entries(inventory(price="10", currency="wei", unit="fixed"))
    assert runner.rate_error is None


def test_the_orchestrators_observed_at_is_read_in_utc() -> None:
    """livepeer-node stamps each runner with RFC 3339 nanoseconds (Go's RFC3339Nano) at response time."""
    stamps = [
        "2026-10-29T02:05:55.123456789Z",
        "2026-10-29T04:05:55+02:00",
        "2026-10-29T02:05:55",
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
    ]


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


async def test_an_app_with_no_usable_rate_is_not_listed(clock: FixedClock) -> None:
    unpriced = priced("10", name="free").model_copy(update={"app": "c/d", "rate": None})
    unreadable = unpriced.model_copy(
        update={"rate_error": "price_info has no currency", "url": "https://o/x"}
    )
    (available,) = await catalog(clock, priced("10"), unpriced, unreadable)
    assert (available.app, available.rate_status) == ("a/b", "available")


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
    pixels, seconds = await catalog(
        clock,
        priced("1", unit="720p-pixel-seconds", name="p1"),
        priced("2", unit="720p-pixel-seconds", name="p2"),
        priced("10", name="s1"),
    )
    assert (seconds.rate_low.unit, seconds.rate_status, seconds.runner_count) == ("seconds", "available", 1)
    assert (pixels.rate_low.unit, pixels.rate_status, pixels.runner_count) == (
        "720p-pixel-seconds",
        "missing",
        2,
    )


async def test_lpb_rate_max_age_s_sets_the_max_age(clock: FixedClock) -> None:
    assert (
        EngineSettings.from_env({}).rate_max_age_s == 600
    )  # two of livepeer-node discovery's 5-minute refreshes
    assert EngineSettings.from_env({"LPB_RATE_MAX_AGE_S": "60"}).rate_max_age_s == 60
    stamped = priced("10", clock.now() - timedelta(seconds=61))
    async with make_engine(runners=[stamped], supply=EngineSupply(clock=clock), rate_max_age_s=60) as engine:
        (offering,) = engine.discovery.offerings()
    assert offering.stale
