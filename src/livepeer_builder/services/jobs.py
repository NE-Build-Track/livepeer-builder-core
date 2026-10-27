"""Jobs: immediate execution with failover, durable attempts, streaming, and recovery.

Rules this module enforces (docs/contracts/engine.md#behavior-the-contract-fixes):
1. Fail over when another runner can do what this one did not (services/failover.py, D51): before
   payment, when the runner refused or never answered; after payment, when it was refused or failed on
   the runner's or orchestrator's side, and then only to another orchestrator. A paid failure that may
   have run the work is ``uncertain``; one about the request is ``failed``.
2. One (application, actor, operation_ref) dispatches at most once; a changed body is a conflict.
3. An attempt row is written *before* the call and updated after it. Paid-but-unknown is
   ``uncertain``, never ``failed``. Jobs left ``running`` by a restart are resolved on start.
4. Admission and provisioning are checked before any payment.
5. A streamed job (docs/contracts/engine.md#streaming) is bound to its runner once the stream is
   open; it ends ``succeeded``, or ``uncertain`` when the runner failed, timed out, or the caller
   stopped reading (``abandoned``). The admission slot is held until the stream ends.
6. Opening a stream is bounded by ``first_byte_timeout_s`` and the job's ``timeout_s`` (D35). The
   transport enforces it and keeps the payment evidence; the engine's backstop catches a transport
   that does not, and treats the payment as unknown.
"""

import asyncio
import contextlib
import functools
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
    PaymentCredential,
    ResourceRef,
    ResultRef,
    Runner,
    RunnerReply,
)
from livepeer_builder.errors import (
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
    RunnerStream,
    RunnerTransport,
    SelectionPolicy,
    StreamingRunnerTransport,
)
from livepeer_builder.services.admission import AdmissionLimiter
from livepeer_builder.services.discovery import DiscoveryService
from livepeer_builder.services.evidence import SignedSessionRecorder, store_evidence, with_evidence
from livepeer_builder.services.failover import Candidates, attempt_verdict, failure_of
from livepeer_builder.services.ownership import check_owner, job_resource
from livepeer_builder.services.streams import JobStream, Streaming, StreamLimits
from livepeer_builder.utils import run_shielded

