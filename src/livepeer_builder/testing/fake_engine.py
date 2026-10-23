"""``FakeEngine``: the journey (``JourneyApi``) scripted at the engine level, for application unit tests.

It returns real contract values and raises real engine errors, so code under test cannot tell it from
the engine for the journey's *shape*: run a job, read it back, stream it, see costs and the catalog.
It does not model the engine's money rules (idempotency, admission, failover, payment). Use
``make_engine`` when those matter. A contract test runs the same journey assertions against both.

Script replies per ``(app, path)``::

    engine = FakeEngine(
        runners=[runner("chat/llm")],
        replies={("chat/llm", "/v1/chat"): Reply(content=b'{"answer": 42}')},
        streaming=True,
    )
"""

from collections.abc import AsyncGenerator, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from types import TracebackType
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from livepeer_builder.contracts import (
    ActorContext,
    Allowance,
    CostSummary,
    EngineHealth,
    Failure,
    FailureKind,
    Job,
    JobCost,
    JobRequest,
    JobResult,
    JobState,
    Offering,
    Runner,
)
from livepeer_builder.errors import InvalidRequest, JobFailed, NotFound, NotProvisioned

if TYPE_CHECKING:
    from livepeer_builder.api import JourneyApi


@dataclass(frozen=True)
class Reply:
    """A successful runner reply. ``chunks`` is what a stream yields."""

    content: bytes = b'{"ok": true}'
    status_code: int = 200
    content_type: str = "application/json"
    chunks: tuple[bytes, ...] = (b'{"ok": true}',)


@dataclass(frozen=True)
class Fails:
    """The job fails: ``run`` and ``stream`` raise ``JobFailed`` with this failure."""

    kind: FailureKind = "runner_error"
    message: str = "scripted failure"
    status_code: int | None = 500


class FakeJobStream:
    """A ``JobStreamApi`` over scripted chunks. Records ``aclose`` and ``disconnected``."""

    def __init__(self, job: Job, reply: Reply) -> None:
        self._job, self._reply = job, reply
        self.closed = False
        self.disconnected_called = False

    @property
    def job(self) -> Job:
        return self._job

    @property
    def status_code(self) -> int:
        return self._reply.status_code

    @property
    def content_type(self) -> str:
        return self._reply.content_type

    def __aiter__(self) -> AsyncGenerator[bytes, None]:
        return self._iterate()

    async def _iterate(self) -> AsyncGenerator[bytes, None]:
        for chunk in self._reply.chunks:
            if self.closed:
                return
            yield chunk

    async def aclose(self) -> None:
        self.closed = True

    async def disconnected(self) -> None:
        self.disconnected_called = True

    async def __aenter__(self) -> "FakeJobStream":
        return self

    async def __aexit__(
        self, kind: type[BaseException] | None, error: BaseException | None, trace: TracebackType | None
    ) -> None:
        await self.aclose()


class FakeStreaming:
    def __init__(self, jobs: "FakeJobs") -> None:
        self._jobs = jobs

    async def stream(self, actor: ActorContext, request: JobRequest) -> FakeJobStream:
        reply = self._jobs.reply_for(actor, request)
        return FakeJobStream(self._jobs.record(actor, request, "running", None), reply)


