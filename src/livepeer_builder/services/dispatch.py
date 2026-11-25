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

7. A job that is not a stream has one deadline, its ``timeout_s`` counted from its start (an async
   job's from its claim): each attempt is given only the time left, measured once its row is written,
   and no runner is called after it.

An async job runs under a claim (ISS-26), and ``dispatch`` is told its number. Every write for the job
then carries the number and applies only while that claim holds the job: a miss is ``ClaimLost``, and
nothing more is written. And a job that cannot be dispatched for a reason that passes, before anything
was attempted, is not failed: ``NotNow`` hands it back to the worker, which lets it wait
(``services/job_runners.py``, which says who could run a job).
"""

import asyncio
import contextlib
import hashlib
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import cast
from uuid import UUID

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
from livepeer_builder.contracts.http import response_content_type
from livepeer_builder.errors import JobFailed, RunnerCallError
from livepeer_builder.protocols import (
    BoundedRunnerTransport,
    Clock,
    CredentialResolver,
    JobRepo,
    RunnerStream,
    RunnerTransport,
    SelectionPolicy,
    StreamingRunnerTransport,
)
from livepeer_builder.services.discovery import DiscoveryService
from livepeer_builder.services.evidence import SignedSessionRecorder, store_evidence, with_evidence
from livepeer_builder.services.failover import (
    Candidates,
    attempt_verdict,
    error_end,
    failure_of,
    returned_failover,
    stream_timings,
)
from livepeer_builder.services.job_runners import JobRunners, Nobody
from livepeer_builder.services.streams import StreamLimits
from livepeer_builder.utils import run_shielded

_LOG = logging.getLogger(__name__)

# How a succeeded job ends: given the job, the runner's reply and the attempts, write the end and return
# the job as it ended.
KeepResult = Callable[[Job, RunnerReply, Sequence[Attempt]], Awaitable[Job]]


class ClaimLost(Exception):
    """A write for an async job found that the claim it was made under no longer holds the job: the job
    was put back and claimed again, or ended by another writer. Nothing was written, and whoever
    catches this writes nothing more for the job and does not end it."""

    def __init__(self, job_id: UUID, claim: int | None) -> None:
        super().__init__(f"job {job_id}: claim {claim} no longer holds it")


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
        self._transport = transport
        self._credentials = credentials
        self._clock = clock
        self._stream_limits = stream_limits or StreamLimits()
        self._runners = JobRunners(discovery, transport, selection, credentials, max_paid_attempts)
        # Decided once, as streaming is (D33): only a transport with call_bounded reads with a bound.
        self._bounded = transport if isinstance(transport, BoundedRunnerTransport) else None

    async def check_allocation(self, actor: ActorContext) -> None:
        """Raise ``NotProvisioned`` for an actor with no allocation to pay from. An async job is checked
        here when it is submitted, to fail fast, and again when it is dispatched (ISS-26)."""
        await self._credentials.credential(actor)

    def waits(self, app: str) -> bool:
        """Whether a queued job of ``app`` that pays could not be dispatched now (``JobRunners.waits``)."""
        return self._runners.waits(app)

    async def _candidates(
        self,
        job: Job,
        actor: ActorContext,
        request: JobRequest,
        attempts: list[Attempt],
        claim: int | None = None,
    ) -> tuple[Candidates, PaymentKey]:
        """Who could run this, and with which credential. Fails the job (before payment) when nobody
        can. An async job (``claim``) is not failed for what may pass: that is ``NotNow``."""
        try:
            return await self._runners.choose(job.id, actor, request, queued=claim is not None)
        except Nobody as nobody:
            failed = await self.fail(job, attempts, nobody.kind, str(nobody), claim=claim)
            raise failed from nobody

    async def _start_attempt(
        self,
        job: Job,
        number: int,
        runner: Runner,
        credential: PaymentKey,
        attempts: list[Attempt],
        claim: int | None = None,
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
        if claim is None:
            await self._store.start_attempt(attempt)
        elif await self._store.start_attempt(attempt, claim=claim) is False:
            raise ClaimLost(job.id, claim)  # before any runner call
        attempts.append(attempt)
        return attempt

    async def _in_time(
        self,
        job: Job,
        request: JobRequest,
        attempts: list[Attempt],
        deadline: float,
        claim: int | None,
        *,
        written: bool = False,
    ) -> JobRequest:
        """The request the next attempt is called with: its ``timeout_s`` is the time left until
        ``deadline``, so a job's attempts share one ``timeout_s`` between them, the whole job's. With
        none left the job ends ``timeout`` here, and no runner is called or paid again. The attempts
        that were called failed over, so none of them left the work in doubt: the job is ``failed``.

        It is asked twice for each attempt: before the attempt's row is written, so that no row is
        written for a call that will not be made, and with ``written`` once it is, because that write
        takes time and the transport counts its timeout from the call. A row already written is then
        ended as the call it stands for never having been made (``_never_called``)."""
        left = deadline - asyncio.get_running_loop().time()
        if left > 0:
            return request.model_copy(update={"timeout_s": left})
        called = attempts
        if written:
            called = attempts[:-1]
            await self._never_called(attempts)
        failure = Failure(
            kind="timeout",
            message=f"timeout_s ran out before {'another' if called else 'a'} runner was called",
            payment_sent=any(attempt.payment_sent for attempt in attempts),
        )
        failed = JobFailed(await self.finish(job, "failed", failure, None, attempts, claim=claim))
        raise failed

    async def _never_called(self, attempts: list[Attempt]) -> None:
        """End the attempt whose row was written and whose runner was then not called. It is recorded
        ``refused``, unpaid, with no status code, no session and no timings: the engine refused the
        call itself, before a request left it. Everything that outcome says to a reader is true here:
        nothing ran, nothing was paid, and the same reference may run again. ``timeout`` would say a
        runner was asked and did not answer in time, which may have run the work."""
        attempts[-1] = attempts[-1].model_copy(update={"outcome": "refused", "ended_at": self._clock.now()})
        await self._store.finish_attempt(attempts[-1])

    async def _attempt_failed(
        self,
        job: Job,
        attempts: list[Attempt],
        error: RunnerCallError,
        runner: Runner,
        candidates: Candidates,
        claim: int | None = None,
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
        return JobFailed(await self.finish(job, state, failure, None, attempts, claim=claim))

    async def _exhausted(
        self, job: Job, attempts: list[Attempt], last: RunnerCallError, claim: int | None = None
    ) -> JobFailed:
        """Every runner tried failed over, so none ran the work: the job is ``failed`` with the last
        runner's words. ``last`` is set: ``_candidates`` fails a job with no runner to try."""
        failure = failure_of(last, any(attempt.payment_sent for attempt in attempts), tried=len(attempts))
        return JobFailed(await self.finish(job, "failed", failure, None, attempts, claim=claim))

    async def dispatch(
        self,
        job: Job,
        actor: ActorContext,
        request: JobRequest,
        attempts: list[Attempt],
        *,
        deadline: float,
        keep: KeepResult | None = None,
        keep_bytes: int | None = None,
        claim: int | None = None,
    ) -> JobResult:
        """Run ``job`` on the runners that can do it, until one answers or none is left.

        ``deadline`` is the loop time at which the job's ``timeout_s`` runs out: counted from the job's
        start for an immediate job and from its claim for an async one, once for all its attempts
        (``_in_time``). ``keep`` ends a succeeded job in place of the default, which records the shape of
        a result returned inline: the async worker stores the bytes there (ISS-26). ``keep_bytes`` is the
        most of a reply ``keep`` has a use for: a transport that can (``BoundedRunnerTransport``) then
        holds no more of it. ``claim`` is the number of the claim an async job runs under: every write
        then checks it (``ClaimLost``), and a job nobody can take now is ``NotNow``."""
        candidates, credential = await self._candidates(job, actor, request, attempts, claim)
        last: RunnerCallError | None = None
        for number, runner in enumerate(candidates, start=1):
            await self._in_time(job, request, attempts, deadline, claim)
            await self._start_attempt(job, number, runner, credential, attempts, claim)
            timed = await self._in_time(job, request, attempts, deadline, claim, written=True)
            recorder = SignedSessionRecorder(self._store, attempts)
            try:
                reply = await self._call(runner, timed, credential.token, recorder, keep_bytes)
            except RunnerCallError as error:
                failed = await self._attempt_failed(job, attempts, error, runner, candidates, claim)
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
            finished = await (keep or self._returned_inline)(job, reply, attempts)
            return JobResult(
                job=finished,
                status_code=reply.status_code,
                content=reply.content,
                content_type=response_content_type(reply.content_type),
                data=reply.data,
            )
        raise await self._exhausted(job, attempts, cast(RunnerCallError, last), claim)

    async def _call(
        self,
        runner: Runner,
        request: JobRequest,
        token: str | None,
        recorder: SignedSessionRecorder,
        keep_bytes: int | None,
    ) -> RunnerReply:
        if keep_bytes is None or self._bounded is None:  # an immediate job, or a transport that reads whole
            return await self._transport.call(runner, request, token, on_signed=recorder)
        return await self._bounded.call_bounded(
            runner, request, token, on_signed=recorder, max_bytes=keep_bytes
        )

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

    async def fail(
        self,
        job: Job,
        attempts: list[Attempt],
        kind: FailureKind,
        message: str,
        *,
        claim: int | None = None,
    ) -> JobFailed:
        failure = Failure(kind=kind, message=message)
        return JobFailed(await self.finish(job, "failed", failure, None, attempts, claim=claim))

    async def close_after_error(self, job: Job, attempts: list[Attempt], exc: BaseException) -> None:
        """End a job the engine, not a runner, stopped: ``uncertain`` if it may have paid. A write that
        fails is logged, not raised: what stopped the job is what its caller is told."""
        state, failure = error_end(attempts, type(exc).__name__)
        try:
            await self.close(job, attempts, state, failure)
        except Exception:
            _LOG.exception("could not close job", extra={"event": "jobs.close_failed", "job_id": str(job.id)})

    async def close(
        self,
        job: Job,
        attempts: Sequence[Attempt],
        state: JobState,
        failure: Failure,
        *,
        claim: int | None = None,
    ) -> None:
        """Write the end of a job that was stopped: the evidence its attempts hold, then its state.
        Raises what the store raises, so the async worker, which owns a claimed job's end, can write it
        again later: both writes can be repeated."""
        await store_evidence(self._store, attempts)  # the write at signing may have failed (D42)
        await self.finish(job, state, failure, None, attempts, claim=claim)

    async def _returned_inline(self, job: Job, reply: RunnerReply, attempts: Sequence[Attempt]) -> Job:
        return await self.finish(job, "succeeded", None, _result_ref(reply), attempts)

    async def finish(
        self,
        job: Job,
        state: JobState,
        failure: Failure | None,
        result: ResultRef | None,
        attempts: Sequence[Attempt],
        *,
        content: bytes | None = None,
        claim: int | None = None,
    ) -> Job:
        """Write the job's end. With ``content``, the bytes of a succeeded job's result are stored in the
        same transaction (ISS-26). Only a running job ends: one that already ended is left as it is, and
        that is logged, not raised, since the caller has nothing to undo. With ``claim``, an async
        job's, the job ends only while that claim holds it, and a miss is ``ClaimLost``."""
        completed = self._clock.now()
        fence = {} if claim is None else {"claim": claim}  # a store never asked to claim is never told
        if content is not None and result is not None:
            ended = await self._store.finish_job_with_result(job.id, result, content, completed, **fence)
        else:
            ended = await self._store.finish_job(job.id, state, failure, result, completed, **fence)
        if ended is False and claim is not None:
            raise ClaimLost(job.id, claim)
        if ended is False:  # a supplied store that answers nothing has written, as it did before the guard
            _LOG.warning(
                "the job had already ended; its record is unchanged",
                extra={"event": "jobs.finish_missed", "job_id": str(job.id), "state": state},
            )
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
        content_type=response_content_type(reply.content_type),  # one rule (#158)
        size_bytes=len(reply.content),
        sha256=hashlib.sha256(reply.content).hexdigest(),
    )