_LOG = logging.getLogger(__name__)


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
        stream_limits: StreamLimits | None = None,
    ) -> None:
        self._store = store
        self._discovery = discovery
        self._transport = transport
        self._selection = selection
        self._credentials = credentials
        self._policy = policy
        self._admission = admission
        self._clock = clock
        self._stream_limits = stream_limits or StreamLimits()
        self.streaming: Streaming | None = (  # decided once (D33): only a transport with open() streams
            Streaming(functools.partial(self._stream, transport=transport))
            if isinstance(transport, StreamingRunnerTransport)
            else None
        )
        self._open_streams: set[JobStream] = set()

    async def run(self, actor: ActorContext, request: JobRequest) -> JobResult:
        """Dispatch one immediate job and wait for its result. Raises JobFailed, OperationExists."""
        fingerprint = await self._admit_request(actor, request)
        async with self._admission.admit(request.app):
            job = self._new_job(actor, request)
            await self._store.create_job(job, fingerprint)
            attempts: list[Attempt] = []
            try:
                return await self._dispatch(job, actor, request, attempts)
            except JobFailed:
                raise
            except BaseException as exc:
                # Shielded as in stream (D34): a host that cancels every await must not cut this write off.
                await run_shielded(self._close_after_error(job, attempts, exc))
                raise

    async def _stream(
        self, actor: ActorContext, request: JobRequest, *, transport: StreamingRunnerTransport
    ) -> JobStream:
        """``jobs.streaming.stream``: like ``run`` up to the first byte (see ``Streaming``, streams.py)."""
        deadline = asyncio.get_running_loop().time() + request.timeout_s  # timeout_s counts from here
        fingerprint = await self._admit_request(actor, request)
        slot = contextlib.AsyncExitStack()
        await slot.enter_async_context(self._admission.admit(request.app))
        try:
            job = self._new_job(actor, request)
            await self._store.create_job(job, fingerprint)
            attempts: list[Attempt] = []
            try:
                opened, first_byte_left = await self._open(job, actor, request, attempts, transport, deadline)
            except JobFailed:
                raise
            except BaseException as exc:
                await run_shielded(self._close_after_error(job, attempts, exc))
                raise
        except BaseException:
            await run_shielded(slot.aclose())
            raise
        stream = JobStream(
            store=self._store,
            clock=self._clock,
            finish=self._finish,
            job=job,
            attempts=attempts,
            stream=opened,
            request=request,
            slot=slot,
            limits=self._stream_limits,
            deadline=deadline,
            first_byte_left=first_byte_left,
            on_done=self._open_streams.discard,
        )
        self._open_streams.add(stream)
        return stream

    async def close_streams(self) -> None:
        """Engine shutdown: let open streams finish for up to ``drain_timeout_s``, then abandon the rest.

        Returns once every stream's end is written, including ones already finishing, so the store
        is never closed under a final write (each is bounded by ``finalize_timeout_s``)."""
        open_now = list(self._open_streams)
        if open_now:
            waits = [asyncio.ensure_future(stream.wait_ended()) for stream in open_now]
            _, pending = await asyncio.wait(waits, timeout=self._stream_limits.drain_timeout_s)
            for waiting in pending:
                waiting.cancel()
            left = [stream for stream in open_now if not stream.finished]
            _LOG.info("closed open streams", extra={"event": "jobs.streams_closed", "abandoned": len(left)})
            message = "the engine stopped while the stream was open"
            finishing = [stream.wait_ended() for stream in open_now if stream not in left]
            await asyncio.gather(*(stream.abandon(message) for stream in left), *finishing)

    async def submit(self, actor: ActorContext, request: JobRequest) -> Job:
        del request
        self._policy.check(actor, Operation.JOBS_RUN)
        raise NotImplementedInMilestone("async jobs", "M4", "ISS-26")

    async def result(self, actor: ActorContext, job_id: UUID) -> JobResult:
        await self._owned(actor, Operation.JOBS_READ, job_id)
        raise NotImplementedInMilestone("stored job results", "M4", "ISS-26")

    async def get(self, actor: ActorContext, job_id: UUID) -> Job:
        return await self._owned(actor, Operation.JOBS_READ, job_id)

    async def list_jobs(
        self, actor: ActorContext, *, limit: int = 50, before: datetime | None = None
    ) -> Sequence[Job]:
        """The caller's own jobs; an admin's are every actor's in its application, as the cost reads (D44)."""
        target = None if actor.credential_kind == "admin" else actor.actor_id
        self._policy.check(
            actor,
            Operation.JOBS_READ,
            ResourceRef(kind="job", application_id=actor.application_id, owner_actor_id=target),
        )
        return await self._store.list_jobs(actor.application_id, target, limit=limit, before=before)

    async def report_usage(self, actor: ActorContext, job_id: UUID, quantities: Mapping[str, int]) -> Job:
        """The application measured the work and reports it once (service-mode default). Running the job
        is the permission it needs, not reading it (D44)."""
        job = await self._owned(actor, Operation.JOBS_RUN, job_id)
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

    # -- shared steps -------------------------------------------------------------------------------

    async def _owned(self, actor: ActorContext, operation: Operation, job_id: UUID) -> Job:
        """Permission (403), then the job, then ownership (404): the answer rule (services/ownership.py)."""
        self._policy.check(actor, operation)
        job = await self._store.get_job(job_id)
        if job is None:
            raise NotFound(f"job {job_id} not found")
        check_owner(self._policy, actor, operation, job_resource(job), f"job {job_id} not found")
        return job

    async def _admit_request(self, actor: ActorContext, request: JobRequest) -> str:
        """Policy, mode, path and idempotency checks that run before anything is written."""
        self._policy.check(actor, Operation.JOBS_RUN)
        if request.mode == "async":
            raise NotImplementedInMilestone("async jobs", "M4", "ISS-26")
        if request.path and not request.path.startswith("/"):
            raise InvalidRequest("path must start with '/'")
        fingerprint = request_fingerprint(request)
        if request.operation_ref is not None:
            await self._check_operation(actor, request.operation_ref, fingerprint)
        return fingerprint

    async def _check_operation(self, actor: ActorContext, operation_ref: str, fingerprint: str) -> None:
        found = await self._store.job_by_operation_ref(actor.application_id, actor.actor_id, operation_ref)
        if found is None:
            return
        existing, existing_fingerprint = found
        if existing_fingerprint is not None and existing_fingerprint != fingerprint:
            raise OperationConflict(existing)
        raise OperationExists(existing)

    async def _candidates(
        self, job: Job, actor: ActorContext, request: JobRequest, attempts: list[Attempt]
    ) -> tuple[Candidates, PaymentCredential]:
        """Who could run this, and with which credential. Fails the job (before payment) when nobody can."""
        candidates = self._discovery.candidates(request.app)
        if not candidates:
            raise await self._fail(job, attempts, "no_offering", f"no runner advertises app {request.app!r}")
        if all(r.capacity_available is not None and r.capacity_available <= 0 for r in candidates):
            raise await self._fail(job, attempts, "no_capacity", f"every runner for {request.app!r} is busy")
        try:
            credential = await self._credentials.credential(actor)
        except NotProvisioned as exc:
            raise await self._fail(job, attempts, "not_provisioned", str(exc)) from exc
        return Candidates(self._selection.order(candidates, request), request.max_attempts), credential

    async def _start_attempt(
        self, job: Job, number: int, runner: Runner, credential: PaymentCredential, attempts: list[Attempt]
    ) -> Attempt:
        attempt = Attempt(
            job_id=job.id,
            number=number,
            runner_url=runner.url,
            orchestrator_url=runner.orchestrator_url,
            allocation_id=credential.allocation_id,
            sessions=(),
            payment_sent=False,
            outcome="in_flight",
            status_code=None,
            started_at=self._clock.now(),
            ended_at=None,
        )
        await self._store.start_attempt(attempt)
        attempts.append(attempt)
        return attempt

    async def _attempt_failed(
        self,
        job: Job,
        attempts: list[Attempt],
        error: RunnerCallError,
        runner: Runner,
        candidates: Candidates,
    ) -> JobFailed | None:
        """Record the error on the current attempt. Returns the JobFailed to raise, or None to fail over.

        Evidence the signer reported during the call stays: an error that says ``payment_sent=False``
        after a session was signed still leaves the attempt paid. A paid attempt that fails over takes
        its orchestrator out of the candidates, so the job never pays it twice.
        """
        attempts[-1] = with_evidence(attempts[-1], error.manifest_ids, error.payment_sent).model_copy(
            update={"outcome": error.kind, "status_code": error.status_code, "ended_at": self._clock.now()}
        )
        await self._store.finish_attempt(attempts[-1])
        paid = attempts[-1].payment_sent
        verdict = attempt_verdict(error.kind, error.status_code, paid)
        if verdict == "failover":
            if paid:
                candidates.exclude(runner)
            return None
        state: JobState = "uncertain" if verdict == "uncertain" else "failed"
        return JobFailed(await self._finish(job, state, failure_of(error, paid), None, attempts))

    async def _exhausted(self, job: Job, attempts: list[Attempt], last: RunnerCallError) -> JobFailed:
        """Every runner tried failed over: the job fails with the last runner's words. It is ``failed``,
        not ``uncertain``, because none of those failures may have run the work. ``last`` is never
        None here: the first candidate is always tried, and only a failover leaves the loop."""
        failure = failure_of(last, any(attempt.payment_sent for attempt in attempts), tried=len(attempts))
        return JobFailed(await self._finish(job, "failed", failure, None, attempts))

    async def _dispatch(
        self, job: Job, actor: ActorContext, request: JobRequest, attempts: list[Attempt]
    ) -> JobResult:
        candidates, credential = await self._candidates(job, actor, request, attempts)
        last: RunnerCallError | None = None
        for number, runner in enumerate(candidates, start=1):
            await self._start_attempt(job, number, runner, credential, attempts)
            recorder = SignedSessionRecorder(self._store, attempts)
            try:
                reply = await self._transport.call(runner, request, credential.token, on_signed=recorder)
            except RunnerCallError as error:
                failed = await self._attempt_failed(job, attempts, error, runner, candidates)
                if failed is not None:
                    raise failed from error
                last = error
                continue
            attempts[-1] = with_evidence(attempts[-1], reply.manifest_ids, reply.payment_sent).model_copy(
                update={
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
        raise await self._exhausted(job, attempts, cast(RunnerCallError, last))

    async def _open(
        self,
        job: Job,
        actor: ActorContext,
        request: JobRequest,
        attempts: list[Attempt],
        transport: StreamingRunnerTransport,
        deadline: float,
    ) -> tuple[RunnerStream, float]:
        """Like ``_dispatch`` up to the runner's answer. Returns the stream and the first-byte time left."""
        loop = asyncio.get_running_loop()
        candidates, credential = await self._candidates(job, actor, request, attempts)
        last: RunnerCallError | None = None
        for number, runner in enumerate(candidates, start=1):
            await self._start_attempt(job, number, runner, credential, attempts)
            recorder = SignedSessionRecorder(self._store, attempts)
            started = loop.time()
            budget = min(request.first_byte_timeout_s, deadline - started)
            bounded = request.model_copy(update={"first_byte_timeout_s": max(budget, 0.001)})
            try:
                async with asyncio.timeout(max(budget, 0) + self._stream_limits.open_grace_s):
                    opened = await transport.open(runner, bounded, credential.token, on_signed=recorder)
            except RunnerCallError as error:
                failed = await self._attempt_failed(job, attempts, error, runner, candidates)
                if failed is not None:
                    raise failed from error
                last = error
                continue
            except TimeoutError as error:
                # The transport ignored its deadline. Payment is unknown, so assume it was sent.
                unknown = RunnerCallError("timeout", "no answer within first_byte_timeout", payment_sent=True)
                failed = await self._attempt_failed(job, attempts, unknown, runner, candidates)
                raise cast(JobFailed, failed) from error
            # Payment evidence is recorded now, so a restart mid-stream resolves the job as uncertain.
            attempts[-1] = with_evidence(attempts[-1], opened.manifest_ids, opened.payment_sent).model_copy(
                update={"status_code": opened.status_code}
            )
            try:
                await self._store.finish_attempt(attempts[-1])
            except BaseException:
                await run_shielded(self._discard_opened(opened, attempts[-1]))
                raise
            return opened, request.first_byte_timeout_s - (loop.time() - started)
        raise await self._exhausted(job, attempts, cast(RunnerCallError, last))

    async def _discard_opened(self, opened: RunnerStream, attempt: Attempt) -> None:
        """Recording an opened stream failed: close it, and keep its manifests if the store allows."""
        with contextlib.suppress(Exception):
            await opened.aclose()
        with contextlib.suppress(Exception):
            await self._store.finish_attempt(attempt)

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
        await store_evidence(self._store, attempts)  # the write at signing may have failed (D42)
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


def _result_ref(reply: RunnerReply) -> ResultRef:
    return ResultRef(
        storage="inline",
        content_type=reply.content_type,
        size_bytes=len(reply.content),
        sha256=hashlib.sha256(reply.content).hexdigest(),
    )
