"""A streamed job's life after the runner answered (docs/contracts/engine.md#streaming).

``JobStream`` owns the open runner stream, the admission slot and the attempt row. A pump task reads
the runner into a one-chunk queue, so the caller's cancellation never interrupts the runner read, and
the stream is finished exactly once by a finishing task the caller cannot cancel (D34):

- ``succeeded`` when the runner ends the stream, or when the caller says it has what it needs
  (``complete()``, D38);
- otherwise ``uncertain`` (paid) or ``failed`` (unpaid), with the outcome that ended it: a runner
  error, a ``timeout`` (D35), or ``abandoned`` when the caller stopped reading (D30). A caller that is
  cancelled, or whose client disconnects (``disconnected()``), first gets a short settle window in
  which the runner may still end the stream by itself (D38). Closing or dropping the iterator is the
  caller stopping, so it abandons at once, like ``aclose()`` (D40).

The admission slot is always released, even when the store cannot be written; the job row is then
left ``running`` and the next start resolves it (``recover_interrupted``).
"""

import asyncio
import contextlib
import hashlib
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from types import TracebackType
from typing import Literal, cast

from livepeer_builder.contracts import (
    ActorContext,
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
from livepeer_builder.utils import BODY_LIMIT, run_shielded

_LOG = logging.getLogger(__name__)
_END = b""  # the pump's end-of-stream marker; the pump never queues an empty chunk
FinishJob = Callable[[Job, JobState, Failure | None, ResultRef | None, Sequence[Attempt]], Awaitable[Job]]
_Deadline = Literal["first_byte", "idle", "total"]

_TIMEOUT_MESSAGES: dict[_Deadline, str] = {
    "first_byte": "the runner sent nothing within first_byte_timeout_s",
    "idle": "the runner sent nothing for longer than idle_timeout_s",
    "total": "the job ran past timeout_s",
}


@dataclass(frozen=True)
class StreamLimits:
    """Engine-wide stream settings (``LPB_STREAM_*``, docs/RELIABILITY.md#configuration)."""

    finalize_timeout_s: float = 10.0  # the most the finishing writes may take (D34)
    drain_timeout_s: float = 30.0  # how long engine.stop() waits for open streams (D36)
    settle_s: float = 1.0  # after a cancel or disconnect, how long the runner may take to end (D38)
    open_grace_s: float = 5.0  # how far past its deadline a transport's open runs before the engine stops it


@dataclass(frozen=True)
class _Ending:
    outcome: AttemptOutcome
    message: str = ""
    status_code: int | None = None
    body: str | None = None
    settle: bool = False  # wait settle_s for the runner to end the stream before deciding


_SUCCEEDED = _Ending("succeeded")
_STOPPED = "the caller stopped reading the stream"
_DISCONNECTED = "the client disconnected"


class _Timeout(Exception):
    def __init__(self, clock: _Deadline) -> None:
        super().__init__(_TIMEOUT_MESSAGES[clock])


class JobStream:
    """A running job's response, delivered as it arrives (docs/contracts/engine.md#streaming).

    Use it as an async context manager and iterate it::

        stream = await engine.jobs.streaming.stream(actor, request)
        async with stream:
            async for chunk in stream:
                ...

    Reading to the end makes the job ``succeeded``. ``complete()`` does the same when the caller has
    everything it needs before the runner closes (an end-of-stream marker in its format). ``aclose()``
    stops now: the attempt is ``abandoned`` and spending stops; so does leaving the loop without
    either. ``disconnected()`` is for an HTTP layer whose client went away. A runner failure or a
    timeout raises ``JobFailed`` from the iterator.
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
        limits: StreamLimits,
        deadline: float,
        first_byte_left: float,
        on_done: Callable[["JobStream"], None] = lambda _: None,
    ) -> None:
        self._store = store
        self._clock = clock
        self._finish = finish
        self._job = job.model_copy(update={"attempts": tuple(attempts)})
        self._attempts = attempts
        self._stream = stream
        self._idle = request.idle_timeout_s
        self._slot = slot
        self._limits = limits
        self._deadline = deadline  # loop time when timeout_s runs out, counted from the job's start
        self._first_byte_left = first_byte_left  # counted only while the caller waits for data
        self._on_done = on_done
        self._hash = hashlib.sha256()
        self._size = 0
        self._got_first = False
        self._waiting = False
        self._caller_stopped = False
        self._final: asyncio.Task[None] | None = None
        self._ended = asyncio.Event()
        self._runner_closed = False
        self._runner_finished = False  # the runner ended the stream; deadlines no longer apply
        self._loop = asyncio.get_running_loop()
        self._queue: asyncio.Queue[bytes | Exception] = asyncio.Queue(maxsize=1)
        self._pump = self._loop.create_task(self._pump_chunks(), name=f"lpb-stream-{job.id}")
        self._timer = self._loop.call_at(deadline, self._deadline_passed)

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
        """True once the stream has ended, however it ended (the job record may still be writing)."""
        return self._final is not None

    def __aiter__(self) -> AsyncGenerator[bytes, None]:
        return self._iterate()

    async def __aenter__(self) -> "JobStream":
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        if exc_type is not None and issubclass(exc_type, asyncio.CancelledError):
            await run_shielded(self._wait(self._begin(_Ending("abandoned", _STOPPED, settle=True))))
        else:
            await self.aclose()

    async def complete(self) -> None:
        """The caller has everything it needs: stop the runner and record the job ``succeeded``.

        The result describes the bytes the caller has read, not any the engine read ahead. Use this
        when the stream's own format says it is done (for example ``data: [DONE]``) and you stop
        reading before the runner closes the connection. Does nothing once the stream has ended.
        """
        await run_shielded(self._wait(self._begin(_SUCCEEDED)))

    async def aclose(self) -> None:
        """Stop now: close the runner stream (spending stops) and record the attempt ``abandoned``."""
        if self._final is None:
            self._caller_stopped = True
        await run_shielded(self._wait(self._begin(_Ending("abandoned", _STOPPED))))

    async def disconnected(self) -> None:
        """The client this stream was serving went away. The runner gets ``settle_s`` to end the
        stream by itself (then ``succeeded``), else the attempt is ``abandoned`` (D38, D40)."""
        await run_shielded(self._wait(self._begin(_Ending("abandoned", _DISCONNECTED, settle=True))))

    async def wait_ended(self) -> None:
        """Wait until the stream has ended and its job record is written (or given up on)."""
        await self._ended.wait()

    async def abandon(self, message: str) -> None:
        """End the stream for a reason that is not the caller's (engine shutdown)."""
        await run_shielded(self._wait(self._begin(_Ending("abandoned", message))))

    # -- reading ----------------------------------------------------------------------------------------

    async def _iterate(self) -> AsyncGenerator[bytes, None]:
        try:
            while True:
                if self._final is not None:
                    await self._ended_already()
                    return
                item = await self._next()
                if isinstance(item, Exception):
                    raise item
                if item == _END:
                    break
                self._count(item)
                yield item
        except _Timeout as timeout:
            await run_shielded(self._wait(self._begin(_Ending("timeout", str(timeout)))))
            raise JobFailed(self._job) from None
        except RunnerCallError as error:
            ending = _Ending(error.kind, str(error), status_code=error.status_code, body=error.body)
            await run_shielded(self._wait(self._begin(ending)))
            raise JobFailed(self._job) from error
        except asyncio.CancelledError:
            final = self._begin(_Ending("abandoned", _STOPPED, settle=True))  # before any await
            await run_shielded(self._wait(final))
            raise
        except GeneratorExit:  # the iterator was closed or dropped: the caller stopped (D40)
            if self._final is None:
                self._caller_stopped = True
            await run_shielded(self._wait(self._begin(_Ending("abandoned", _STOPPED))))
            raise
        except JobFailed:
            raise
        except Exception as exc:
            await run_shielded(
                self._wait(self._begin(_Ending("http", f"engine error: {type(exc).__name__}")))
            )
            raise JobFailed(self._job) from exc
        await run_shielded(self._wait(self._begin(_SUCCEEDED)))

    async def _ended_already(self) -> None:
        """Iterating a stream that already ended: nothing more, or the failure that ended it."""
        assert self._final is not None
        await run_shielded(self._wait(self._final))
        if self._job.state != "succeeded" and not self._caller_stopped:
            raise JobFailed(self._job)

    async def _next(self) -> bytes | Exception:
        """The next chunk, end marker or runner error, within whichever deadline applies now."""
        if not self._queue.empty():
            return self._took(self._queue.get_nowait())
        if self._runner_finished:  # the end marker is on its way; a deadline would only lose it
            return self._took(await self._queue.get())
        total_left = self._deadline - self._loop.time()
        clock: _Deadline = "total"
        wait = total_left
        if not self._got_first and self._first_byte_left < wait:
            clock, wait = "first_byte", self._first_byte_left
        elif self._got_first and self._idle is not None and self._idle < wait:
            clock, wait = "idle", self._idle
        if wait <= 0:
            raise _Timeout(clock)
        started = self._loop.time()
        self._waiting = True
        try:
            return self._took(await asyncio.wait_for(self._queue.get(), wait))
        except TimeoutError:
            if not self._runner_finished:
                raise _Timeout(clock) from None
            return self._took(await self._queue.get())
        finally:
            self._waiting = False
            if not self._got_first:
                self._first_byte_left -= self._loop.time() - started

    def _took(self, item: bytes | Exception) -> bytes | Exception:
        self._got_first = True
        return item

    def _count(self, chunk: bytes) -> None:
        """Add a chunk to the result. Only chunks handed over count, never the pump's read-ahead."""
        self._hash.update(chunk)
        self._size += len(chunk)

    async def _pump_chunks(self) -> None:
        try:
            async for chunk in self._stream:
                if chunk:
                    await self._queue.put(chunk)
        except Exception as error:
            await self._queue.put(error)
            return
        self._runner_finished = True
        await self._queue.put(_END)

    def _deadline_passed(self) -> None:
        """``timeout_s`` ran out. A waiting reader times out by itself; anything else ends here, unless
        the runner already finished: then the caller is only handling chunks it has, and ends it."""
        if self._final is None and not self._waiting and not self._runner_finished:
            self._begin(_Ending("timeout", _TIMEOUT_MESSAGES["total"]))

    # -- finishing --------------------------------------------------------------------------------------

    def _begin(self, ending: _Ending) -> "asyncio.Task[None]":
        """Start finishing the stream, once. Synchronous, so it happens even if the caller is cancelled."""
        if self._final is None:
            self._timer.cancel()
            self._final = self._loop.create_task(
                self._finalize(ending), name=f"lpb-stream-end-{self._job.id}"
            )
        return self._final

    @staticmethod
    async def _wait(final: "asyncio.Task[None]") -> None:
        await asyncio.wait([final])

    async def _finalize(self, ending: _Ending) -> None:
        try:
            if ending.settle:
                ending = await self._settle(ending)
            async with asyncio.timeout(self._limits.finalize_timeout_s):
                await self._record(ending)
        except Exception:
            _LOG.exception(
                "could not record the end of a streamed job; the next start resolves it",
                extra={"event": "jobs.stream_finish_failed", "job_id": str(self._job.id)},
            )
        finally:
            with contextlib.suppress(Exception):
                async with asyncio.timeout(self._limits.finalize_timeout_s):
                    await self._stop_runner()
            try:
                await self._slot.aclose()
            except Exception:
                _LOG.exception(
                    "could not release an admission slot", extra={"event": "jobs.slot_release_failed"}
                )
            self._ended.set()
            self._on_done(self)

    async def _settle(self, stopped: _Ending) -> _Ending:
        """The caller went away. If the runner ends the stream within settle_s, the job succeeded, and
        its result includes what the runner sent in that window."""
        if self._limits.settle_s <= 0:
            return stopped
        try:
            async with asyncio.timeout(self._limits.settle_s):
                while True:
                    item = await self._queue.get()
                    if isinstance(item, RunnerCallError):
                        return _Ending(item.kind, str(item), status_code=item.status_code, body=item.body)
                    if isinstance(item, Exception):
                        return _Ending("http", f"engine error: {type(item).__name__}")
                    if item == _END:
                        return _SUCCEEDED
                    self._count(item)
        except TimeoutError:
            return stopped

    async def _stop_runner(self) -> None:
        """Stop reading and close the runner stream, which also stops any ongoing funding. Idempotent."""
        self._pump.cancel()
        await asyncio.wait([self._pump])
        if not self._runner_closed:
            self._runner_closed = True
            with contextlib.suppress(Exception):
                await self._stream.aclose()

    async def _record(self, ending: _Ending) -> None:
        await self._stop_runner()
        await self._store_attempt(ending.outcome, ending.status_code)
        if ending.outcome == "succeeded":
            result = ResultRef(
                storage="inline",
                content_type=self._stream.content_type,
                size_bytes=self._size,
                sha256=self._hash.hexdigest(),
            )
            state: JobState = "succeeded"
            self._job = await self._finish(self._job, state, None, result, self._attempts)
        else:
            paid = self._attempts[-1].payment_sent
            failure = Failure(
                kind=failure_kind(ending.outcome, ending.status_code),
                message=ending.message,
                status_code=ending.status_code,
                body=ending.body[:BODY_LIMIT] if ending.body else None,
                payment_sent=paid,
            )
            state = "uncertain" if paid else "failed"
            self._job = await self._finish(self._job, state, failure, None, self._attempts)
        _LOG.info(
            "stream ended",
            extra={
                "event": "jobs.stream_ended",
                "job_id": str(self._job.id),
                "outcome": ending.outcome,
                "state": state,
                "bytes": self._size,
            },
        )

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


class Streaming:
    """``jobs.streaming``: exists only when the transport implements ``open`` (D33).

    Everything up to the first byte behaves like ``run``; then the job is bound to that runner. Use
    the returned ``JobStream`` with ``async with`` and iterate it; ``complete()`` or ``aclose()`` end it
    early.
    """

    def __init__(self, open_stream: Callable[[ActorContext, JobRequest], Awaitable[JobStream]]) -> None:
        self._open_stream = open_stream

    async def stream(self, actor: ActorContext, request: JobRequest) -> JobStream:
        return await self._open_stream(actor, request)
