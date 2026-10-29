"""go-livepeer compatibility: how the engine reads a go-livepeer orchestrator's ``/discovery`` (v0.9.x).

go-livepeer is being retired in favour of livepeer/node (D2). Delete this file together with the "go-livepeer
compatibility" section of docs/contracts/engine.md. The records below follow go-livepeer's
``LiveRunnerDiscoveryRunner`` (ai/runner/live_runner.go, v0.9.1 and master): the price is always wei, there is no
``price_usd`` and no ``observed_at``, and per-pixel video uses the unit ``720p-pixel-seconds``.
"""

from collections.abc import AsyncIterator
from decimal import Decimal

import pytest

from livepeer_builder.adapters.sdk import SdkRunnerTransport, runners_from_entries
from livepeer_builder.adapters.static import StaticDiscoverySource
from livepeer_builder.contracts import JobRequest, NetworkRate
from livepeer_builder.errors import RunnerCallError
from livepeer_builder.services.discovery import DiscoveryService
from livepeer_builder.services.policy import DefaultPolicy
from livepeer_builder.testing import FixedClock
from tests.unit.livepeer_net import Net, start

APP = "example/echo"


@pytest.fixture
async def net() -> AsyncIterator[Net]:
    async for value in start():
        yield value


def go_livepeer(price_info: dict[str, object] | None) -> list[dict[str, object]]:
    """One go-livepeer orchestrator entry; ``price_info`` is omitted when None, as go-livepeer does."""
    runner: dict[str, object] = {
        "url": "https://orch.example:8935/apps/r1/app", "app": APP, "gpu": {"name": "H100"},
        "capacity": 2, "capacity_used": 0, "capacity_available": 2,
    }  # fmt: skip
    if price_info is not None:
        runner["price_info"] = price_info
    return [{"address": "https://orch.example:8935", "runners": [runner]}]


async def test_a_go_livepeer_rate_has_no_usd_figure_and_the_engines_age(clock: FixedClock) -> None:
    runners = runners_from_entries(go_livepeer({"price": "4492197255", "currency": "wei", "unit": "seconds"}))
    discovery = DiscoveryService(StaticDiscoverySource(runners), DefaultPolicy(), clock)
    await discovery.refresh()
    (offering,) = discovery.offerings()
    assert offering.rate_low is not None
    assert (offering.rate_low.amount, offering.rate_low.price_usd) == (Decimal(4492197255), None)
    assert (offering.rate_low.observed_at, offering.rate_low.observed_by) == (clock.now(), "engine")


async def test_a_per_pixel_rate_is_kept_as_advertised_but_never_paid(net: Net, clock: FixedClock) -> None:
    (parsed,) = runners_from_entries(
        go_livepeer({"price": "1", "currency": "wei", "unit": "720p-pixel-seconds"})
    )
    assert parsed.rate == NetworkRate(amount=Decimal(1), currency="wei", unit="720p-pixel-seconds")
    discovery = DiscoveryService(StaticDiscoverySource([parsed]), DefaultPolicy(), clock)
    await discovery.refresh()
    (offering,) = discovery.offerings()
    assert (
        offering.rate_low is not None and offering.rate_low.unit == "720p-pixel-seconds"
    )  # kept as advertised
    assert offering.rate_status == "missing"  # no rate the engine will pay
    runner = net.runner().model_copy(update={"rate": parsed.rate})
    with pytest.raises(RunnerCallError) as refused:
        await SdkRunnerTransport(net.url).call(runner, JobRequest(app=APP, path="/run"), "lpg_k")
    assert (refused.value.kind, refused.value.payment_sent) == ("payment_refused", False)
    assert net.signer_requests == []


async def test_a_runner_listed_before_its_price_feed_is_ready_is_never_signed_for(net: Net) -> None:
    """go-livepeer lists an on-chain runner with no price_info until its price converter is ready."""
    (parsed,) = runners_from_entries(go_livepeer(None))
    assert (parsed.rate, parsed.rate_error) == (None, None)
    runner = net.runner().model_copy(update={"rate": None})
    with pytest.raises(RunnerCallError) as refused:
        await SdkRunnerTransport(net.url).call(runner, JobRequest(app=APP, path="/run"), "lpg_k")
    assert (refused.value.kind, refused.value.payment_sent) == ("payment_refused", False)
    assert net.signer_requests == []
