"""The jobs of ``FakeEngine``: scripted replies recorded as the engine would record them, with its repeat
rules (D57), its answer rule (D44) and its usage rule (#115). ``testing/fake_engine.py`` says what the
fake models and what it does not."""

import hashlib
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from uuid import UUID, uuid4

from livepeer_builder.contracts import (
    ActorContext,
    Attempt,
    AttemptOutcome,
    Failure,
    Job,
    JobRequest,
    JobResult,
    JobState,
    Operation,
    ResourceRef,
    ResultRef,
    SignedSession,
)
from livepeer_builder.errors import InvalidRequest, JobFailed, NotFound
from livepeer_builder.services.failover import attempt_verdict
from livepeer_builder.services.operations import retry_of
from livepeer_builder.services.ownership import check_owner, job_resource, sees_every_actor
from livepeer_builder.services.policy import DefaultPolicy
from livepeer_builder.services.request_identity import request_fingerprint
from livepeer_builder.services.usage_reports import usage_to_store
from livepeer_builder.testing.fake_async import RESULT_LOCATION, FakeQueue, scripted_failure
from livepeer_builder.testing.fake_parts import FakeDiscovery
from livepeer_builder.testing.fake_stream import FakeJobStream
from livepeer_builder.testing.scripts import (
    NOT_DISPATCHED,
    Fails,
    Raises,
    Reply,
    attempt_outcome,
)


class FakeStreaming:
    def __init__(self, jobs: "FakeJobs") -> None:
        self._jobs = jobs

    async def stream(  # NOSONAR - async by protocol (StreamingApi); the fake awaits nothing
        self, actor: ActorContext, request: JobRequest
    ) -> FakeJobStream:
        reply = self._jobs.reply_for(actor, request)
        return FakeJobStream(
            self._jobs.record(actor, request, "running", None),
            reply,
            self._jobs,
            return_http_errors=request.return_http_errors,
        )


