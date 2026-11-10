"""ISS-10 rates: what a livepeer-node inventory advertises, and how the catalog reports its provenance and age."""

import json
import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from livepeer_builder import EngineSettings, EngineSupply, testing
from livepeer_builder.adapters.discovery_inventory import read_inventory
from livepeer_builder.adapters.runner_price import max_price
from livepeer_builder.contracts import NetworkRate, ObservedRate, Offering, PriceInfo, Runner
from livepeer_builder.contracts.payments import payable_rate
from livepeer_builder.services.discovery import DiscoveryService
from livepeer_builder.services.policy import DefaultPolicy
from livepeer_builder.testing import FixedClock, make_engine

_URL = "http://127.0.0.1:9/discovery"
_STAMP = "2026-10-22T18:05:00.123+01:00"


class _Listed:
    """A DiscoverySource that hands runners over as given. The HTTP reader is tested separately."""

    def __init__(self, runners: tuple[Runner, ...]) -> None:
        self._runners = runners

    async def fetch(self) -> tuple[Runner, ...]:
        return self._runners


def _document(**price_info: object) -> str:
    """One orchestrator entry with one runner advertising ``price_info``, as JSON text."""
    runner = {
        "url": "https://orch.example:8935/apps/r1/app",
        "app": "a/b",
        "observed_at": _STAMP,
        "price_info": price_info,
    }
    return json.dumps([{"address": "https://orch.example:8935", "runners": [runner]}])


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
        url=f"https://o/apps/{name}/app",
        app="a/b",
        mode="single-shot",
        orchestrator_url="https://o",
        capacity=1,
        capacity_available=1,
        price_info=PriceInfo(price=Decimal(amount), currency=currency, unit=unit),
        observed_at=observed_at,
    )


async def catalog(clock: FixedClock, *runners: Runner) -> list[Offering]:
    """The public catalog right after one poll at ``clock.now()``."""
    discovery = DiscoveryService(_Listed(runners), DefaultPolicy(), clock)
    await discovery.refresh()
    return discovery.offerings()


# -- parsing: a rate exactly as advertised ---------------------------------------------------------------


def test_no_price_info_is_kept_and_cannot_be_paid() -> None:
    """Off-chain registrations advertise no price (livepeer-node omits price_info). A null is not an object."""
    body = json.dumps(
        [
            {
                "address": "https://orch.example",
                "runners": [
                    {"url": "https://orch.example/apps/r1/app", "app": "a/b", "observed_at": _STAMP},
                    {
                        "url": "https://orch.example/apps/r2/app",
                        "app": "a/b",
                        "observed_at": _STAMP,
                        "price_info": None,
                    },
                ],
            }
        ]
    )
    (kept,) = read_inventory(body)
    assert kept.url.endswith("/r1/app") and kept.price_info is None and payable_rate(kept.price_info) is None


def test_a_price_the_schema_rejects_is_skipped() -> None:
    """A price string is not a JSON number. The runner is not kept, and the refresh does not fail."""
    assert read_inventory(_document(price="1", currency="wei", unit="seconds")) == ()


def test_a_price_of_0_is_a_free_rate() -> None:
    """0 is a price (free); only the engine's ceiling needs a positive one, and a 402 from it is refused."""
    (runner,) = read_inventory(_document(price=0, currency="wei", unit="seconds"))
    assert payable_rate(runner.price_info) == NetworkRate(amount=Decimal(0), currency="wei", unit="seconds")
    free = testing.runner(price="0")
    assert free.price_info is not None and free.price_info.price == 0


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
        ' "app": "a/b", "observed_at": "2026-10-22T18:05:00.123+01:00",'
        ' "price_info": {"price": 4492197255, "price_usd": 0.0000027777777777777777777777777777,'
        ' "currency": "wei", "unit": "seconds"}}]}]'
    )  # livepeer-node sends both prices as JSON numbers
    (runner,) = read_inventory(payload)
    assert runner.price_info is not None
    assert payable_rate(runner.price_info) == NetworkRate(
        amount=Decimal(4492197255),
        currency="wei",
        unit="seconds",
        price_usd=Decimal("0.0000027777777777777777777777777777"),
    )


def test_a_price_outside_the_ceiling_is_kept_and_cannot_be_paid() -> None:
    """The reader keeps the advertised number. The payment layer refuses one that cannot be a ceiling."""
    for token in ("-1", "1e-400", "1e-999999", "1e+300", "1e+1000000"):
        body = (
            '[{"address":"https://orch.example:8935","runners":[{"url":"https://orch.example:8935/apps/r1/app",'
            f'"app":"a/b","observed_at":"{_STAMP}","price_info":{{"price":{token},'
            '"currency":"wei","unit":"seconds"}}]}]'
        )
        (runner,) = read_inventory(body)
        assert runner.price_info is not None and runner.price_info.price == Decimal(token), token
        assert payable_rate(runner.price_info) is None, token


def test_an_unreadable_number_keeps_the_runner_and_cannot_be_paid() -> None:
    """A JSON number ``Decimal`` cannot hold is still a number. The runner stays; the price cannot be paid."""
    token = "1e" + "9" * 20
    (runner,) = read_inventory(
        '[{"address":"https://orch.example","runners":[{"url":"https://orch.example/app","app":"a/b",'
        f'"observed_at":"{_STAMP}","price_info":{{"price":{token},"currency":"wei","unit":"fixed"}}}}]}}]'
    )
    assert runner.price_info is not None and runner.price_info.price is None
    assert payable_rate(runner.price_info) is None


