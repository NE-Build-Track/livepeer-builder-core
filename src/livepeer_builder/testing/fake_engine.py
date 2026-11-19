"""``FakeEngine``: the actor API (``ActorApi``) scripted at the engine level, for application unit tests.

It returns real contract values and raises real engine errors, so code under test cannot tell it from
the engine for the API's *shape*: run a job, read it back, stream it, see costs and the catalog.
Like the engine, a run records an attempt on the first runner for the app, a path with no leading
``/`` is an ``InvalidRequest``, a repeated
``operation_ref`` follows the engine's rules (D57): ``OperationConflict`` for a different request,
``OperationInProgress`` while its stream is open, a new job after a failure that never reached a
runner, ``OperationExists`` otherwise; a stream that fails part-way leaves the job ``uncertain``; and a
job's usage is stored once, a repeat answered by its quantities (#115, C20).
It does not model the money rules (admission, failover, payment); script those outcomes with
``Fails`` and ``Raises``, or use ``make_engine`` when they matter. A contract test runs the same
actor-API assertions against both.

Script replies per ``(app, path)``::

    engine = FakeEngine(
        runners=[runner("chat/llm")],
        replies={
            ("chat/llm", "/v1/chat"): Reply(content=b'{"answer": 42}', data={"answer": 42}),
            ("chat/llm", "/v1/busy"): Raises(NoCapacity("app is at its limit")),
        },
        streaming=True,
    )
"""

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from uuid import UUID

from livepeer_builder.contracts import (
    ActorContext,
    Allowance,
    CostSummary,
    EngineHealth,
    JobCost,
    Offering,
    Operation,
    ResourceRef,
    Runner,
)
from livepeer_builder.errors import NotFound
from livepeer_builder.services.costs import classify, summarize, summary_window
from livepeer_builder.services.ownership import sees_every_actor
from livepeer_builder.testing.fake_jobs import FakeJobs, FakeStreaming
from livepeer_builder.testing.fake_parts import FakeAllowance, FakeDiscovery
from livepeer_builder.testing.scripts import Fails, Raises, Reply
from livepeer_builder.utils import utc

__all__ = ["FakeAllowance", "FakeCosts", "FakeDiscovery", "FakeEngine", "FakeJobs", "FakeStreaming"]


class FakeCosts:
    """A scripted cost wins. Otherwise a job the fake ran costs what the engine reports before any usage
    arrives (``classify`` with no rows): ``pending`` while running or once paid, ``none`` if unpaid. An
    unscripted summary totals those costs the engine's way (``summarize``), in its window and scope."""

    def __init__(
        self, costs: Mapping[UUID, JobCost | Raises], summary: CostSummary | None, jobs: FakeJobs
    ) -> None:
        self.costs = dict(costs)
        self._summary = summary
        self._jobs = jobs

    async def for_job(self, actor: ActorContext, job_id: UUID) -> JobCost:
        self._jobs.policy.check(actor, Operation.COSTS_READ)  # before any lookup, as the engine (D44)
        scripted = self.costs.get(job_id)
        if isinstance(scripted, Raises):
            raise scripted.error
        if scripted is not None:
            return scripted
        try:
            job = self._jobs.owned(actor, Operation.COSTS_READ, job_id)
        except NotFound:
            raise NotFound(f"no cost for job {job_id}") from None
        return classify(job, ())

    async def summary(
        self,
        actor: ActorContext,
        start: datetime | None = None,
        end: datetime | None = None,
        actor_id: str | None = None,
    ) -> CostSummary:
        target = actor_id or (None if sees_every_actor(actor) else actor.actor_id)
        owner = ResourceRef(kind="costs", tenant_id=actor.tenant_id, owner_actor_id=target)
        self._jobs.policy.check(actor, Operation.COSTS_READ, owner)  # as the engine, before any lookup
        if self._summary is not None:
            return self._summary
        start, end = summary_window(datetime.now(UTC), start, end)  # the fake's jobs use the wall clock
        ran = self._jobs.in_scope(actor.tenant_id, target)
        since, until = utc(start), utc(end)  # as the store: a time with no offset is UTC (T25)
        return summarize(
            start, end, [(job.app, classify(job, ())) for job in ran if since <= utc(job.created_at) < until]
        )


class FakeEngine:
    """Implements ``ActorApi``; ``streaming=False`` gives an engine whose transport cannot stream.
    Like the engine, it refuses a second ``start()``, and a ``start()`` after ``stop()``."""

    def __init__(
        self,
        *,
        replies: Mapping[tuple[str, str], Reply | Fails | Raises] | None = None,
        runners: Iterable[Runner] = (),
        offerings: Iterable[Offering] = (),
        costs: Mapping[UUID, JobCost | Raises] | None = None,
        summary: CostSummary | None = None,
        allowance: Allowance | None = None,
        health: EngineHealth | None = None,
        streaming: bool = False,
    ) -> None:
        self.discovery = FakeDiscovery(offerings, runners)
        self.jobs = FakeJobs(replies or {}, streaming, self.discovery)
        self.costs = FakeCosts(costs or {}, summary, self.jobs)
        self.payments = FakeAllowance(allowance, self.jobs.policy)
        self._health = health or EngineHealth(
            ready=True,
            discovery_fresh=True,
            discovery_observed_at=None,
            discovery_error=None,
            cost_sync_at=None,
            cost_sync_error=None,
            running_jobs=0,
            version="fake",
        )
        self.started = False
        self.stopped = False

    def health(self) -> EngineHealth:
        return self._health

    async def start(self) -> None:
        if self.stopped:
            raise RuntimeError("engine is stopped; create a new one")
        if self.started:  # as the engine (#113, C27)
            raise RuntimeError(
                "engine is already started; stop() it, then create a new one to start again. "
                "To serve an engine you start yourself, pass create_app(engine, manage_lifecycle=False)"
            )
        self.started = True

    async def stop(self) -> None:
        self.stopped = True
