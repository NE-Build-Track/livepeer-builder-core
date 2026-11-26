"""Discovery: a cached snapshot of advertised runners, grouped into offerings by app."""

import asyncio
import logging
from collections.abc import Collection, Mapping, Sequence
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
from livepeer_builder.contracts.discovery import ObservedBy, UnreadableInventory, is_kept
from livepeer_builder.contracts.payments import is_payable, payable_rate, signable
from livepeer_builder.protocols import AccessPolicy, Clock, DiscoverySource
from livepeer_builder.services.failure_codes import source_failure

_LOG = logging.getLogger(__name__)
# livepeer-node's collectors reject a stamp this far ahead (discovery/shared/inventory.go, ClockSkew).
# The engine is deliberately looser: such a rate is kept, with the poll time instead of the stamp.
_CLOCK_SKEW = timedelta(seconds=30)


class MostCapacityFirst:
    """Default SelectionPolicy: most free capacity first; unknown capacity counts as one free slot. A
    runner advertising zero stays, tried last: the hint can lag, so the engine still asks (#114, C17)."""

    def order(self, candidates: Sequence[Runner], request: JobRequest) -> Sequence[Runner]:
        del request
        return sorted(candidates, key=_free_capacity, reverse=True)


def capped_first(runners: tuple[Runner, ...]) -> tuple[Runner, ...]:
    """The policy's order, free runners and those the signer takes a ceiling for first; the rest last."""
    return tuple(sorted(runners, key=lambda runner: not _paid_ready(runner)))  # stable: the policy's order


def paid_candidates(runners: Sequence[Runner]) -> list[Runner]:
    """The runners a job that pays may try (#114, C17): all of them, unless every one advertises zero
    capacity. livepeer/node before v1.0.7 asks for payment before it looks at capacity, so a full runner's
    refusal comes after the signer signed and the ticket is charged. From v1.0.7 it looks first, but it
    reserves no slot, and the engine cannot tell the versions apart. Then only the runners an attempt
    signs nothing for are left, and none left is the job's ``no_capacity``.

    With any runner that has room, a full one the job would pay stays, on purpose: the hint can lag, and
    it is the last resort when the others failed. Failover can then reach it and pay for its refusal,
    and ``capped_first`` can put it ahead of a runner with room whose price the signer does not take."""
    if any(_is_ready(runner) for runner in runners):
        return list(runners)
    return [runner for runner in runners if not _signs_for(runner)]


def _signs_for(runner: Runner) -> bool:
    """A paid attempt on this runner signs a payment: it advertises a price the signer takes a ceiling for.
    The engine signs for no other runner: a free one is never asked to pay, and a 402 from a runner with
    any other price, or none, is refused unsigned (D62)."""
    rate = payable_rate(runner.price_info)
    return rate is not None and signable(rate.amount, rate.currency, rate.unit)


def _paid_ready(runner: Runner) -> bool:
    # a free runner is never asked to pay; a 402 from it is still refused
    rate = payable_rate(runner.price_info)
    return rate is not None and (rate.amount == 0 or signable(rate.amount, rate.currency, rate.unit))


class NotRunners(TypeError):
    """What a source returned is not runners (#158): no collection of ``Runner`` objects. A supplied
    source builds its answer itself, so the engine checks it where it takes it. The refresh fails as
    it does when the source raises, and the last good snapshot stays. The text names the type that
    came and quotes nothing of it."""


def _runners(answer: object) -> tuple[Runner, ...]:
    """A source's answer as the runners it holds, or ``NotRunners``. A tuple or a list is what a source
    returns; any other sized collection of runners reads the same. Text, bytes and a mapping are none,
    empty ones included: they would read as an empty inventory and replace the snapshot."""
    if isinstance(answer, str | bytes | bytearray | Mapping) or not isinstance(answer, Collection):
        raise NotRunners(f"discovery source returned {_kind(answer)}, not a list of runners")
    items = tuple(answer)
    strangers = [item for item in items if not isinstance(item, Runner)]
    if strangers:
        raise NotRunners(
            f"discovery source returned {len(strangers)} of {len(items)} items that are not runners; "
            f"the first is {_kind(strangers[0])}"
        )
    return items


def _kind(value: object) -> str:
    return "None" if value is None else f"a {type(value).__name__}"