class FakeJobs:
    def __init__(self, replies: Mapping[tuple[str, str], Reply | Fails], streaming: bool) -> None:
        self.replies = dict(replies)
        self.requests: list[tuple[ActorContext, JobRequest]] = []
        self.usage_reports: list[tuple[UUID, dict[str, int]]] = []
        self.streaming: FakeStreaming | None = FakeStreaming(self) if streaming else None
        self._jobs: dict[UUID, Job] = {}
        self._results: dict[UUID, JobResult] = {}

    def reply_for(self, actor: ActorContext, request: JobRequest) -> Reply:
        self.requests.append((actor, request))
        scripted = self.replies.get((request.app, request.path), Reply())
        if isinstance(scripted, Fails):
            failure = Failure(kind=scripted.kind, message=scripted.message, status_code=scripted.status_code)
            raise JobFailed(self.record(actor, request, "failed", failure))
        return scripted

    def record(
        self, actor: ActorContext, request: JobRequest, state: JobState, failure: Failure | None
    ) -> Job:
        now = datetime.now(UTC)
        job = Job(
            id=uuid4(),
            actor_id=actor.actor_id,
            application_id=actor.application_id,
            operation_ref=request.operation_ref,
            mode="immediate",
            app=request.app,
            capability=None,
            model=None,
            state=state,
            attempts=(),
            failure=failure,
            result=None,
            usage=None,
            created_at=now,
            completed_at=None if state == "running" else now,
        )
        self._jobs[job.id] = job
        return job

    async def run(self, actor: ActorContext, request: JobRequest) -> JobResult:
        reply = self.reply_for(actor, request)
        job = self.record(actor, request, "succeeded", None)
        result = JobResult(
            job=job,
            status_code=reply.status_code,
            content=reply.content,
            content_type=reply.content_type,
            data=None,
        )
        self._results[job.id] = result
        return result

    async def get(self, actor: ActorContext, job_id: UUID) -> Job:
        job = self._jobs.get(job_id)
        if job is None or (job.actor_id, job.application_id) != (actor.actor_id, actor.application_id):
            raise NotFound(f"job {job_id} not found")
        return job

    async def list_jobs(
        self, actor: ActorContext, *, limit: int = 50, before: datetime | None = None
    ) -> Sequence[Job]:
        mine = [
            job
            for job in self._jobs.values()
            if (job.actor_id, job.application_id) == (actor.actor_id, actor.application_id)
            and (before is None or job.created_at < before)
        ]
        return sorted(mine, key=lambda job: job.created_at, reverse=True)[:limit]

    async def result(self, actor: ActorContext, job_id: UUID) -> JobResult:
        await self.get(actor, job_id)
        if job_id not in self._results:
            raise NotFound(f"job {job_id} has no result")
        return self._results[job_id]

    async def report_usage(self, actor: ActorContext, job_id: UUID, quantities: Mapping[str, int]) -> Job:
        job = await self.get(actor, job_id)
        if job.state != "succeeded":
            raise InvalidRequest("usage can only be reported for a succeeded job")
        if any(value < 0 for value in quantities.values()):
            raise InvalidRequest("usage quantities must be non-negative")
        self.usage_reports.append((job_id, dict(quantities)))
        return job


class FakeCosts:
    def __init__(self, costs: Mapping[UUID, JobCost], summary: CostSummary | None) -> None:
        self.costs = dict(costs)
        self._summary = summary

    async def for_job(self, actor: ActorContext, job_id: UUID) -> JobCost:
        if job_id not in self.costs:
            raise NotFound(f"no cost for job {job_id}")
        return self.costs[job_id]

    async def summary(
        self, actor: ActorContext, start: datetime, end: datetime, actor_id: str | None = None
    ) -> CostSummary:
        if self._summary is None:
            raise NotFound("no cost summary scripted")
        return self._summary


class FakeDiscovery:
    def __init__(self, offerings: Iterable[Offering], runners: Iterable[Runner]) -> None:
        self._offerings, self._runners = list(offerings), list(runners)

    def offerings(self, actor: ActorContext, *, app: str | None = None) -> list[Offering]:
        return [offering for offering in self._offerings if app is None or offering.app == app]

    def runners(self, actor: ActorContext, app: str) -> list[Runner]:
        return self.candidates(app)

    def candidates(self, app: str) -> list[Runner]:
        return [runner for runner in self._runners if runner.app == app]


class FakeAllowance:
    def __init__(self, allowance: Allowance | None) -> None:
        self._allowance = allowance

    async def allowance(self, actor: ActorContext, actor_id: str | None = None) -> Allowance:
        if self._allowance is None:
            raise NotProvisioned("no allowance scripted")
        return self._allowance


class FakeEngine:
    """Implements ``JourneyApi``; ``streaming=False`` gives an engine whose transport cannot stream."""

    def __init__(
        self,
        *,
        replies: Mapping[tuple[str, str], Reply | Fails] | None = None,
        runners: Iterable[Runner] = (),
        offerings: Iterable[Offering] = (),
        costs: Mapping[UUID, JobCost] | None = None,
        summary: CostSummary | None = None,
        allowance: Allowance | None = None,
        health: EngineHealth | None = None,
        streaming: bool = False,
    ) -> None:
        self.jobs = FakeJobs(replies or {}, streaming)
        self.costs = FakeCosts(costs or {}, summary)
        self.discovery = FakeDiscovery(offerings, runners)
        self.payments = FakeAllowance(allowance)
        self._health = health or EngineHealth(
            ready=True,
            discovery_fresh=True,
            discovery_observed_at=None,
            discovery_error=None,
            cost_sync_at=None,
            cost_sync_error=None,
            running_jobs=0,
            allocation_mode="per_actor",
            version="fake",
        )
        self.started = False
        self.stopped = False

    def health(self) -> EngineHealth:
        return self._health

    async def start(self) -> None:
        if self.stopped:
            raise RuntimeError("engine is stopped; create a new one")
        self.started = True

    async def stop(self) -> None:
        self.stopped = True


if TYPE_CHECKING:  # mypy proves the fake offers the journey

    def _journey(fake: FakeEngine) -> "JourneyApi":
        return fake
