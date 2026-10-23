"""``FakeEngine``: the actor API (``ActorApi``) scripted at the engine level, for application unit tests.

It returns real contract values and raises real engine errors, so code under test cannot tell it from
the engine for the API's *shape*: run a job, read it back, stream it, see costs and the catalog.
Like the engine, a run records an attempt on the first runner for the app, a repeated
``operation_ref`` raises ``OperationExists`` (or ``OperationConflict`` for a different request), and
a stream that fails part-way leaves the job ``uncertain``. It does not model the money rules
(admission, failover, payment); script those outcomes with ``Fails`` and ``Raises``, or use
``make_engine`` when they matter. A contract test runs the same actor-API assertions against both.

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

import hashlib
from collections.abc import AsyncGenerator, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from types import TracebackType
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from livepeer_builder.contracts import (
    ActorContext,
    Allowance,
    Attempt,
    AttemptOutcome,
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
    ResultRef,
    Runner,
)
from livepeer_builder.errors import (
    EngineError,
    InvalidRequest,
    JobFailed,
    NotFound,
    NotProvisioned,
    OperationConflict,
    OperationExists,
)
from livepeer_builder.services.jobs import request_fingerprint

if TYPE_CHECKING:
    from livepeer_builder.api import ActorApi


@dataclass(frozen=True)
class Fails:
    """The job fails: ``run`` and ``stream`` raise ``JobFailed`` with this failure.

    ``payment_sent=True`` is a runner error after payment: the job fails on that runner (no failover)
    and its cost counts as failed spend. ``body`` is the runner's own words.
    """

    kind: FailureKind = "runner_error"
    message: str = "scripted failure"
    status_code: int | None = 500
    payment_sent: bool = False
    body: str | None = None


@dataclass(frozen=True)
class Reply:
    """A successful runner reply. ``chunks`` is what a stream yields."""

    content: bytes = b'{"ok": true}'
    status_code: int = 200
    content_type: str = "application/json"
    chunks: tuple[bytes | Fails, ...] = (b'{"ok": true}',)
    data: dict[str, Any] | None = None


@dataclass(frozen=True)
class Raises:
    """The call raises ``error``, as the engine does before any job exists: ``NoCapacity`` at admission,
    ``ProviderUnavailable``, and so on. Scripted for a reply, or for a job's cost."""

    error: EngineError


class FakeJobStream:
    """A ``JobStreamApi`` over scripted chunks that ends the job the way the engine's ``JobStream`` does.

    The job ends once. Reading to the end, or ``complete()``, records it ``succeeded`` with a
    ``ResultRef`` of the bytes read. ``aclose()``, or leaving ``async with`` early, records the attempt
    ``abandoned`` and the paid job ``uncertain``. ``disconnected()`` lets the runner finish: the
    scripted chunks are all there, so the stream succeeds unless a ``Fails`` comes first. A ``Fails``
    among the chunks raises ``JobFailed`` there. ``closed`` and ``disconnected_called`` record the
    caller's calls.
    """

    def __init__(self, job: Job, reply: Reply, jobs: "FakeJobs") -> None:
        self._job, self._reply, self._jobs = job, reply, jobs
        self._next = 0  # the next scripted chunk
        self._read = hashlib.sha256()
        self._size = 0
        self._ended = False
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

    @property
    def finished(self) -> bool:
        return self._ended

    def __aiter__(self) -> AsyncGenerator[bytes, None]:
        return self._iterate()

    async def _iterate(self) -> AsyncGenerator[bytes, None]:
        try:
            while not self._ended:
                if self._next == len(self._reply.chunks):
                    self._succeed()
                    return
                chunk = self._reply.chunks[self._next]
                self._next += 1
                if isinstance(chunk, Fails):
                    self._fail(chunk)
                    raise JobFailed(self._job)
                self._count(chunk)
                yield chunk
            if self._job.state != "succeeded" and not self.closed:
                raise JobFailed(self._job)  # iterating a stream that already failed
        except GeneratorExit:  # the iterator was closed or dropped: the caller stopped
            self._abandon()
            raise

    async def __aenter__(self) -> "FakeJobStream":
        return self

    async def __aexit__(
        self, kind: type[BaseException] | None, error: BaseException | None, trace: TracebackType | None
    ) -> None:
        await self.aclose()

    async def complete(self) -> None:
        self._succeed()

    async def aclose(self) -> None:
        self.closed = True
        self._abandon()

    async def disconnected(self) -> None:
        self.disconnected_called = True
        while not self._ended and self._next < len(self._reply.chunks):
            chunk = self._reply.chunks[self._next]
            self._next += 1
            if isinstance(chunk, Fails):
                self._fail(chunk)
                return
            self._count(chunk)
        self._succeed()

    def _count(self, chunk: bytes) -> None:
        self._read.update(chunk)
        self._size += len(chunk)

    def _succeed(self) -> None:
        if not self._ended:
            self._ended = True
            result = ResultRef(
                storage="inline",
                content_type=self._reply.content_type,
                size_bytes=self._size,
                sha256=self._read.hexdigest(),
            )
            self._job = self._jobs.ended(self._job, "succeeded", self._reply.status_code, result=result)

    def _abandon(self) -> None:
        if not self._ended:
            self._ended = True
            failure = Failure(
                kind="abandoned", message="the caller stopped reading the stream", payment_sent=True
            )
            self._job = self._jobs.ended(self._job, "abandoned", self._reply.status_code, failure=failure)

    def _fail(self, fails: Fails) -> None:
        if not self._ended:
            self._ended = True
            self._job = self._jobs.broke(self._job, fails)