class FakeJobs(FakeQueue):
    def __init__(
        self,
        replies: Mapping[tuple[str, str], Reply | Fails | Raises],
        streaming: bool,
        discovery: FakeDiscovery,
    ) -> None:
        self.replies = dict(replies)
        self.requests: list[tuple[ActorContext, JobRequest]] = []
        self.usage_reports: list[tuple[UUID, dict[str, int]]] = []
        self.streaming: FakeStreaming | None = FakeStreaming(self) if streaming else None
        self._discovery = discovery
        self.policy = DefaultPolicy()  # the engine's default policy and answer rule (D44)
        self._jobs: dict[UUID, Job] = {}
        self._results: dict[UUID, Reply] = {}  # the stored results of async jobs (ISS-26)
        self._waiting: dict[UUID, tuple[ActorContext, JobRequest]] = {}  # queued, oldest first
        self._operations: dict[tuple[str, str, str], tuple[str, UUID]] = {}

    def reply_for(self, actor: ActorContext, request: JobRequest) -> Reply:
        """The scripted reply, after the checks the engine makes before it dispatches."""
        self.policy.check(actor, Operation.JOBS_RUN)  # first, as the engine's _admit_request
        if request.mode == "async":  # then the mode, as the engine: an async job is queued with submit
            raise InvalidRequest('mode="async" is queued with jobs.submit; jobs.run and a stream wait')
        if request.path and not request.path.startswith("/"):  # then the path, as the engine (#113, C3)
            raise InvalidRequest("path must start with '/'")
        self.requests.append((actor, request))
        self._check_operation(actor, request)
        scripted = self.replies.get((request.app, request.path), Reply())
        if isinstance(scripted, Raises):
            raise scripted.error
        if isinstance(scripted, Fails):
            raise JobFailed(self.record(actor, request, *scripted_failure(scripted)))
        return scripted

    def _check_operation(self, actor: ActorContext, request: JobRequest) -> None:
        if request.operation_ref is None:
            return
        found = self._operations.get((actor.tenant_id, actor.actor_id, request.operation_ref))
        if found is None:
            return
        fingerprint, job_id = found
        # The engine's repeat rules (D57): conflict, in progress, exists, or dispatch again, in which
        # case ``record`` gives the reference to the new job.
        retry_of(self._jobs[job_id], same_request=fingerprint == request_fingerprint(request))

    def record(
        self,
        actor: ActorContext,
        request: JobRequest,
        state: JobState,
        failure: Failure | None,
        *,
        reply: Reply | None = None,
        queued: Job | None = None,
    ) -> Job:
        """Store a job with one attempt on the first runner for the app, as the engine would record it.

        A failure before dispatch (no offering, no capacity, no allocation) has no attempt. A succeeded
        ``reply`` sets the attempt's status and the job's ``ResultRef``. ``queued`` is an async job that
        now runs (ISS-26): it keeps its id and its creation time, and its result is ``stored``.
        """
        now = datetime.now(UTC)
        job_id = queued.id if queued is not None else uuid4()
        job = Job(
            id=job_id,
            actor_id=actor.actor_id,
            tenant_id=actor.tenant_id,
            operation_ref=request.operation_ref,
            mode=request.mode,
            app=request.app,
            capability=None,
            model=None,
            state=state,
            attempts=self._attempts(job_id, request.app, state, failure, reply, now),
            failure=failure,
            result=_result(job_id, state, reply, stored=queued is not None),
            usage=None,
            created_at=now if queued is None else queued.created_at,
            completed_at=None if state == "running" else now,
            started_at=None if queued is None else now,
        )
        self._jobs[job.id] = job
        if request.operation_ref is not None:
            key = (actor.tenant_id, actor.actor_id, request.operation_ref)
            self._operations[key] = (request_fingerprint(request), job.id)
        return job

    def _attempts(
        self,
        job_id: UUID,
        app: str,
        state: JobState,
        failure: Failure | None,
        reply: Reply | None,
        now: datetime,
    ) -> tuple[Attempt, ...]:
        """One attempt on the first runner for the app, or none for a failure before dispatch."""
        dispatched = failure is None or failure.kind not in NOT_DISPATCHED
        used = self._discovery.candidates(app)[:1] if dispatched else []
        sessions = (SignedSession(manifest_id="manifest-1"),) if failure is None else ()
        outcome, status = _outcome(state, failure), _status(reply, failure)
        return tuple(
            Attempt(
                job_id=job_id,
                number=1,
                runner_url=runner.url,
                orchestrator_url=runner.orchestrator_url,
                allocation_id=None,
                sessions=sessions,
                payment_sent=failure is None or failure.payment_sent,
                outcome=outcome,
                status_code=status,
                started_at=now,
                ended_at=None if state == "running" else now,
            )
            for runner in used
        )

    def ended(
        self,
        job: Job,
        outcome: AttemptOutcome,
        status_code: int | None,
        *,
        result: ResultRef | None = None,
        failure: Failure | None = None,
        http_error: bool = False,
    ) -> Job:
        """Record how a stream ended: the attempt's outcome, then ``succeeded``, or ``uncertain`` when
        a paid call did not finish (``failed`` when it was not paid)."""
        now = datetime.now(UTC)
        attempts = tuple(
            attempt.model_copy(update={"outcome": outcome, "status_code": status_code, "ended_at": now})
            for attempt in job.attempts
        )
        paid = any(attempt.payment_sent for attempt in attempts)
        # A returned HTTP error is the caller's answer; a paid 502 or 504 may still run (D52).
        may_run = not http_error or attempt_verdict(outcome, status_code, True) == "uncertain"
        if failure is None:
            state: JobState = "succeeded"
        else:
            state = "uncertain" if paid and may_run else "failed"
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
            reason=fails.reason,
        )
        return self.ended(job, attempt_outcome(fails.kind), status, failure=failure)

    async def run(  # NOSONAR - async by protocol (JobsApi); the fake awaits nothing
        self, actor: ActorContext, request: JobRequest
    ) -> JobResult:
        if request.return_http_errors:
            raise InvalidRequest("return_http_errors requires jobs.streaming.stream")
        reply = self.reply_for(actor, request)
        job = self.record(actor, request, "succeeded", None, reply=reply)
        return JobResult(  # returned once and not kept, as the engine's immediate result
            job=job,
            status_code=reply.status_code,
            content=reply.content,
            content_type=reply.content_type,
            data=reply.data,
        )

    async def get(  # NOSONAR - async by protocol (JobsApi); the fake awaits nothing
        self, actor: ActorContext, job_id: UUID
    ) -> Job:
        return self.owned(actor, Operation.JOBS_READ, job_id)

    def owned(self, actor: ActorContext, operation: Operation, job_id: UUID) -> Job:
        """The engine's answer rule (D44): permission (403), then ownership (404)."""
        self.policy.check(actor, operation)
        job = self._jobs.get(job_id)
        if job is None:
            raise NotFound(f"job {job_id} not found")
        check_owner(self.policy, actor, operation, job_resource(job), f"job {job_id} not found")
        return job

    async def list_jobs(  # NOSONAR - async by protocol (JobsApi); the fake awaits nothing
        self, actor: ActorContext, *, limit: int = 50, before: datetime | None = None
    ) -> Sequence[Job]:
        target = None if sees_every_actor(actor) else actor.actor_id
        self.policy.check(
            actor,
            Operation.JOBS_READ,
            ResourceRef(kind="job", tenant_id=actor.tenant_id, owner_actor_id=target),
        )
        mine = [
            job for job in self.in_scope(actor.tenant_id, target) if before is None or job.created_at < before
        ]
        return sorted(mine, key=lambda job: job.created_at, reverse=True)[:limit]

    def in_scope(self, tenant_id: str, actor_id: str | None) -> list[Job]:
        """The jobs of one tenant, and of one actor when given: what an admin or an actor sees."""
        return [
            job
            for job in self._jobs.values()
            if job.tenant_id == tenant_id and actor_id in (None, job.actor_id)
        ]

    async def report_usage(  # NOSONAR - async by protocol (JobsApi); the fake awaits nothing
        self, actor: ActorContext, job_id: UUID, quantities: Mapping[str, int]
    ) -> Job:
        """Stored once on the job, by the engine's own rule (#115, C20): the same quantities again get the
        stored usage back, and any other report is refused. ``usage_reports`` lists each report answered."""
        job = self.owned(actor, Operation.JOBS_RUN, job_id)
        usage = usage_to_store(job, quantities, datetime.now(UTC))
        self.usage_reports.append((job_id, dict(quantities)))
        if usage is not None:
            self._jobs[job_id] = job.model_copy(update={"usage": usage})
        return self._jobs[job_id]


def _outcome(state: JobState, failure: Failure | None) -> AttemptOutcome:
    """The outcome of the one attempt of a job recorded in ``state``."""
    if state == "running":
        return "in_flight"
    return "succeeded" if failure is None else attempt_outcome(failure.kind)


def _status(reply: Reply | None, failure: Failure | None) -> int | None:
    """The attempt's status: the reply's when there is one, else the failure's own."""
    if reply is not None:
        return reply.status_code
    return None if failure is None else failure.status_code


def _result(job_id: UUID, state: JobState, reply: Reply | None, *, stored: bool) -> ResultRef | None:
    """What a succeeded job says of its result: returned inline, or ``stored`` for an async job."""
    if reply is None or state != "succeeded":
        return None
    return ResultRef(
        storage="stored" if stored else "inline",
        content_type=reply.content_type,
        size_bytes=len(reply.content),
        sha256=hashlib.sha256(reply.content).hexdigest(),
        location=RESULT_LOCATION.format(job_id=job_id) if stored else None,
    )