def _kept(runners: tuple[Runner, ...]) -> tuple[Runner, ...]:
    """The runners of a source's answer that the engine keeps (``is_kept``, #158): the reader's rules
    for a runner's text, URLs and ``app``, asked of every source. The built-in reader has skipped such a
    runner already; one a supplied source built is skipped here, and counted as the reader counts its
    own. With runners in the answer and none kept, it is an answer the engine could not read, as the
    reader's is (#114, C28): ``UnreadableInventory``, so the refresh fails and the snapshot stays. An
    answer with no runner in it is an empty inventory."""
    kept = tuple(runner for runner in runners if is_kept(runner))
    skipped = len(runners) - len(kept)
    if skipped:
        _LOG.warning(
            "discovery source returned runners the engine does not keep",
            extra={"event": "discovery.inventory_skipped", "entries": 0, "runners": skipped},
        )
        if not kept:
            raise UnreadableInventory(0, skipped)
    return kept


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
            kept = _kept(_runners(await self._source.fetch()))
        except Exception as exc:  # any failure keeps the last good snapshot: an answer that is not
            # runners (NotRunners), or one whose runners were all skipped (UnreadableInventory), too
            code = source_failure(exc)  # /readyz shows the code; the text is logged (#113, C7)
            _LOG.warning(
                "discovery failed", extra={"event": "discovery.failed", "code": code, "error": str(exc)}
            )
            self._snapshot = DiscoverySnapshot(  # built, not copied: a copy would not check the code
                runners=self._snapshot.runners,
                observed_at=self._snapshot.observed_at,
                last_attempt_at=attempted,
                last_error=code,
            )
            return self._snapshot
        self._snapshot = DiscoverySnapshot(
            runners=kept, observed_at=attempted, last_attempt_at=attempted, last_error=None
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
        # one group per app and (currency, unit); an app's runners with no rate are one more group, after them
        by_unit: dict[tuple[str, bool, str, str], list[Runner]] = {}
        for runner in self._snapshot.runners:
            if app is None or runner.app == app:
                rate = payable_rate(runner.price_info)
                key = (runner.app, False, rate.currency, rate.unit) if rate else (runner.app, True, "", "")
                by_unit.setdefault(key, []).append(runner)
        fresh = self.is_fresh()
        offerings = []
        for key in sorted(by_unit):
            name, _, currency, unit = key
            group = by_unit[key]
            span = _range(group, observed)
            offerings.append(
                Offering(
                    app=name,
                    modes=tuple(sorted({runner.mode for runner in group if runner.mode})),
                    rate_low=span[0] if span else None,
                    rate_high=span[1] if span else None,
                    rate_status="available" if span and is_payable(currency, unit) else "missing",
                    stale=not fresh or (span is not None and self._too_old(span[2])),
                    runner_count=len(group),
                    ready_count=sum(1 for runner in group if _is_ready(runner)),
                    observed_at=observed,
                )
            )
        return offerings

    def _too_old(self, oldest: datetime) -> bool:
        """The oldest rate in the range is older than the max age: its source missed refreshes."""
        return self._clock.now() - oldest > self._rate_max_age


def _free_capacity(runner: Runner) -> int:
    return 1 if runner.capacity_available is None else runner.capacity_available


def _is_ready(runner: Runner) -> bool:
    return runner.capacity_available is None or runner.capacity_available > 0


def _range(group: list[Runner], polled_at: datetime) -> tuple[ObservedRate, ObservedRate, datetime] | None:
    """The cheapest and dearest rate, and the oldest time any rate in the group was known; None with no rate.

    Two ``ObservedRate`` are built, not one per runner: the catalog is public, built on every request.
    """
    rated = [
        (rate, runner.observed_at)
        for runner in group
        if (rate := payable_rate(runner.price_info)) is not None
    ]
    if not rated:
        return None
    cheapest = min(rated, key=lambda pair: pair[0].amount)
    dearest = max(rated, key=lambda pair: pair[0].amount)
    oldest = min(_known(stamp, polled_at)[0] for _, stamp in rated)
    return _observed(*cheapest, polled_at), _observed(*dearest, polled_at), oldest


def _observed(rate: NetworkRate, stamp: datetime | None, polled_at: datetime) -> ObservedRate:
    observed_at, observed_by = _known(stamp, polled_at)
    return ObservedRate(**rate.model_dump(), observed_at=observed_at, observed_by=observed_by)


def _known(stamp: datetime | None, polled_at: datetime) -> tuple[datetime, ObservedBy]:
    """When a rate was last known, and whose clock says so: its orchestrator's stamp, else the poll time.

    A stamp with no UTC offset (a source other than HTTP can hand one over) is unreadable, as HTTP's are."""
    if stamp is None or stamp.utcoffset() is None or stamp > polled_at + _CLOCK_SKEW:
        return polled_at, "engine"
    return stamp, "orchestrator"
