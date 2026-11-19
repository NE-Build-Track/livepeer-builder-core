"""The paid call: one job's attempts on the runners that can do it, with failover, written evidence and
the job's end (D52 to D55). ``JobService`` runs an immediate job and opens a stream through it, and the
async worker runs a queued job through it (ISS-26), so the money rules are in one place.

Rules this module enforces (docs/contracts/engine.md#behavior-the-contract-fixes):
1. Fail over when another runner can do what this one did not, and only to another orchestrator after
   a payment (services/failover.py, D52). A paid failure that may have run the work, or whose payment is
   unknown, is ``uncertain``; one that ran, or is about the request, is ``failed``.
3. An attempt row is written *before* the call and updated after it. Paid-but-unknown is
   ``uncertain``, never ``failed``.
4. The allocation is checked before any payment.
6. Opening a stream is bounded by ``first_byte_timeout_s`` and the job's ``timeout_s`` (D35). The
   transport enforces it and keeps the payment evidence; the engine's backstop catches a transport
   that does not, and treats the payment as unknown.
"""

import asyncio
import contextlib
import hashlib
import logging
from collections.abc import Sequence
from typing import cast

from livepeer_builder.contracts import (
    ActorContext,
    Attempt,
    Failure,
    FailureKind,
    Job,
    JobRequest,
    JobResult,
    JobState,
    PaymentKey,
    ResultRef,
    Runner,
    RunnerReply,
)
from livepeer_builder.errors import JobFailed, NotProvisioned, RunnerCallError
from livepeer_builder.protocols import (
    Clock,
    CredentialResolver,
    JobRepo,
    RunnerStream,
    RunnerTransport,
    SelectionPolicy,
    StreamingRunnerTransport,
)
from livepeer_builder.services.discovery import DiscoveryService, capped_first, paid_candidates
from livepeer_builder.services.evidence import SignedSessionRecorder, store_evidence, with_evidence
from livepeer_builder.services.failover import (
    Candidates,
    attempt_verdict,
    failure_of,
    payment_unresolved,
    returned_failover,
    stream_timings,
)
from livepeer_builder.services.streams import StreamLimits
from livepeer_builder.utils import run_shielded

_LOG = logging.getLogger(__name__)


