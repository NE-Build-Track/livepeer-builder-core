"""A streamed job's life after the first byte (docs/contracts/engine.md#streaming).

``JobStream`` owns the open runner stream, the admission slot and the attempt row. It finishes the
job exactly once: ``succeeded`` when the runner closes the stream, otherwise ``uncertain`` (paid) or
``failed`` (unpaid) with the outcome that ended it: a runner error, a timeout, or ``abandoned``
when the caller stopped reading.
"""

import asyncio
import contextlib
import hashlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from typing import cast

from livepeer_builder.contracts import (
    Attempt,
    AttemptOutcome,
    Failure,
    FailureKind,
    Job,
    JobRequest,
    JobState,
    ResultRef,
    SignedSession,
)
from livepeer_builder.errors import JobFailed, RunnerCallError
from livepeer_builder.protocols import Clock, JobRepo, RunnerStream

_LOG = logging.getLogger(__name__)
_BODY_LIMIT = 4096
FinishJob = Callable[[Job, JobState, Failure | None, ResultRef | None, Sequence[Attempt]], Awaitable[Job]]


class JobStream:
    """A running job's response, delivered as it arrives (docs/contracts/engine.md#streaming).

    Iterate it to the end and the job succeeds with a ``ResultRef`` for the bytes that passed. A
    runner failure, an idle gap longer than ``idle_timeout_s`` or a total time over ``timeout_s``
    ends it with ``JobFailed`` and the job ``uncertain`` (the work was paid for). Stopping early,
    by ``aclose()`` or by cancelling the consumer, records the attempt as ``abandoned``.
    """

    def __init__(
        self,
        *,
        store: JobRepo,
        clock: Clock,
        finish: FinishJob,
        job: Job,
        attempts: list[Attempt],
        stream: RunnerStream,
        request: JobRequest,
        slot: contextlib.AsyncExitStack,
    ) -> None:
        self._store = store
        self._clock = clock
        self._finish = finish
        self._job = job.model_copy(update={"attempts": tuple(attempts)})
        self._attempts = attempts
        self._stream = stream
        self._request = request
        self._slot = slot
        self._hash = hashlib.sha256()
        self._size = 0
        self._finished = False

    @property
    def job(self) -> Job:
        """The job as last known: ``running`` while the stream is open, final afterwards."""
        return self._job

    @property
    def status_code(self) -> int:
        return self._stream.status_code

    @property
    def content_type(self) -> str:
        return self._stream.content_type

    @property
    def finished(self) -> bool:
        return self._finished

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[bytes]:
        if self._finished:
            return
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._request.timeout_s
        idle = self._request.idle_timeout_s
        chunks = self._stream.__aiter__()
        try:
            while True:
                remaining = deadline - loop.time()
                wait = remaining if idle is None else min(remaining, idle)
                if wait <= 0:
                    raise TimeoutError
                try:
                    chunk = await asyncio.wait_for(chunks.__anext__(), timeout=wait)
                except StopAsyncIteration:
                    break
                self._hash.update(chunk)
                self._size += len(chunk)
                yield chunk
        except TimeoutError:
            await self._end("timeout", "the runner stopped sending before the stream ended")
            raise JobFailed(self._job) from None
        except RunnerCallError as error:
            await self._end(error.kind, str(error), status_code=error.status_code, body=error.body)
            raise JobFailed(self._job) from error
        except (asyncio.CancelledError, GeneratorExit):
            await self._end("abandoned", "the caller stopped reading the stream")
            raise
        except BaseException as exc:
            await self._end("http", f"engine error: {type(exc).__name__}")
            raise
        await self._succeed()

    async def aclose(self) -> None:
        """Abandon the stream: stop reading, release the runner and the admission slot."""
        await self._end("abandoned", "the caller stopped reading the stream")

    async def _succeed(self) -> None:
        if self._finished:
            return
        self._finished = True
        await self._release()
        await self._store_attempt("succeeded")
        result = ResultRef(
            storage="inline",
            content_type=self._stream.content_type,
            size_bytes=self._size,
            sha256=self._hash.hexdigest(),
        )
        self._job = await self._finish(self._job, "succeeded", None, result, self._attempts)
        await self._slot.aclose()

    async def _end(
        self,
        outcome: AttemptOutcome,
        message: str,
        *,
        status_code: int | None = None,
        body: str | None = None,
    ) -> None:
        if self._finished:
            return
        self._finished = True
        await self._release()
        await self._store_attempt(outcome, status_code)
        paid = self._attempts[-1].payment_sent
        failure = Failure(
            kind=failure_kind(outcome, status_code),
            message=message,
            status_code=status_code,
            body=body[:_BODY_LIMIT] if body else None,
            payment_sent=paid,
        )
        state: JobState = "uncertain" if paid else "failed"
        self._job = await self._finish(self._job, state, failure, None, self._attempts)
        _LOG.info(
            "stream ended",
            extra={
                "event": "jobs.stream_ended",
                "job_id": str(self._job.id),
                "outcome": outcome,
                "state": state,
            },
        )
        await self._slot.aclose()

    async def _release(self) -> None:
        with contextlib.suppress(Exception):
            await self._stream.aclose()

    async def _store_attempt(self, outcome: AttemptOutcome, status_code: int | None = None) -> None:
        current = self._attempts[-1]
        self._attempts[-1] = current.model_copy(
            update={
                "sessions": sessions_from(self._stream.manifest_ids) or current.sessions,
                "payment_sent": self._stream.payment_sent or current.payment_sent,
                "outcome": outcome,
                "status_code": status_code if status_code is not None else self._stream.status_code,
                "ended_at": self._clock.now(),
            }
        )
        await self._store.finish_attempt(self._attempts[-1])


def sessions_from(manifest_ids: tuple[str, ...]) -> tuple[SignedSession, ...]:
    return tuple(SignedSession(manifest_id=manifest_id) for manifest_id in manifest_ids if manifest_id)


def failure_kind(outcome: AttemptOutcome, status_code: int | None) -> FailureKind:
    """Attempt outcome → failure kind, for a call that failed or a stream that ended early."""
    if outcome == "http":
        status = status_code or 0
        return "runner_rejected" if 400 <= status < 500 else "runner_error"
    if outcome == "payment_refused":
        return "payment"
    if outcome in ("in_flight", "succeeded"):
        return "runner_error"
    return cast(FailureKind, outcome)
