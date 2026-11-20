"""The async jobs of ``FakeEngine`` (ISS-26): ``submit`` queues a job, and the test's own ``advance()``
runs what is queued, since the fake has no worker and no loop.

It follows the engine's rules for the API's shape: a queued job reads back ``queued`` and costs
``pending``; ``advance()`` ends it as its script says, with a ``stored`` result when it succeeds;
``result`` answers the stored bytes, ``JobNotFinished`` before the end, ``JobFailed`` for a failure and
``ResultExpired`` for an immediate job, whose result was never kept; ``cancel`` ends a queued job
``failed`` with reason ``cancelled`` and refuses any other; and a repeated ``operation_ref`` returns
the job it names. It does not model the queue's limits: script a full queue with
``Raises(NoCapacity(...))``, which is raised at ``submit``, and an actor with no allocation with
``Fails(kind="not_provisioned")``, which fails the job at ``submit`` as the engine does.
"""

from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID, uuid4

from livepeer_builder.contracts import (
    OPERATION_REPLAYED,
    ActorContext,
    Failure,
    Job,
    JobRequest,
    JobResult,
    JobState,
    Operation,
)
from livepeer_builder.errors import InvalidRequest, JobFailed, JobNotFinished, JobNotQueued, ResultExpired
from livepeer_builder.protocols import AccessPolicy
from livepeer_builder.services.failover import attempt_verdict
from livepeer_builder.services.operations import async_repeat
from livepeer_builder.services.request_identity import request_fingerprint
from livepeer_builder.testing.scripts import UNCERTAIN_OUTCOMES, Fails, Raises, Reply, attempt_outcome

RESULT_LOCATION = "/v1/jobs/{job_id}/result"  # as the engine's (services/async_jobs.py)
CANCELLED = "cancelled"


def scripted_failure(scripted: Fails) -> tuple[JobState, Failure]:
    """The state and the failure a scripted ``Fails`` leaves on its job, by the engine's rules."""
    failure = Failure(
        kind=scripted.kind,
        message=scripted.message,
        status_code=scripted.status_code,
        body=scripted.body,
        payment_sent=scripted.payment_sent,
        reason=scripted.reason,
    )
    outcome = attempt_outcome(scripted.kind)  # a paid 502 or 504 may have run, as in the engine
    may_have_run = attempt_verdict(outcome, scripted.status_code, True) == "uncertain"
    paid_unknown = scripted.payment_sent and (outcome in UNCERTAIN_OUTCOMES or may_have_run)
    signer_unknown = outcome == "payment_unknown"  # the signer may have charged, paid or not
    return ("uncertain" if paid_unknown or signer_unknown else "failed"), failure


class _Recorder(Protocol):
    def __call__(
        self,
        actor: ActorContext,
        request: JobRequest,
        state: JobState,
        failure: Failure | None,
        *,
        reply: Reply | None = None,
        queued: Job | None = None,
    ) -> Job: ...


class _Owned(Protocol):
    def __call__(self, actor: ActorContext, operation: Operation, job_id: UUID) -> Job: ...


