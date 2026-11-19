"""Jobs: immediate execution, streaming, history and recovery. The paid call itself, with failover and
the durable attempts, is ``services/dispatch.py`` (``Dispatcher``).

Rules this module enforces (docs/contracts/engine.md#behavior-the-contract-fixes):
2. One (tenant, actor, operation_ref) dispatches at most once; a changed body is a conflict. A
   repeat while the job runs is ``OperationInProgress``; a repeat after a failure that never reached a
   runner dispatches a new job under the same reference (D57, services/operations.py).
3. Jobs left ``running`` by a restart are resolved on start.
4. Admission is checked before any payment.
5. A streamed job (docs/contracts/engine.md#streaming) is bound to its runner once the stream is
   open; it ends ``succeeded``, or ``uncertain`` when the runner failed, timed out, or the caller
   stopped reading (``abandoned``). The admission slot is held until the stream ends.
"""

import asyncio
import contextlib
import functools
import logging
from collections.abc import Mapping, Sequence
from datetime import datetime
from uuid import UUID, uuid4

from livepeer_builder.contracts import (
    ActorContext,
    Attempt,
    Job,
    JobRequest,
    JobResult,
    Operation,
    ResourceRef,
)
from livepeer_builder.errors import InvalidRequest, JobFailed, NotFound, NotImplementedInMilestone
from livepeer_builder.protocols import (
    AccessPolicy,
    Clock,
    JobRepo,
    RunnerTransport,
    StreamingRunnerTransport,
)
from livepeer_builder.services.admission import AdmissionLimiter
from livepeer_builder.services.dispatch import Dispatcher
from livepeer_builder.services.failover import recover_interrupted
from livepeer_builder.services.operations import check_operation, claim_operation
from livepeer_builder.services.ownership import check_owner, job_resource, sees_every_actor
from livepeer_builder.services.request_identity import request_fingerprint
from livepeer_builder.services.streams import JobStream, Streaming, StreamLimits
from livepeer_builder.services.usage_reports import report_once
from livepeer_builder.utils import run_shielded

_LOG = logging.getLogger(__name__)


class JobService:
    def __init__(
        self,
        store: JobRepo,
        dispatcher: Dispatcher,
        transport: RunnerTransport,
        policy: AccessPolicy,
        admission: AdmissionLimiter,
        clock: Clock,
        stream_limits: StreamLimits | None = None,
    ) -> None:
        self._store = store
        self._dispatcher = dispatcher
        self._transport = transport
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
        """Dispatch one immediate job and wait for its result. Raises JobFailed, OperationExists
        (``OperationInProgress`` while the reference's job runs) and OperationConflict."""
        if request.return_http_errors:
            raise InvalidRequest("return_http_errors requires jobs.streaming.stream")
        fingerprint, retrying = await self._admit_request(actor, request)
        async with self._admission.admit(request.app):
            job = self._new_job(actor, request)
            await claim_operation(self._store, job, fingerprint, retrying)
            attempts: list[Attempt] = []
            try:
                return await self._dispatcher.dispatch(job, actor, request, attempts)
            except JobFailed:
                raise
            except BaseException as exc:
                # Shielded as in stream (D34): a host that cancels every await must not cut this write off.
                await run_shielded(self._dispatcher.close_after_error(job, attempts, exc))
                raise

    async def _stream(
        self, actor: ActorContext, request: JobRequest, *, transport: StreamingRunnerTransport
    ) -> JobStream:
        """``jobs.streaming.stream``: like ``run`` up to the first byte (see ``Streaming``, streams.py)."""
        deadline = asyncio.get_running_loop().time() + request.timeout_s  # timeout_s counts from here
        fingerprint, retrying = await self._admit_request(actor, request)
        slot = contextlib.AsyncExitStack()
        await slot.enter_async_context(self._admission.admit(request.app))
        try:
            job = self._new_job(actor, request)
            await claim_operation(self._store, job, fingerprint, retrying)
            attempts: list[Attempt] = []
            try:
                opened, first_byte_left = await self._dispatcher.open(
                    job, actor, request, attempts, transport, deadline
                )
            except JobFailed:
                raise
            except BaseException as exc:
                await run_shielded(self._dispatcher.close_after_error(job, attempts, exc))
                raise
        except BaseException:
            await run_shielded(slot.aclose())
            raise
        stream = JobStream(
            store=self._store,
            clock=self._clock,
            finish=self._dispatcher.finish,
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
        """The caller's own jobs; an admin's are every actor's in its tenant, as the cost reads (D44)."""
        target = None if sees_every_actor(actor) else actor.actor_id
        self._policy.check(
            actor,
            Operation.JOBS_READ,
            ResourceRef(kind="job", tenant_id=actor.tenant_id, owner_actor_id=target),
        )
        return await self._store.list_jobs(actor.tenant_id, target, limit=limit, before=before)

    async def report_usage(self, actor: ActorContext, job_id: UUID, quantities: Mapping[str, int]) -> Job:
        """The application measured the work and reports it once (service-mode default): it is stored once,
        and an identical repeat is answered with the stored usage (#115, C20; ``services/usage_reports``).
        Running the job is the permission it needs, not reading it (D44)."""
        job = await self._owned(actor, Operation.JOBS_RUN, job_id)
        return await report_once(self._store, job, quantities, self._clock.now())

    async def recover_interrupted(self) -> int:
        """Resolve jobs a restart left ``running`` (``failover.recover_interrupted``)."""
        return await recover_interrupted(self._store, self._clock)

    # -- shared steps -------------------------------------------------------------------------------

    async def _owned(self, actor: ActorContext, operation: Operation, job_id: UUID) -> Job:
        """Permission (403), then the job, then ownership (404): the answer rule (services/ownership.py)."""
        self._policy.check(actor, operation)
        job = await self._store.get_job(job_id)
        if job is None:
            raise NotFound(f"job {job_id} not found")
        check_owner(self._policy, actor, operation, job_resource(job), f"job {job_id} not found")
        return job

    async def _admit_request(self, actor: ActorContext, request: JobRequest) -> tuple[str, UUID | None]:
        """Policy, mode, path and idempotency checks that run before anything is written. Returns the
        request's fingerprint and the failed job a new one supersedes, if the repeat may run again."""
        self._policy.check(actor, Operation.JOBS_RUN)
        if request.mode == "async":
            raise NotImplementedInMilestone("async jobs", "M4", "ISS-26")
        if request.path and not request.path.startswith("/"):
            raise InvalidRequest("path must start with '/'")
        fingerprint = request_fingerprint(request)
        if request.operation_ref is None:
            return fingerprint, None
        return fingerprint, await check_operation(self._store, actor, request.operation_ref, fingerprint)

    def _new_job(self, actor: ActorContext, request: JobRequest) -> Job:
        return Job(
            id=uuid4(),
            actor_id=actor.actor_id,
            tenant_id=actor.tenant_id,
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
