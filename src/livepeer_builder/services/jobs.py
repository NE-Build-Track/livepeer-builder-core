"""Jobs: immediate execution with failover before payment, durable attempts, and restart recovery.

Rules this module enforces (docs/contracts/engine.md#behavior-the-contract-fixes):
1. Fail over only while no payment was sent and the runner refused or never answered.
2. One (application, actor, operation_ref) dispatches at most once; a changed body is a conflict.
3. An attempt row is written *before* the call and updated after it. Paid-but-unknown is
   ``uncertain``, never ``failed``. Jobs left ``running`` by a restart are resolved on start.
4. Admission and provisioning are checked before any payment.
"""

import hashlib
import json
import logging
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import cast
from uuid import UUID, uuid4

from livepeer_builder.contracts import (
    ActorContext,
    Attempt,
    Failure,
    FailureKind,
    Job,
    JobRequest,
    JobResult,
    JobState,
    MeasuredUsage,
    Operation,
    ResourceRef,
    ResultRef,
    RunnerReply,
)
from livepeer_builder.errors import (
    AccessDenied,
    InvalidRequest,
    JobFailed,
    NotFound,
    NotImplementedInMilestone,
    NotProvisioned,
    OperationConflict,
    OperationExists,
    RunnerCallError,
)
from livepeer_builder.protocols import (
    AccessPolicy,
    Clock,
    CredentialResolver,
    JobRepo,
    RunnerTransport,
    SelectionPolicy,
)
from livepeer_builder.services.admission import AdmissionLimiter
from livepeer_builder.services.discovery import DiscoveryService

_LOG = logging.getLogger(__name__)
_BODY_LIMIT = 4096
_FAILOVER_OUTCOMES = frozenset(
    {"refused", "unreachable", "payment_refused"}
)  # only while payment_sent is false
_UNCERTAIN_OUTCOMES = frozenset({"unreachable", "timeout"})  # after payment the runner may have run