class FakeQueue:
    """What ``FakeJobs`` does for ``mode="async"``. It shares the fake's jobs, scripts and policy."""

    policy: AccessPolicy
    replies: dict[tuple[str, str], Reply | Fails | Raises]
    requests: list[tuple[ActorContext, JobRequest]]
    record: _Recorder
    owned: _Owned
    _jobs: dict[UUID, Job]
    _results: dict[UUID, Reply]
    _waiting: dict[UUID, tuple[ActorContext, JobRequest]]
    _operations: dict[tuple[str, str, str], tuple[str, UUID]]

    async def submit(  # NOSONAR - async by protocol (JobsApi); the fake awaits nothing
        self, actor: ActorContext, request: JobRequest
    ) -> Job:
        """Queue the job, as the engine does: nothing is scripted to run until ``advance()``."""
        self.policy.check(actor, Operation.JOBS_RUN)  # first, as the engine's submit
        if request.mode != "async":
            raise InvalidRequest('jobs.submit queues a job with mode="async"; jobs.run runs an immediate one')
        if request.return_http_errors:
            raise InvalidRequest("return_http_errors requires jobs.streaming.stream")
        if request.path and not request.path.startswith("/"):
            raise InvalidRequest("path must start with '/'")
        self.requests.append((actor, request))
        OPERATION_REPLAYED.set(False)
        held = self._held(actor, request)
        if held is not None:
            OPERATION_REPLAYED.set(True)
            return held
        scripted = self.replies.get((request.app, request.path), Reply())
        if isinstance(scripted, Raises):
            raise scripted.error
        if isinstance(scripted, Fails) and scripted.kind == "not_provisioned":  # fails fast, as in run
            raise JobFailed(self.record(actor, request, *scripted_failure(scripted)))
        job = Job(
            id=uuid4(),
            actor_id=actor.actor_id,
            tenant_id=actor.tenant_id,
            operation_ref=request.operation_ref,
            mode="async",
            app=request.app,
            capability=None,
            model=None,
            state="queued",
            attempts=(),
            failure=None,
            result=None,
            usage=None,
            created_at=datetime.now(UTC),
            completed_at=None,
        )
        self._jobs[job.id] = job
        self._waiting[job.id] = (actor, request)
        if request.operation_ref is not None:
            key = (actor.tenant_id, actor.actor_id, request.operation_ref)
            self._operations[key] = (request_fingerprint(request), job.id)
        return job

    def _held(self, actor: ActorContext, request: JobRequest) -> Job | None:
        """The job a repeated reference names, by the engine's rule for an async repeat (D69 (1)); ``None``
        when the reference is free, or its job never reached a runner and may be queued again."""
        if request.operation_ref is None:
            return None
        found = self._operations.get((actor.tenant_id, actor.actor_id, request.operation_ref))
        if found is None:
            return None
        fingerprint, job_id = found
        holder = self._jobs[job_id]
        retrying = async_repeat(holder, same_request=fingerprint == request_fingerprint(request))
        return None if retrying else holder

    async def advance(self) -> int:  # NOSONAR - awaited as the worker's run_once is; awaits nothing
        """Run every queued job, oldest first, to the end its script gives it; how many ran. The
        engine's worker does this by itself; a test of the engine steps it with ``run_once()``."""
        waiting, self._waiting = self._waiting, {}
        for job_id, (actor, request) in waiting.items():
            job = self._jobs[job_id]
            scripted = self.replies.get((request.app, request.path), Reply())
            if isinstance(scripted, Reply):
                self.record(actor, request, "succeeded", None, reply=scripted, queued=job)
                self._results[job_id] = scripted
            elif isinstance(scripted, Fails):
                self.record(actor, request, *scripted_failure(scripted), queued=job)
            else:  # the engine's worker raises nothing: an error it meets ends the job
                failure = Failure(kind="runner_error", message="engine error")
                self.record(actor, request, "failed", failure, queued=job)
        return len(waiting)

    async def result(  # NOSONAR - async by protocol (JobsApi); the fake awaits nothing
        self, actor: ActorContext, job_id: UUID
    ) -> JobResult:
        """The engine's rules (ISS-26): the stored bytes of a succeeded async job, and nothing else."""
        job = self.owned(actor, Operation.JOBS_READ, job_id)
        if job.state in ("queued", "running"):
            raise JobNotFinished(job)
        if job.state != "succeeded":
            raise JobFailed(job)
        if job.result is None or job_id not in self._results:
            raise ResultExpired(job, "the result of an immediate job is returned once and not kept")
        kept = self._results[job_id]
        return JobResult(
            job=job, status_code=200, content=kept.content, content_type=kept.content_type, data=kept.data
        )

    async def cancel(  # NOSONAR - async by protocol (JobsApi); the fake awaits nothing
        self, actor: ActorContext, job_id: UUID
    ) -> Job:
        """Cancel a queued job; any other is ``JobNotQueued``, and a cancelled one is answered again."""
        job = self.owned(actor, Operation.JOBS_RUN, job_id)
        if job.state != "queued":
            if job.failure is None or job.failure.reason != CANCELLED:
                raise JobNotQueued(job)
            return job
        del self._waiting[job_id]
        failure = Failure(kind="abandoned", message="cancelled before it ran", reason=CANCELLED)
        ended = job.model_copy(
            update={"state": "failed", "failure": failure, "completed_at": datetime.now(UTC)}
        )
        self._jobs[job_id] = ended
        return ended

    def expire_results(self) -> None:
        """Drop every stored result, as the engine's retention does once ``LPB_RETENTION_RESULTS_DAYS``
        have passed. The jobs keep their record, and ``result`` then raises ``ResultExpired``."""
        self._results.clear()
