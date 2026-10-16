"""In-memory fakes for the network and provider protocols."""

import secrets
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from pydantic import SecretStr

from livepeer_builder.contracts import (
    Allowance,
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
) -> Runner:
    return Runner(
        url=url,
        app=app,
        runner_id=url.rsplit("/", 1)[-1],
        mode="single-shot",
        orchestrator_url="https://orch.example",
        capacity=capacity_available,
        capacity_available=capacity_available,
        rate=NetworkRate(amount=Decimal(price), currency="usd", unit="request") if price else None,
    )


Step = RunnerReply | RunnerCallError | Callable[[Runner, JobRequest, str | None], RunnerReply]


@dataclass
class ScriptedTransport:
    """Returns or raises the scripted steps in order and records every call."""

    steps: list[Step] = field(default_factory=list)
    calls: list[tuple[str, str | None]] = field(default_factory=list)

    async def call(self, runner: Runner, request: JobRequest, credential: str | None) -> RunnerReply:
        self.calls.append((runner.url, credential))
        if not self.steps:
            raise AssertionError("ScriptedTransport ran out of steps")
        step = self.steps.pop(0)
        if isinstance(step, RunnerCallError):
            raise step
        if isinstance(step, RunnerReply):
            return step
        return step(runner, request, credential)

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

    async def create_key_for_grant(
        self, grant_id: str, name: str, amount: Money, idempotency_key: str
    ) -> NewAllocationKey:
        self.calls.append("create_key_for_grant")
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

    async def set_allocation_status(self, allocation_id: str, status: str) -> None:
        self.allocations[allocation_id].status = status

    async def revoke_allocation(self, allocation_id: str) -> None:
        self.allocations[allocation_id].status = "revoked"

    async def revoke_key(self, api_key_id: str) -> None:
        allocation, secret, _ = self.keys[api_key_id]
        self.keys[api_key_id] = (allocation, secret, True)

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
