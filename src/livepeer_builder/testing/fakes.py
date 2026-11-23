"""In-memory fakes for the network and provider protocols."""

import asyncio
import secrets
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from livepeer_builder.contracts import (
    CallTimings,
    JobRequest,
    PriceInfo,
    Runner,
    RunnerReply,
    SecretRef,
)
from livepeer_builder.errors import NotFound, RunnerCallError
from livepeer_builder.protocols import OnSigned


class FixedClock:
    """A controllable clock. ``advance`` moves it forward."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 10, 12, 12, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)


def runner(
    app: str = "example/echo",
    url: str = "https://runner-1.example",
    *,
    capacity_available: int | None = 1,
    price: str | None = "0.10",
    orchestrator_url: str | None = "https://orch.example",
    metadata: str | None = None,
) -> Runner:
    return Runner(
        url=url,
        app=app,
        mode="single-shot",
        orchestrator_url=orchestrator_url,
        capacity=capacity_available,
        capacity_available=capacity_available,
        price_info=PriceInfo(price=Decimal(price), currency="wei", unit="fixed") if price else None,
        observed_at=datetime(2026, 10, 22, 17, 5, 0, 123000, tzinfo=UTC),
        metadata=metadata,
    )


Step = RunnerReply | RunnerCallError | Callable[[Runner, JobRequest, str | None], RunnerReply]


async def report_signed(on_signed: OnSigned | None, manifest_ids: Sequence[str], paid: bool) -> None:
    """What the Live Runner transport does for a paid call: report each signed session first (D42)."""
    if on_signed is None or not paid:
        return
    for manifest_id in manifest_ids or ("",):
        await on_signed(manifest_id)


@dataclass
class ScriptedTransport:
    """Returns or raises the scripted steps in order and records every call."""

    steps: list[Step] = field(default_factory=list)
    calls: list[tuple[str, str | None]] = field(default_factory=list)
    pays: bool = True  # whether a credential pays the runner, as a transport with a signer does

    async def call(
        self,
        runner: Runner,
        request: JobRequest,
        credential: str | None,
        on_signed: OnSigned | None = None,
    ) -> RunnerReply:
        self.calls.append((runner.url, credential))
        if not self.steps:
            raise AssertionError("ScriptedTransport ran out of steps")
        step = self.steps.pop(0)
        if isinstance(step, RunnerCallError):
            await report_signed(on_signed, step.manifest_ids, step.payment_sent)
            raise step
        reply = step if isinstance(step, RunnerReply) else step(runner, request, credential)
        await report_signed(on_signed, reply.manifest_ids, reply.payment_sent)
        return reply

    @staticmethod
    def ok(body: bytes = b'{"ok": true}', manifest_ids: Sequence[str] = ("manifest-1",)) -> RunnerReply:
        return RunnerReply(
            status_code=200,
            content=body,
            content_type="application/json",
            data=None,
            manifest_ids=tuple(manifest_ids),
            payment_sent=bool(manifest_ids),
        )


StreamEvent = bytes | float | RunnerCallError


@dataclass
class StreamScript:
    """What ScriptedStreamTransport hands back for one ``open``.

    ``events`` play in order: ``bytes`` is a chunk, a number sleeps that many seconds before the next
    event (for idle and total timeouts), and a RunnerCallError is raised mid-stream. ``open_delay_s``
    delays the answer to ``open``; a transport that ``honors_deadline`` gives up after
    ``first_byte_timeout_s`` the way SdkRunnerTransport does, one that does not just keeps waiting.
    """

    events: list[StreamEvent] = field(default_factory=list)
    status_code: int = 200
    headers: tuple[tuple[str, str], ...] = ()
    content_type: str = "text/event-stream"
    manifest_ids: tuple[str, ...] = ("manifest-1",)
    payment_sent: bool = True
    timings: CallTimings | None = None
    open_delay_s: float = 0.0
    honors_deadline: bool = True


class ScriptedStream:
    def __init__(self, script: StreamScript) -> None:
        self._script = script
        self.closed = False
        self.delivered = 0

    @property
    def timings(self) -> CallTimings | None:
        return self._script.timings

    @property
    def status_code(self) -> int:
        return self._script.status_code

    @property
    def headers(self) -> tuple[tuple[str, str], ...]:
        return self._script.headers

    @property
    def content_type(self) -> str:
        return self._script.content_type

    @property
    def manifest_ids(self) -> tuple[str, ...]:
        return self._script.manifest_ids

    @property
    def payment_sent(self) -> bool:
        return self._script.payment_sent

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[bytes]:
        for event in self._script.events:
            if isinstance(event, RunnerCallError):
                raise event
            if isinstance(event, int | float):
                await asyncio.sleep(event)
                continue
            self.delivered += 1
            yield event

    async def aclose(self) -> None:
        self.closed = True


@dataclass
class ScriptedStreamTransport(ScriptedTransport):
    """``ScriptedTransport`` that also streams: ``opens`` are consumed by ``open`` in order."""

    opens: list[StreamScript | RunnerCallError] = field(default_factory=list)
    streams: list[ScriptedStream] = field(default_factory=list)

    async def open(
        self,
        runner: Runner,
        request: JobRequest,
        credential: str | None,
        on_signed: OnSigned | None = None,
    ) -> ScriptedStream:
        self.calls.append((runner.url, credential))
        if not self.opens:
            raise AssertionError("ScriptedStreamTransport ran out of opens")
        step = self.opens.pop(0)
        if isinstance(step, RunnerCallError):
            await report_signed(on_signed, step.manifest_ids, step.payment_sent)
            raise step
        await report_signed(on_signed, step.manifest_ids, step.payment_sent)  # signed before the request
        if step.honors_deadline and step.open_delay_s > request.first_byte_timeout_s:
            await asyncio.sleep(request.first_byte_timeout_s)
            raise RunnerCallError(
                "timeout", "no response", payment_sent=step.payment_sent, manifest_ids=step.manifest_ids
            )
        await asyncio.sleep(step.open_delay_s)
        stream = ScriptedStream(step)
        self.streams.append(stream)
        return stream


class MemorySecretStore:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def put(self, name: str, secret: str) -> SecretRef:
        ref = f"mem:{name[:160]}:{secrets.token_hex(4)}"  # kept short; the actor row takes any length (#115)
        self.values[ref] = secret
        return SecretRef(ref=ref)

    async def get(self, ref: SecretRef) -> str:
        if ref.ref not in self.values:
            raise NotFound("unknown secret reference")
        return self.values[ref.ref]

    async def delete(self, ref: SecretRef) -> None:
        self.values.pop(ref.ref, None)
