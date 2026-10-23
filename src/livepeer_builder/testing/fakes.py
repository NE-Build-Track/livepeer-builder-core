"""In-memory fakes for the network and provider protocols."""

import asyncio
import secrets
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from pydantic import SecretStr

from livepeer_builder.contracts import (
    Allowance,
    GrantRecord,
    JobRequest,
    Money,
    NetworkRate,
    NewAllocationKey,
    Runner,
    RunnerReply,
    SecretRef,
    UsagePage,
    UsageRow,
)
from livepeer_builder.errors import CursorMismatch, NotFound, RunnerCallError
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
        runner_id=url.rsplit("/", 1)[-1],
        mode="single-shot",
        orchestrator_url=orchestrator_url,
        capacity=capacity_available,
        capacity_available=capacity_available,
        rate=NetworkRate(amount=Decimal(price), currency="usd", unit="request") if price else None,
        metadata=metadata,
    )


Step = RunnerReply | RunnerCallError | Callable[[Runner, JobRequest, str | None], RunnerReply]


async def report_signed(on_signed: OnSigned | None, manifest_ids: Sequence[str], paid: bool) -> None:
    """What the SDK's ``on_payment_session`` does for a paid call: report each signed session first."""
    if on_signed is None or not paid:
        return
    for manifest_id in manifest_ids or ("",):
        await on_signed(manifest_id)


@dataclass
class ScriptedTransport:
    """Returns or raises the scripted steps in order and records every call."""

    steps: list[Step] = field(default_factory=list)
    calls: list[tuple[str, str | None]] = field(default_factory=list)

    async def call(
        self, runner: Runner, request: JobRequest, credential: str | None, on_signed: OnSigned | None = None
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
    content_type: str = "text/event-stream"
    manifest_ids: tuple[str, ...] = ("manifest-1",)
    payment_sent: bool = True
    open_delay_s: float = 0.0
    honors_deadline: bool = True


class ScriptedStream:
    def __init__(self, script: StreamScript) -> None:
        self._script = script
        self.closed = False
        self.delivered = 0

    @property
    def status_code(self) -> int:
        return self._script.status_code

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
        self, runner: Runner, request: JobRequest, credential: str | None, on_signed: OnSigned | None = None
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
        ref = f"mem:{name}:{secrets.token_hex(4)}"
        self.values[ref] = secret
        return SecretRef(ref=ref)

    async def get(self, ref: SecretRef) -> str:
        if ref.ref not in self.values:
            raise NotFound("unknown secret reference")
        return self.values[ref.ref]

    async def delete(self, ref: SecretRef) -> None:
        self.values.pop(ref.ref, None)


@dataclass
class _Allocation:
    status: str
    currency: str
    granted: Decimal
    spent: Decimal = Decimal(0)


class FakeAllocationManager:
    """Mimics Batteries management semantics, including idempotent replays."""

    def __init__(self) -> None:
        self.allocations: dict[str, _Allocation] = {}
        self.keys: dict[str, tuple[str, str, bool]] = {}  # api_key_id -> (allocation, secret, revoked)
        self.replays: dict[str, object] = {}
        self.calls: list[str] = []
        self.funded_grants: list[tuple[str, Money, str]] = []
        self.grants: list[GrantRecord] = []
        self.last_grant_id = ""

    async def create_key_for_grant(
        self, grant_id: str, name: str, amount: Money, idempotency_key: str
    ) -> NewAllocationKey:
        self.calls.append("create_key_for_grant")
        self.last_grant_id = grant_id
        if idempotency_key in self.replays:
            return self.replays[idempotency_key]  # type: ignore[return-value]
        allocation = f"alloc_{len(self.allocations) + 1}"
        status = "exhausted" if amount.amount == 0 else "active"
        self.allocations[allocation] = _Allocation(status, amount.currency, amount.amount)
        key = self._mint(allocation)
        self.replays[idempotency_key] = key
        return key

    async def create_key(self, allocation_id: str, name: str, idempotency_key: str) -> NewAllocationKey:
        self.calls.append("create_key")
        if idempotency_key in self.replays:
            return self.replays[idempotency_key]  # type: ignore[return-value]
        key = self._mint(allocation_id)
        self.replays[idempotency_key] = key
        return key

    async def fund_allocation(self, allocation_id: str, amount: Money, idempotency_key: str) -> None:
        self.calls.append("fund_allocation")
        if idempotency_key in self.replays:
            return
        self.replays[idempotency_key] = True
        allocation = self.allocations[allocation_id]
        allocation.granted += amount.amount
        if allocation.status == "exhausted":
            allocation.status = "active"

    async def fund_grant(self, grant_id: str, amount: Money, idempotency_key: str) -> None:
        self.calls.append("fund_grant")
        if idempotency_key in self.replays:
            return
        self.replays[idempotency_key] = True
        self.funded_grants.append((grant_id, amount, idempotency_key))

    async def set_allocation_status(self, allocation_id: str, status: str) -> None:
        self.allocations[allocation_id].status = status

    async def revoke_allocation(self, allocation_id: str) -> None:
        self.allocations[allocation_id].status = "revoked"

    async def revoke_key(self, api_key_id: str) -> None:
        allocation, secret, _ = self.keys[api_key_id]
        self.keys[api_key_id] = (allocation, secret, True)

    async def list_grants(self) -> list[GrantRecord]:
        self.calls.append("list_grants")
        return list(self.grants)

    async def create_grant(self, name: str, amount: Money) -> GrantRecord:
        self.calls.append("create_grant")
        grant = GrantRecord(
            id=f"grant_{len(self.grants) + 1}",
            name=name,
            status="active",
            currency=amount.currency,
            total=format(amount.amount, "f"),
        )
        self.grants.append(grant)
        return grant

    async def read_allocation(self, allocation_id: str) -> Allowance:
        allocation = self.allocations[allocation_id]
        currency = "USD" if allocation.currency == "USD" else "ETH"
        return Allowance(
            allocation_id=allocation_id,
            status=allocation.status,
            granted=Money(amount=allocation.granted, currency=currency),
            spent=Money(amount=allocation.spent, currency=currency),
            available=Money(amount=allocation.granted - allocation.spent, currency=currency),
        )

    def _mint(self, allocation_id: str) -> NewAllocationKey:
        key_id = f"key_{len(self.keys) + 1}"
        secret = "lpg_" + secrets.token_hex(8)
        self.keys[key_id] = (allocation_id, secret, False)
        return NewAllocationKey(allocation_id=allocation_id, api_key_id=key_id, secret=SecretStr(secret))


class FakeUsageSource:
    """Pages a fixed row list like Batteries: opaque cursors, empty cursor on the last page."""

    def __init__(self, rows: Sequence[UsageRow] = (), page_size: int = 2) -> None:
        self.rows = list(rows)
        self.page_size = page_size

    async def page(self, cursor: str | None, limit: int = 1000) -> UsagePage:
        start = 0
        if cursor:
            if not cursor.startswith("c"):
                raise CursorMismatch("invalid cursor")
            start = int(cursor[1:])
        end = start + min(limit, self.page_size)
        items = tuple(self.rows[start:end])
        return UsagePage(items=items, next_cursor=f"c{end}" if end < len(self.rows) else "")
