"""Discovery: a cached snapshot of advertised runners, grouped into offerings by app."""

import asyncio
import logging
from collections import Counter
from collections.abc import Sequence
from datetime import datetime, timedelta

from livepeer_builder.contracts import (
    ActorContext,
    DiscoverySnapshot,
    JobRequest,
    NetworkRate,
    ObservedRate,
    Offering,
    Operation,
    Runner,
)
from livepeer_builder.protocols import AccessPolicy, Clock, DiscoverySource

_LOG = logging.getLogger(__name__)
# livepeer-node's collectors reject a stamp this far ahead (discovery/shared/inventory.go, ClockSkew).
# The engine is deliberately looser: such a rate is kept, with the poll time instead of the stamp.
_CLOCK_SKEW = timedelta(seconds=30)


class MostCapacityFirst:
    """Default SelectionPolicy: most free capacity first; unknown capacity counts as ready."""

    def order(self, candidates: Sequence[Runner], request: JobRequest) -> Sequence[Runner]:
        del request
        return sorted(candidates, key=_free_capacity, reverse=True)


class DiscoveryService:
    def __init__(
        self,
        source: DiscoverySource,
        policy: AccessPolicy,
        clock: Clock,
        interval_s: float = 15.0,
        rate_max_age_s: float = 600.0,
    ) -> None:
        self._source = source
        self._policy = policy
        self._clock = clock
        self._interval_s = interval_s
        self._rate_max_age = timedelta(seconds=rate_max_age_s)
        self._snapshot = DiscoverySnapshot(
            runners=(), observed_at=None, last_attempt_at=None, last_error=None
        )

    @property
    def snapshot(self) -> DiscoverySnapshot:
        return self._snapshot

    def is_fresh(self) -> bool:
        observed = self._snapshot.observed_at
        if observed is None:
            return False
        return self._clock.now() - observed <= timedelta(seconds=self._interval_s * 3)

    async def refresh(self) -> DiscoverySnapshot:
        attempted = self._clock.now()
        try:
            runners = await self._source.fetch()
        except Exception as exc:  # any source failure keeps the last good snapshot
            _LOG.warning("discovery failed", extra={"event": "discovery.failed", "error": str(exc)})
            self._snapshot = self._snapshot.model_copy(
                update={"last_attempt_at": attempted, "last_error": str(exc)}
            )
            return self._snapshot
        self._snapshot = DiscoverySnapshot(
            runners=runners, observed_at=attempted, last_attempt_at=attempted, last_error=None
        )
        return self._snapshot

    async def run(self, stop: asyncio.Event) -> None:
        """Refresh once per interval until ``stop`` is set."""
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._interval_s)
            except TimeoutError:
                await self.refresh()

    def candidates(self, app: str) -> list[Runner]:
        """Runners for an app. Internal: the job service calls this after its own policy check."""
        return [runner for runner in self._snapshot.runners if runner.app == app]

    def runners(self, actor: ActorContext, app: str) -> list[Runner]:
        self._policy.check(actor, Operation.DISCOVER)
        return self.candidates(app)

    def offerings(self, *, app: str | None = None) -> list[Offering]:
        """The public catalog: no actor, no permission. It names no runner URL and spends nothing (#5)."""
        observed = self._snapshot.observed_at
        if observed is None:
            return []
        by_app: dict[str, list[Runner]] = {}
        for runner in self._snapshot.runners:
            if app is None or runner.app == app:
                by_app.setdefault(runner.app, []).append(runner)
        offerings = []
        for name in sorted(by_app):
            group = by_app[name]
            advertised = [
                _observed(runner.rate, runner.observed_at, observed)
                for runner in group
                if runner.rate is not None
            ]
            rates = _comparable_rates(advertised)
            offerings.append(
                Offering(
                    app=name,
                    modes=tuple(sorted({runner.mode for runner in group if runner.mode})),
                    rate_low=rates[0] if rates else None,
                    rate_high=rates[-1] if rates else None,
                    rate_status="available" if rates else "missing",
                    stale=not self.is_fresh() or self._too_old(rates),
                    rates_excluded=len(advertised) - len(rates),
                    runner_count=len(group),
                    ready_count=sum(1 for runner in group if _is_ready(runner)),
                    observed_at=observed,
                )
            )
        return offerings

    def _too_old(self, rates: list[ObservedRate]) -> bool:
        """The oldest rate in the range is older than the max age: its source missed refreshes."""
        return any(self._clock.now() - rate.observed_at > self._rate_max_age for rate in rates)


def _free_capacity(runner: Runner) -> int:
    return 1 if runner.capacity_available is None else runner.capacity_available


def _is_ready(runner: Runner) -> bool:
    return runner.capacity_available is None or runner.capacity_available > 0


def _observed(rate: NetworkRate, stamp: datetime | None, polled_at: datetime) -> ObservedRate:
    """A rate with when it was last known: its orchestrator's stamp, else the poll that read it."""
    if stamp is None or stamp > polled_at + _CLOCK_SKEW:
        return ObservedRate(**rate.model_dump(), observed_at=polled_at, observed_by="engine")
    return ObservedRate(**rate.model_dump(), observed_at=stamp, observed_by="orchestrator")


def _comparable_rates(rates: list[ObservedRate]) -> list[ObservedRate]:
    """Rates in the group's most common (currency, unit), cheapest first; other units do not compare."""
    if not rates:
        return []
    common, _ = Counter((rate.currency, rate.unit) for rate in rates).most_common(1)[0]
    return sorted((rate for rate in rates if (rate.currency, rate.unit) == common), key=lambda r: r.amount)
