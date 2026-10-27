"""``FakeJobStream``: a scripted ``JobStreamApi`` that ends its job as the engine's ``JobStream`` does."""

import asyncio
import hashlib
from collections.abc import AsyncGenerator
from types import TracebackType
from typing import Protocol

from livepeer_builder.contracts import AttemptOutcome, Failure, Job, ResultRef
from livepeer_builder.errors import JobFailed
from livepeer_builder.testing.scripts import Fails, Reply


class StreamEndings(Protocol):
    """Where a stream records how it ended (``FakeJobs``)."""

    def ended(
        self,
        job: Job,
        outcome: AttemptOutcome,
        status_code: int | None,
        *,
        result: ResultRef | None = None,
        failure: Failure | None = None,
    ) -> Job: ...

    def broke(self, job: Job, fails: Fails, stream_status: int) -> Job: ...


class FakeJobStream:
    """A ``JobStreamApi`` over scripted chunks that ends the job the way the engine's ``JobStream`` does.

    The job ends once. Reading to the end, or ``complete()``, records it ``succeeded`` with a
    ``ResultRef`` of the bytes read. ``aclose()``, or leaving ``async with`` early (``break``,
    ``return``, an error), records the attempt ``abandoned`` and the paid job ``uncertain``. A
    cancellation leaving the block, and ``disconnected()``, give the runner its settle window: the
    scripted chunks are all there, so the stream succeeds unless a ``Fails`` comes first. A ``Fails``
    among the chunks raises ``JobFailed`` there. ``closed`` and ``disconnected_called`` record the
    caller's calls.
    """

    def __init__(self, job: Job, reply: Reply, jobs: StreamEndings) -> None:
        self._job, self._reply, self._jobs = job, reply, jobs
        self._next = 0  # the next scripted chunk
        self._read = hashlib.sha256()
        self._size = 0
        self._ended = False
        self.closed = False
        self.disconnected_called = False

    @property
    def job(self) -> Job:
        return self._job

    @property
    def status_code(self) -> int:
        return self._reply.status_code

    @property
    def headers(self) -> tuple[tuple[str, str], ...]:
        return self._reply.headers

    @property
    def content_type(self) -> str:
        return self._reply.content_type

    @property
    def finished(self) -> bool:
        return self._ended

    def __aiter__(self) -> AsyncGenerator[bytes, None]:
        return self._iterate()

    async def _iterate(self) -> AsyncGenerator[bytes, None]:
        try:
            while not self._ended:
                if self._next == len(self._reply.chunks):
                    self._succeed()
                    return
                chunk = self._reply.chunks[self._next]
                self._next += 1
                if isinstance(chunk, Fails):
                    self._fail(chunk)
                    raise JobFailed(self._job)
                self._count(chunk)
                yield chunk
            if self._job.state != "succeeded" and not self.closed:
                raise JobFailed(self._job)  # iterating a stream that already failed
        except GeneratorExit:  # the iterator was closed or dropped: the caller stopped
            self._abandon()
            raise

    async def __aenter__(self) -> "FakeJobStream":
        return self

    async def __aexit__(
        self, kind: type[BaseException] | None, error: BaseException | None, trace: TracebackType | None
    ) -> None:
        if kind is not None and issubclass(kind, asyncio.CancelledError):
            self._settle()  # a cancel is not the caller choosing to stop: the runner may finish (D38)
        else:
            await self.aclose()  # a normal exit, break, return or another error stops at once (D40)

    async def complete(self) -> None:
        self._succeed()

    async def aclose(self) -> None:
        self.closed = True
        self._abandon()

    async def disconnected(self) -> None:
        self.disconnected_called = True
        self._settle()

    def _settle(self) -> None:
        """The runner gets its settle window. The scripted chunks are all there, so it finishes inside
        it: the stream succeeds with every chunk counted, unless a ``Fails`` comes first."""
        while not self._ended and self._next < len(self._reply.chunks):
            chunk = self._reply.chunks[self._next]
            self._next += 1
            if isinstance(chunk, Fails):
                self._fail(chunk)
                return
            self._count(chunk)
        self._succeed()

    def _count(self, chunk: bytes) -> None:
        self._read.update(chunk)
        self._size += len(chunk)

    def _succeed(self) -> None:
        if not self._ended:
            self._ended = True
            result = ResultRef(
                storage="inline",
                content_type=self._reply.content_type,
                size_bytes=self._size,
                sha256=self._read.hexdigest(),
            )
            self._job = self._jobs.ended(self._job, "succeeded", self._reply.status_code, result=result)

    def _abandon(self) -> None:
        if not self._ended:
            self._ended = True
            failure = Failure(
                kind="abandoned", message="the caller stopped reading the stream", payment_sent=True
            )
            self._job = self._jobs.ended(self._job, "abandoned", self._reply.status_code, failure=failure)

    def _fail(self, fails: Fails) -> None:
        if not self._ended:
            self._ended = True
            self._job = self._jobs.broke(self._job, fails, self._reply.status_code)