def test_a_per_pixel_unit_is_kept_as_advertised_and_has_no_ceiling() -> None:
    """The reader does not judge the unit. The signer takes no ceiling in one it does not pay."""
    (runner,) = read_inventory(_document(price=10, currency="WEI", unit="720p-Pixel-Seconds"))
    assert runner.price_info is not None
    assert (runner.price_info.currency, runner.price_info.unit) == ("WEI", "720p-Pixel-Seconds")
    rate = payable_rate(runner.price_info)
    assert rate == NetworkRate(amount=Decimal(10), currency="wei", unit="720p-pixel-seconds")
    assert max_price(rate) is None


def test_the_orchestrators_observed_at_is_read_in_utc() -> None:
    """A schema-valid RFC 3339 stamp is kept in UTC. One the schema rejects is skipped, not stored as None."""
    stamps = [
        "2026-10-29T02:05:55.123456789Z",
        "2026-10-29T04:05:55+02:00",
        "2026-10-29T02:05:55",  # no offset
        "yesterday",
    ]
    body = json.dumps(
        [
            {
                "address": "https://orch.example",
                "runners": [
                    {
                        "url": f"https://orch.example/apps/r{i}/app",
                        "app": "a/b",
                        "observed_at": stamp,
                    }
                    for i, stamp in enumerate(stamps)
                ],
            }
        ]
    )
    assert [runner.observed_at for runner in read_inventory(body)] == [
        datetime(2026, 10, 29, 2, 5, 55, 123456, tzinfo=UTC),  # nanoseconds truncated to microseconds
        datetime(2026, 10, 29, 2, 5, 55, tzinfo=UTC),
    ]


def test_a_stamp_outside_the_utc_range_is_unreadable_and_keeps_the_inventory() -> None:
    """``astimezone(UTC)`` raises OverflowError at the ends of the datetime range.

    One such stamp on one runner must not fail the refresh for every other runner. A cache that
    re-serves the text only checks that it is a JSON string.
    """
    body = json.dumps(
        [
            {
                "address": "https://orch.example",
                "runners": [
                    {
                        "url": "https://orch.example/apps/good/app",
                        "app": "a/b",
                        "observed_at": "2026-10-30T00:00:00Z",
                    },
                    {
                        "url": "https://orch.example/apps/low/app",
                        "app": "a/b",
                        "observed_at": "0001-01-01T00:00:00+01:00",
                    },
                    {
                        "url": "https://orch.example/apps/high/app",
                        "app": "a/b",
                        "observed_at": "9999-12-31T23:00:00-01:00",
                    },
                ],
            }
        ]
    )
    good, low, high = read_inventory(body)
    assert good.observed_at == datetime(2026, 10, 30, tzinfo=UTC)
    assert low.observed_at is None and high.observed_at is None
    assert (low.url, high.url) == ("https://orch.example/apps/low/app", "https://orch.example/apps/high/app")


# -- the catalog: provenance and age ---------------------------------------------------------------------


async def test_a_stamped_rate_was_observed_by_its_orchestrator(clock: FixedClock) -> None:
    stamp = clock.now() - timedelta(seconds=20)
    (offering,) = await catalog(clock, priced("10", stamp))
    assert offering.rate_low == ObservedRate(
        amount=Decimal(10), currency="wei", unit="seconds", observed_at=stamp, observed_by="orchestrator"
    )


async def test_an_unstamped_rate_was_observed_by_the_engines_poll(clock: FixedClock) -> None:
    """A runner a custom source hands over with no stamp is timed by the poll. The HTTP reader does not keep one."""
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
    """A custom DiscoverySource can hand over a naive datetime: its zone is unknown, so the poll time is used."""
    naive = (clock.now() - timedelta(seconds=20)).replace(tzinfo=None)
    (offering,) = await catalog(clock, priced("10", naive))
    assert offering.rate_low is not None
    assert (offering.rate_low.observed_at, offering.rate_low.observed_by) == (clock.now(), "engine")
    assert offering.stale is False


async def test_an_app_with_no_rate_is_listed_as_missing_with_no_range(clock: FixedClock) -> None:
    """A runner with no rate (off-chain, or a price_info that is not one) still serves jobs: it is listed (#77 review)."""
    unpriced = priced("10", name="free").model_copy(update={"app": "c/d", "price_info": None})
    unreadable = unpriced.model_copy(update={"url": "https://o/x"})
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
    unpriced = priced("10", name="free").model_copy(update={"price_info": None})
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
    discovery = DiscoveryService(_Listed((priced("10"), other)), DefaultPolicy(), clock, 15.0)
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
        source = _Listed(runners)
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
    assert EngineSettings.from_env({"LPB_DISCOVERY_URL": _URL}).rate_max_age_s == 600
    assert (
        EngineSettings.from_env({"LPB_DISCOVERY_URL": _URL, "LPB_RATE_MAX_AGE_S": "60"}).rate_max_age_s == 60
    )
    stamped = priced("10", clock.now() - timedelta(seconds=61))
    async with make_engine(runners=[stamped], supply=EngineSupply(clock=clock), rate_max_age_s=60) as engine:
        (offering,) = engine.discovery.offerings()
    assert offering.stale