class FakeStreaming:
    def __init__(self, jobs: "FakeJobs") -> None:
        self._jobs = jobs

    async def stream(self, actor: ActorContext, request: JobRequest) -> FakeJobStream:
        reply = self._jobs.reply_for(actor, request)
        return FakeJobStream(self._jobs.record(actor, request, "running", None), reply, self._jobs)


class FakeJobs:
    def __init__(
        self,
        replies: Mapping[tuple[str, str], Reply | Fails | Raises],
        streaming: bool,
        discovery: "FakeDiscovery",
    ) -> None:
        self.replies = dict(replies)
        self.requests: list[tuple[ActorContext, JobRequest]] = []
        self.usage_reports: list[tuple[UUID, dict[str, int]]] = []
        self.streaming: FakeStreaming | None = FakeStreaming(self) if streaming else None
        self._discovery = discovery
        self._jobs: dict[UUID, Job] = {}
        self._results: dict[UUID, JobResult] = {}
        self._operations: dict[tuple[str, str, str], tuple[str, UUID]] = {}

    def reply_for(self, actor: ActorContext, request: JobRequest) -> Reply:
        """The scripted reply, after the checks the engine makes before it dispatches."""
        self.requests.append((actor, request))
        self._check_operation(actor, request)
        scripted = self.replies.get((request.app, request.path), Reply())
        if isinstance(scripted, Raises):
            raise scripted.error
        if isinstance(scripted, Fails):
            failure = Failure(
                kind=scripted.kind,
                message=scripted.message,
                status_code=scripted.status_code,
                body=scripted.body,
                payment_sent=scripted.payment_sent,
            )
            raise JobFailed(self.record(actor, request, "failed", failure))
        return scripted

    def _check_operation(self, actor: ActorContext, request: JobRequest) -> None:
        if request.operation_ref is None:
            return
        found = self._operations.get((actor.application_id, actor.actor_id, request.operation_ref))
        if found is None:
            return
        fingerprint, job_id = found
        existing = self._jobs[job_id]
        if fingerprint != request_fingerprint(request):
            raise OperationConflict(existing)
        raise OperationExists(existing)

    def record(
        self, actor: ActorContext, request: JobRequest, state: JobState, failure: Failure | None
    ) -> Job:
        now = datetime.now(UTC)
        job_id = uuid4()
        used = self._discovery.candidates(request.app)[:1]
        attempts = tuple(
            Attempt(
                job_id=job_id,
                number=1,
                runner_url=runner.url,
                orchestrator_url=runner.orchestrator_url,
                allocation_id=None,
                sessions=(),
                payment_sent=failure is None or failure.payment_sent,
                outcome="in_flight" if state == "running" else "succeeded" if failure is None else "http",
                status_code=failure.status_code if failure is not None else None,
                started_at=now,
                ended_at=None if state == "running" else now,
            )
            for runner in used
        )
        job = Job(
            id=job_id,
            actor_id=actor.actor_id,
            application_id=actor.application_id,
            operation_ref=request.operation_ref,
            mode="immediate",
            app=request.app,
            capability=None,
            model=None,
            state=state,
            attempts=attempts,
            failure=failure,
            result=None,
            usage=None,
            created_at=now,
            completed_at=None if state == "running" else now,
        )
        self._jobs[job.id] = job
        if request.operation_ref is not None:
            key = (actor.application_id, actor.actor_id, request.operation_ref)
            self._operations[key] = (request_fingerprint(request), job.id)
        return job

    def ended(
        self,
        job: Job,
        outcome: AttemptOutcome,
        status_code: int | None,
        *,
        result: ResultRef | None = None,
        failure: Failure | None = None,
    ) -> Job:
        """Record how a stream ended: the attempt's outcome, then ``succeeded``, or ``uncertain`` when
        a paid call did not finish (``failed`` when it was not paid)."""
        now = datetime.now(UTC)
        attempts = tuple(
            attempt.model_copy(update={"outcome": outcome, "status_code": status_code, "ended_at": now})
            for attempt in job.attempts
        )
        paid = any(attempt.payment_sent for attempt in attempts)
        state: JobState = "succeeded" if failure is None else "uncertain" if paid else "failed"
        done = job.model_copy(
            update={
                "state": state,
                "failure": failure,
                "result": result,
                "attempts": attempts,
                "completed_at": now,
            }
        )
        self._jobs[job.id] = done
        return done

    def broke(self, job: Job, fails: Fails) -> Job:
        """A paid stream that failed part-way: ``uncertain`` until usage settles what it cost."""
        failure = Failure(kind=fails.kind, message=fails.message, body=fails.body, payment_sent=True)
        return self.ended(job, "http", 200, failure=failure)

    async def run(self, actor: ActorContext, request: JobRequest) -> JobResult:
        reply = self.reply_for(actor, request)
        job = self.record(actor, request, "succeeded", None)
        result = JobResult(
            job=job,
            status_code=reply.status_code,
            content=reply.content,
            content_type=reply.content_type,
            data=reply.data,
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
    def __init__(self, costs: Mapping[UUID, JobCost | Raises], summary: CostSummary | None) -> None:
        self.costs = dict(costs)
        self._summary = summary

    async def for_job(self, actor: ActorContext, job_id: UUID) -> JobCost:
        scripted = self.costs.get(job_id)
        if scripted is None:
            raise NotFound(f"no cost for job {job_id}")
        if isinstance(scripted, Raises):
            raise scripted.error
        return scripted

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
    """Implements ``ActorApi``; ``streaming=False`` gives an engine whose transport cannot stream."""

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
        self.costs = FakeCosts(costs or {}, summary)
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


if TYPE_CHECKING:  # mypy proves the fake offers the actor API

    def _actor_api(fake: FakeEngine) -> "ActorApi":
        return fake
