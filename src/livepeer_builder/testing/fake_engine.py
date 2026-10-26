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
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from uuid import UUID, uuid4

from livepeer_builder.contracts import (
    ActorContext,
    Allowance,
    Attempt,
    AttemptOutcome,
    CostSummary,
    EngineHealth,
    Failure,
    Job,
    JobCost,
    JobRequest,
    JobResult,
    JobState,
    Offering,
    Operation,
    ResourceRef,
    ResultRef,
    Runner,
)
from livepeer_builder.errors import (
    InvalidRequest,
    JobFailed,
    NotFound,
    NotProvisioned,
    OperationConflict,
    OperationExists,
)
from livepeer_builder.services.costs import classify
from livepeer_builder.services.jobs import request_fingerprint
from livepeer_builder.services.ownership import check_owner, job_resource
from livepeer_builder.services.policy import DefaultPolicy
from livepeer_builder.testing.fake_stream import FakeJobStream
from livepeer_builder.testing.scripts import (
    NOT_DISPATCHED,
    UNCERTAIN_OUTCOMES,
    Fails,
    Raises,
    Reply,
    attempt_outcome,
)


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
        self.policy = DefaultPolicy()  # the engine's default policy and answer rule (D44)
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
            paid_unknown = scripted.payment_sent and attempt_outcome(scripted.kind) in UNCERTAIN_OUTCOMES
            raise JobFailed(self.record(actor, request, "uncertain" if paid_unknown else "failed", failure))
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
        self,
        actor: ActorContext,
        request: JobRequest,
        state: JobState,
        failure: Failure | None,
        *,
        reply: Reply | None = None,
    ) -> Job:
        """Store a job with one attempt on the first runner for the app, as the engine would record it.

        A failure before dispatch (no offering, no capacity, not provisioned) has no attempt. A succeeded
        ``reply`` sets the attempt's status and the job's ``ResultRef``.
        """
        now = datetime.now(UTC)
        job_id = uuid4()
        dispatched = failure is None or failure.kind not in NOT_DISPATCHED
        used = self._discovery.candidates(request.app)[:1] if dispatched else []
        if state == "running":
            outcome: AttemptOutcome = "in_flight"
        else:
            outcome = "succeeded" if failure is None else attempt_outcome(failure.kind)
        status = (
            reply.status_code if reply is not None else failure.status_code if failure is not None else None
        )
        attempts = tuple(
            Attempt(
                job_id=job_id,
                number=1,
                runner_url=runner.url,
                orchestrator_url=runner.orchestrator_url,
                allocation_id=None,
                sessions=(),
                payment_sent=failure is None or failure.payment_sent,
                outcome=outcome,
                status_code=status,
                started_at=now,
                ended_at=None if state == "running" else now,
            )
            for runner in used
        )
        result = None
        if reply is not None and state == "succeeded":
            result = ResultRef(
                storage="inline",
                content_type=reply.content_type,
                size_bytes=len(reply.content),
                sha256=hashlib.sha256(reply.content).hexdigest(),
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
            result=result,
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

    def broke(self, job: Job, fails: Fails, stream_status: int) -> Job:
        """A stream that failed part-way. Payment was decided when it opened (the attempt is paid), so
        the job is ``uncertain`` until usage settles what it cost; ``Fails.payment_sent`` does not apply.
        The attempt keeps the stream's status unless the failure has one of its own."""
        status = fails.status_code if fails.status_code is not None else stream_status
        failure = Failure(
            kind=fails.kind,
            message=fails.message,
            status_code=fails.status_code,
            body=fails.body,
            payment_sent=True,
        )
        return self.ended(job, attempt_outcome(fails.kind), status, failure=failure)

    async def run(self, actor: ActorContext, request: JobRequest) -> JobResult:
        reply = self.reply_for(actor, request)
        job = self.record(actor, request, "succeeded", None, reply=reply)
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
        return self.owned(actor, Operation.JOBS_READ, job_id)

    def owned(self, actor: ActorContext, operation: Operation, job_id: UUID) -> Job:
        """The engine's answer rule (D44): permission (403), then ownership (404)."""
        self.policy.check(actor, operation)
        job = self._jobs.get(job_id)
        if job is None:
            raise NotFound(f"job {job_id} not found")
        check_owner(self.policy, actor, operation, job_resource(job), f"job {job_id} not found")
        return job

    async def list_jobs(
        self, actor: ActorContext, *, limit: int = 50, before: datetime | None = None
    ) -> Sequence[Job]:
        target = None if actor.credential_kind == "admin" else actor.actor_id
        self.policy.check(
            actor,
            Operation.JOBS_READ,
            ResourceRef(kind="job", application_id=actor.application_id, owner_actor_id=target),
        )
        mine = [
            job
            for job in self._jobs.values()
            if job.application_id == actor.application_id
            and target in (None, job.actor_id)
            and (before is None or job.created_at < before)
        ]
        return sorted(mine, key=lambda job: job.created_at, reverse=True)[:limit]

    async def result(self, actor: ActorContext, job_id: UUID) -> JobResult:
        self.owned(actor, Operation.JOBS_READ, job_id)
        if job_id not in self._results:
            raise NotFound(f"job {job_id} has no result")
        return self._results[job_id]

    async def report_usage(self, actor: ActorContext, job_id: UUID, quantities: Mapping[str, int]) -> Job:
        job = self.owned(actor, Operation.JOBS_RUN, job_id)
        if job.state != "succeeded":
            raise InvalidRequest("usage can only be reported for a succeeded job")
        if any(value < 0 for value in quantities.values()):
            raise InvalidRequest("usage quantities must be non-negative")
        self.usage_reports.append((job_id, dict(quantities)))
        return job


class FakeCosts:
    """A scripted cost wins. Otherwise a job the fake ran costs what the engine reports before any usage
    arrives (``classify`` with no rows): ``pending`` while running or once paid, ``none`` if unpaid."""

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
        self.costs = FakeCosts(costs or {}, summary, self.jobs)
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