class Dispatcher:
    def __init__(
        self,
        store: JobRepo,
        discovery: DiscoveryService,
        transport: RunnerTransport,
        selection: SelectionPolicy,
        credentials: CredentialResolver,
        clock: Clock,
        stream_limits: StreamLimits | None = None,
        max_paid_attempts: int = 2,
    ) -> None:
        self._store = store
        self._discovery = discovery
        self._transport = transport
        self._selection = selection
        self._credentials = credentials
        self._clock = clock
        self._stream_limits = stream_limits or StreamLimits()
        self._max_paid_attempts = max_paid_attempts  # D55

    async def _candidates(
        self, job: Job, actor: ActorContext, request: JobRequest, attempts: list[Attempt]
    ) -> tuple[Candidates, PaymentKey]:
        """Who could run this, and with which credential. Fails the job (before payment) when nobody can."""
        candidates = self._discovery.candidates(request.app)
        if not candidates:
            raise await self.fail(job, attempts, "no_offering", f"no runner advertises app {request.app!r}")
        try:
            credential = await self._credentials.credential(actor)
        except NotProvisioned as exc:
            raise await self.fail(job, attempts, "not_provisioned", str(exc)) from exc
        paid = credential.token is not None and getattr(self._transport, "pays", True)  # no signer, no pay
        # A runner advertising zero capacity stays a candidate (#114, C17): the hint can lag, selection
        # tries it last, and if every runner refuses the job fails ``refused`` through failover. A job
        # that pays is not tried on runners that all advertise zero: a full runner's refusal can cost a
        # ticket.
        if paid and not (candidates := paid_candidates(candidates)):
            raise await self.fail(job, attempts, "no_capacity", f"every runner for {request.app!r} is busy")
        ordered = tuple(self._selection.order(candidates, request))
        if paid:  # a runner with no usable ceiling is refused at its 402 (D62)
            ordered = capped_first(ordered)
        if not ordered:  # an enterprise SelectionPolicy may filter every runner out
            raise await self.fail(job, attempts, "no_offering", f"no runner for {request.app!r} selected")
        return Candidates(ordered, request.max_attempts, self._max_paid_attempts, job.id), credential

    async def _start_attempt(
        self, job: Job, number: int, runner: Runner, credential: PaymentKey, attempts: list[Attempt]
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

        Evidence the signer reported stays: a signed session keeps the attempt paid. A paid attempt that
        fails over takes its orchestrator out of the candidates, so the job never pays it twice.
        """
        outcome = "payment_unknown" if error.payment_unknown else error.kind  # stored: recovery must see it
        attempts[-1] = with_evidence(attempts[-1], error.manifest_ids, error.payment_sent).model_copy(
            update={
                "outcome": outcome,
                "status_code": error.status_code,
                "ended_at": self._clock.now(),
                "timings": error.timings,
            }
        )
        await self._store.finish_attempt(attempts[-1])
        paid = attempts[-1].payment_sent
        verdict = attempt_verdict(error.kind, error.status_code, paid, payment_unknown=error.payment_unknown)
        if verdict == "failover":
            if paid:
                candidates.paid_failover(runner)
            return None
        state: JobState = "uncertain" if verdict == "uncertain" else "failed"
        failure = failure_of(error, any(attempt.payment_sent for attempt in attempts))  # earlier ones too
        return JobFailed(await self.finish(job, state, failure, None, attempts))

    async def _exhausted(self, job: Job, attempts: list[Attempt], last: RunnerCallError) -> JobFailed:
        """Every runner tried failed over, so none ran the work: the job is ``failed`` with the last
        runner's words. ``last`` is set: ``_candidates`` fails a job with no runner to try."""
        failure = failure_of(last, any(attempt.payment_sent for attempt in attempts), tried=len(attempts))
        return JobFailed(await self.finish(job, "failed", failure, None, attempts))

    async def dispatch(
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
                    "timings": reply.timings,
                }
            )
            await self._store.finish_attempt(attempts[-1])
            finished = await self.finish(job, "succeeded", None, _result_ref(reply), attempts)
            return JobResult(
                job=finished,
                status_code=reply.status_code,
                content=reply.content,
                content_type=reply.content_type,
                data=reply.data,
            )
        raise await self._exhausted(job, attempts, cast(RunnerCallError, last))

    async def open(
        self,
        job: Job,
        actor: ActorContext,
        request: JobRequest,
        attempts: list[Attempt],
        transport: StreamingRunnerTransport,
        deadline: float,
    ) -> tuple[RunnerStream, float]:
        """Like ``dispatch`` up to the runner's answer. Returns the stream and the first-byte time left."""
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
            if request.return_http_errors and (answer := returned_failover(opened, runner, candidates)):
                with contextlib.suppress(Exception):
                    await run_shielded(opened.aclose())  # stops any funding
                await self._attempt_failed(job, attempts, answer, runner, candidates)  # fails over
                last = answer
                continue
            # Payment evidence is recorded now, so a restart mid-stream resolves the job as uncertain.
            attempts[-1] = with_evidence(attempts[-1], opened.manifest_ids, opened.payment_sent).model_copy(
                update={"status_code": opened.status_code, "timings": stream_timings(opened)}
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

    async def fail(self, job: Job, attempts: list[Attempt], kind: FailureKind, message: str) -> JobFailed:
        failure = Failure(kind=kind, message=message)
        return JobFailed(await self.finish(job, "failed", failure, None, attempts))

    async def close_after_error(self, job: Job, attempts: list[Attempt], exc: BaseException) -> None:
        maybe_paid = payment_unresolved(attempts)  # as recover_interrupted
        failure = Failure(
            kind="runner_error",
            message=f"engine error: {type(exc).__name__}",
            payment_sent=any(a.payment_sent for a in attempts),
        )
        await store_evidence(self._store, attempts)  # the write at signing may have failed (D42)
        try:
            await self.finish(job, "uncertain" if maybe_paid else "failed", failure, None, attempts)
        except Exception:
            _LOG.exception("could not close job", extra={"event": "jobs.close_failed", "job_id": str(job.id)})

    async def finish(
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


def _result_ref(reply: RunnerReply) -> ResultRef:
    return ResultRef(
        storage="inline",
        content_type=reply.content_type,
        size_bytes=len(reply.content),
        sha256=hashlib.sha256(reply.content).hexdigest(),
    )