class JobService:
    def __init__(
        self,
        store: JobRepo,
        discovery: DiscoveryService,
        transport: RunnerTransport,
        selection: SelectionPolicy,
        credentials: CredentialResolver,
        policy: AccessPolicy,
        admission: AdmissionLimiter,
        clock: Clock,
    ) -> None:
        self._store = store
        self._discovery = discovery
        self._transport = transport
        self._selection = selection
        self._credentials = credentials
        self._policy = policy
        self._admission = admission
        self._clock = clock

    async def run(self, actor: ActorContext, request: JobRequest) -> JobResult:
        """Dispatch one immediate job and wait for its result. Raises JobFailed, OperationExists."""
        self._policy.check(actor, Operation.JOBS_RUN)
        if request.mode == "async":
            raise NotImplementedInMilestone("async jobs", "M4", "ISS-26")
        if request.path and not request.path.startswith("/"):
            raise InvalidRequest("path must start with '/'")
        fingerprint = request_fingerprint(request)
        if request.operation_ref is not None:
            await self._check_operation(actor, request.operation_ref, fingerprint)
        async with self._admission.admit(request.app):
            job = self._new_job(actor, request)
            await self._store.create_job(job, fingerprint)
            attempts: list[Attempt] = []
            try:
                return await self._dispatch(job, actor, request, attempts)
            except JobFailed:
                raise
            except BaseException as exc:
                await self._close_after_error(job, attempts, exc)
                raise

    async def submit(self, actor: ActorContext, request: JobRequest) -> Job:
        del actor, request
        raise NotImplementedInMilestone("async jobs", "M4", "ISS-26")

    async def result(self, actor: ActorContext, job_id: UUID) -> JobResult:
        del actor, job_id
        raise NotImplementedInMilestone("stored job results", "M4", "ISS-26")

    async def get(self, actor: ActorContext, job_id: UUID) -> Job:
        job = await self._store.get_job(job_id)
        if job is None:
            raise NotFound(f"job {job_id} not found")
        try:
            self._policy.check(actor, Operation.JOBS_READ, _job_resource(job))
        except AccessDenied as exc:
            raise NotFound(f"job {job_id} not found") from exc  # never reveal other actors' jobs
        return job

    async def list_jobs(
        self, actor: ActorContext, *, limit: int = 50, before: datetime | None = None
    ) -> Sequence[Job]:
        self._policy.check(actor, Operation.JOBS_READ)
        return await self._store.list_jobs(actor.application_id, actor.actor_id, limit=limit, before=before)

    async def report_usage(self, actor: ActorContext, job_id: UUID, quantities: Mapping[str, int]) -> Job:
        """The application measured the work and reports it once (service-mode default)."""
        job = await self.get(actor, job_id)
        self._policy.check(actor, Operation.JOBS_RUN, _job_resource(job))
        if job.usage is not None:
            raise InvalidRequest("usage was already reported for this job")
        if job.state != "succeeded":
            raise InvalidRequest("usage can only be reported for a succeeded job")
        if not quantities or any(value < 0 for value in quantities.values()):
            raise InvalidRequest("usage needs at least one non-negative quantity")
        usage = MeasuredUsage(
            quantities=dict(quantities), source="app_reported", reported_at=self._clock.now()
        )
        await self._store.set_usage(job.id, usage)
        return job.model_copy(update={"usage": usage})

    async def recover_interrupted(self) -> int:
        """Resolve jobs a restart left ``running``. Anything that may have paid becomes ``uncertain``."""
        recovered = 0
        for job in await self._store.jobs_in_state("running"):
            maybe_paid = any(a.payment_sent or a.outcome == "in_flight" for a in job.attempts)
            failure = Failure(
                kind="interrupted",
                message="the engine stopped while this job was running",
                payment_sent=any(a.payment_sent for a in job.attempts),
            )
            await self._store.finish_job(
                job.id, "uncertain" if maybe_paid else "failed", failure, None, self._clock.now()
            )
            recovered += 1
        if recovered:
            _LOG.warning("recovered interrupted jobs", extra={"event": "jobs.recovered", "count": recovered})
        return recovered

    async def _check_operation(self, actor: ActorContext, operation_ref: str, fingerprint: str) -> None:
        found = await self._store.job_by_operation_ref(actor.application_id, actor.actor_id, operation_ref)
        if found is None:
            return
        existing, existing_fingerprint = found
        if existing_fingerprint is not None and existing_fingerprint != fingerprint:
            raise OperationConflict(existing)
        raise OperationExists(existing)

    async def _dispatch(
        self, job: Job, actor: ActorContext, request: JobRequest, attempts: list[Attempt]
    ) -> JobResult:
        candidates = self._discovery.candidates(request.app)
        if not candidates:
            raise await self._fail(job, attempts, "no_offering", f"no runner advertises app {request.app!r}")
        if all(r.capacity_available is not None and r.capacity_available <= 0 for r in candidates):
            raise await self._fail(job, attempts, "no_capacity", f"every runner for {request.app!r} is busy")
        try:
            credential = await self._credentials.credential(actor)
        except NotProvisioned as exc:
            raise await self._fail(job, attempts, "not_provisioned", str(exc)) from exc

        ordered = list(self._selection.order(candidates, request))[: request.max_attempts]
        for number, runner in enumerate(ordered, start=1):
            attempt = Attempt(
                job_id=job.id,
                number=number,
                runner_url=runner.url,
                orchestrator_url=runner.orchestrator_url,
                auth_ids=(),
                manifest_id=None,
                payment_sent=False,
                outcome="in_flight",
                status_code=None,
                started_at=self._clock.now(),
                ended_at=None,
            )
            await self._store.start_attempt(attempt)
            attempts.append(attempt)
            try:
                reply = await self._transport.call(runner, request, credential)
            except RunnerCallError as error:
                attempts[-1] = attempt.model_copy(
                    update={
                        "auth_ids": error.auth_ids,
                        "payment_sent": error.payment_sent,
                        "outcome": error.kind,
                        "status_code": error.status_code,
                        "ended_at": self._clock.now(),
                    }
                )
                await self._store.finish_attempt(attempts[-1])
                if error.payment_sent or error.kind not in _FAILOVER_OUTCOMES:
                    paid_unknown = error.payment_sent and error.kind in _UNCERTAIN_OUTCOMES
                    finished = await self._finish(
                        job, "uncertain" if paid_unknown else "failed", _failure(error), None, attempts
                    )
                    raise JobFailed(finished) from error
                continue
            attempts[-1] = attempt.model_copy(
                update={
                    "auth_ids": reply.auth_ids,
                    "manifest_id": reply.manifest_id,
                    "payment_sent": reply.payment_sent,
                    "outcome": "succeeded",
                    "status_code": reply.status_code,
                    "ended_at": self._clock.now(),
                }
            )
            await self._store.finish_attempt(attempts[-1])
            finished = await self._finish(job, "succeeded", None, _result_ref(reply), attempts)
            return JobResult(
                job=finished,
                status_code=reply.status_code,
                content=reply.content,
                content_type=reply.content_type,
                data=reply.data,
            )
        last = attempts[-1]  # only refused or unreachable attempts reach this point
        kind: FailureKind = (
            "payment" if last.outcome == "payment_refused" else cast(FailureKind, last.outcome)
        )
        message = f"all {len(attempts)} runners refused, were unreachable, or could not be paid"
        raise await self._fail(job, attempts, kind, message)

    async def _fail(self, job: Job, attempts: list[Attempt], kind: FailureKind, message: str) -> JobFailed:
        failure = Failure(kind=kind, message=message)
        return JobFailed(await self._finish(job, "failed", failure, None, attempts))

    async def _close_after_error(self, job: Job, attempts: list[Attempt], exc: BaseException) -> None:
        maybe_paid = any(a.payment_sent or a.outcome == "in_flight" for a in attempts)
        failure = Failure(
            kind="runner_error",
            message=f"engine error: {type(exc).__name__}",
            payment_sent=any(a.payment_sent for a in attempts),
        )
        try:
            await self._finish(job, "uncertain" if maybe_paid else "failed", failure, None, attempts)
        except Exception:
            _LOG.exception("could not close job", extra={"event": "jobs.close_failed", "job_id": str(job.id)})

    async def _finish(
        self,
        job: Job,
        state: JobState,
        failure: Failure | None,
        result: ResultRef | None,
        attempts: Sequence[Attempt],
    ) -> Job:
        completed = self._clock.now()
        await self._store.finish_job(job.id, state, failure, result, completed)
        return job.model_copy(
            update={
                "state": state,
                "failure": failure,
                "result": result,
                "attempts": tuple(attempts),
                "completed_at": completed,
            }
        )

    def _new_job(self, actor: ActorContext, request: JobRequest) -> Job:
        return Job(
            id=uuid4(),
            actor_id=actor.actor_id,
            application_id=actor.application_id,
            operation_ref=request.operation_ref,
            mode=request.mode,
            app=request.app,
            capability=request.capability,
            model=request.model,
            state="running",
            attempts=(),
            failure=None,
            result=None,
            usage=None,
            created_at=self._clock.now(),
            completed_at=None,
        )


def request_fingerprint(request: JobRequest) -> str:
    """Stable hash of what a retry must repeat exactly. Limits and timeouts may differ between retries."""
    fields = request.model_dump(
        mode="json", include={"app", "path", "method", "payload", "capability", "model"}
    )
    canonical = json.dumps(fields, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _job_resource(job: Job) -> ResourceRef:
    return ResourceRef(
        kind="job", application_id=job.application_id, owner_actor_id=job.actor_id, id=str(job.id)
    )


def _result_ref(reply: RunnerReply) -> ResultRef:
    return ResultRef(
        storage="inline",
        content_type=reply.content_type,
        size_bytes=len(reply.content),
        sha256=hashlib.sha256(reply.content).hexdigest(),
    )


def _failure(error: RunnerCallError) -> Failure:
    kind: FailureKind
    if error.kind == "http":
        status = error.status_code or 0
        kind = "runner_rejected" if 400 <= status < 500 else "runner_error"
    elif error.kind == "payment_refused":
        kind = "payment"
    else:
        kind = cast(FailureKind, error.kind)
    return Failure(
        kind=kind,
        message=str(error),
        status_code=error.status_code,
        body=error.body[:_BODY_LIMIT] if error.body else None,
        payment_sent=error.payment_sent,
    )
