"""In-memory fakes for the network and provider protocols."""

import asyncio
import secrets
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from pydantic import SecretStr

from livepeer_builder.contracts import (
    Allowance,
    CallTimings,
    Currency,
    GrantPage,
    GrantRecord,
    JobRequest,
    Money,
    NewAllocationKey,
    PriceInfo,
    Runner,
    RunnerReply,
    SecretRef,
    TenantGrant,
)
from livepeer_builder.errors import NotFound, ProviderError, RunnerCallError
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


@dataclass
class _Allocation:
    status: str
    currency: str
    granted: Decimal
    spent: Decimal = Decimal(0)
    grant_id: str = ""


@dataclass
class _Grant:
    record: GrantRecord
    total: Decimal
    unallocated: Decimal


class FakeAllocationManager:
    """Mimics Batteries management semantics: idempotent replays, and a grant's unallocated balance.
    A success is saved under the call's idempotency key and replayed to the same request under that key;
    any other request under it is refused as Batteries refuses it (409), and nothing is applied.

    A tenant's id is its grant's id (D83), so an allotment names a grant the fake must hold. ``add_grant``
    gives it one under an id the test chooses (a tenant such as ``"default"``); ``create_grant``, which
    onboarding a tenant calls, makes one under the next ``grant_<n>`` that no grant holds. An unknown
    grant is refused as Batteries refuses it (404), and so is an allocation or a top-up larger than the
    grant's unallocated balance (409, "insufficient unallocated grant balance"). A revoke returns what the
    allocation had not spent. A paused grant still takes allotments and top-ups, as at Batteries: pausing
    stops its payments.

    ``calls`` lists every call in order. ``fail_once(call, error)`` makes the next call of that method
    raise ``error`` before it does anything: a provider that timed out or refused, for the
    crash-between-steps tests (D57).
    """

    def __init__(self) -> None:
        self.allocations: dict[str, _Allocation] = {}
        self.keys: dict[str, tuple[str, str, bool]] = {}  # api_key_id -> (allocation, secret, revoked)
        self.replays: dict[str, object] = {}
        self._sent: dict[str, tuple[object, ...]] = {}  # the request each saved success answered
        self.calls: list[str] = []
        self.topped_up_grants: list[tuple[str, Money, str]] = []
        self.grants: list[GrantRecord] = []
        self.grant_page_size = 1000  # Batteries' largest page
        self.grant_page_delay_s = 0.0  # each page's latency, for a slow catalog (D57)
        self.last_grant_id = ""
        self._held: dict[str, _Grant] = {}
        self._created = 0
        self._failures: dict[str, Exception] = {}

    def fail_once(self, call: str, error: Exception) -> None:
        self._failures[call] = error

    def add_grant(
        self, grant_id: str, total: Decimal | int | str = 1000, currency: str = "USD", name: str = ""
    ) -> GrantRecord:
        """A grant the provider already holds, under ``grant_id``: a tenant onboarded before the test."""
        amount = Decimal(total)
        grant = GrantRecord(
            id=grant_id, name=name or grant_id, status="active", currency=currency, total=format(amount, "f")
        )
        self.grants.append(grant)
        self._held[grant_id] = _Grant(grant, amount, amount)
        return grant

    @property
    def held(self) -> frozenset[str]:
        """The ids of the grants the provider holds."""
        return frozenset(self._held)

    def unallocated(self, grant_id: str) -> Decimal:
        return self._held[grant_id].unallocated

    def _called(self, call: str) -> None:
        self.calls.append(call)
        if call in self._failures:
            raise self._failures.pop(call)

    def _saved(self, idempotency_key: str, *request: object) -> bool:
        """Whether a success is saved under the key, for this request as it is sent: an amount is compared
        as written (``5`` is not ``5.0``). A call that failed saved nothing."""
        sent = tuple(f"{v.amount:f} {v.currency}" if isinstance(v, Money) else v for v in request)
        if idempotency_key not in self.replays:
            self._sent[idempotency_key] = sent
            return False
        if self._sent[idempotency_key] == sent:
            return True
        raise ProviderError(409, "idempotency key was already used with different parameters or operation")

    def _grant(self, grant_id: str) -> _Grant:
        if grant_id not in self._held:
            raise ProviderError(404, "resource not found")
        return self._held[grant_id]

    def _take(self, grant_id: str, amount: Decimal) -> None:
        grant = self._grant(grant_id)
        if grant.unallocated < amount:
            raise ProviderError(409, "insufficient unallocated grant balance")
        grant.unallocated -= amount

    async def create_key_for_grant(
        self, grant_id: str, name: str, amount: Money, idempotency_key: str
    ) -> NewAllocationKey:
        self._called("create_key_for_grant")
        self.last_grant_id = grant_id
        if self._saved(idempotency_key, "create_key_for_grant", grant_id, name, amount):
            return self.replays[idempotency_key]  # type: ignore[return-value]
        self._take(grant_id, amount.amount)
        allocation = f"alloc_{len(self.allocations) + 1}"
        status = "exhausted" if amount.amount == 0 else "active"
        self.allocations[allocation] = _Allocation(status, amount.currency, amount.amount, grant_id=grant_id)
        key = self._mint(allocation)
        self.replays[idempotency_key] = key
        return key

    async def create_key(self, allocation_id: str, name: str, idempotency_key: str) -> NewAllocationKey:
        self._called("create_key")
        if self._saved(idempotency_key, "create_key", allocation_id, name):
            return self.replays[idempotency_key]  # type: ignore[return-value]
        key = self._mint(allocation_id)
        self.replays[idempotency_key] = key
        return key

    async def top_up_allocation(  # NOSONAR - async by protocol (AllocationManager); the fake awaits nothing
        self, allocation_id: str, amount: Money, idempotency_key: str
    ) -> None:
        self._called("top_up_allocation")
        if self._saved(idempotency_key, "top_up_allocation", allocation_id, amount):
            return
        allocation = self.allocations[allocation_id]
        self._take(allocation.grant_id, amount.amount)
        self.replays[idempotency_key] = True
        allocation.granted += amount.amount
        if allocation.status == "exhausted":
            allocation.status = "active"

    async def top_up_grant(  # NOSONAR - async by protocol (AllocationManager); the fake awaits nothing
        self, grant_id: str, amount: Money, idempotency_key: str
    ) -> None:
        self._called("top_up_grant")
        if self._saved(idempotency_key, "top_up_grant", grant_id, amount):
            return
        grant = self._grant(grant_id)
        self.replays[idempotency_key] = True
        grant.total += amount.amount
        grant.unallocated += amount.amount
        self._restate(grant)
        self.topped_up_grants.append((grant_id, amount, idempotency_key))

    async def set_allocation_status(self, allocation_id: str, status: str) -> None:
        self._called("set_allocation_status")
        self.allocations[allocation_id].status = status

    async def revoke_allocation(self, allocation_id: str) -> None:
        self._called("revoke_allocation")
        allocation = self.allocations[allocation_id]
        if allocation.status != "revoked":  # what was not spent returns to the grant, once
            unspent = max(allocation.granted - allocation.spent, Decimal(0))
            self._grant(allocation.grant_id).unallocated += unspent
            allocation.granted -= unspent
        allocation.status = "revoked"

    async def revoke_key(self, api_key_id: str) -> None:
        self._called("revoke_key")
        allocation, secret, _ = self.keys[api_key_id]
        self.keys[api_key_id] = (allocation, secret, True)

    async def list_grants(self) -> list[GrantRecord]:
        self._called("list_grants")
        grants: list[GrantRecord] = []
        cursor: str | None = None
        while True:
            page = await self._page(cursor)
            grants += page.grants
            if page.next_cursor is None:
                return grants
            cursor = page.next_cursor

    async def grant_page(self, cursor: str | None) -> GrantPage:
        self._called("grant_page")
        return await self._page(cursor)

    async def _page(self, cursor: str | None) -> GrantPage:
        await asyncio.sleep(self.grant_page_delay_s)
        start = int(cursor or 0)
        end = start + self.grant_page_size
        more = end < len(self.grants)
        return GrantPage(grants=tuple(self.grants[start:end]), next_cursor=str(end) if more else None)

    async def create_grant(
        self, name: str, amount: Money, metadata: Mapping[str, str] | None = None
    ) -> GrantRecord:
        self._called("create_grant")
        self._created += 1
        while f"grant_{self._created}" in self._held:  # one add_grant seeded: never replace it (#165 review)
            self._created += 1
        grant = GrantRecord(
            id=f"grant_{self._created}",
            name=name,
            status="active",
            currency=amount.currency,
            total=format(amount.amount, "f"),
            metadata=dict(metadata or {}),
        )
        self.grants.append(grant)
        self._held[grant.id] = _Grant(grant, amount.amount, amount.amount)
        return grant

    async def read_grant(self, grant_id: str) -> TenantGrant:  # NOSONAR - async by protocol
        self._called("read_grant")
        return _tenant_grant(self._grant(grant_id), self._spent(grant_id))

    async def grant_balances(self) -> list[TenantGrant]:  # NOSONAR - async by protocol
        self._called("grant_balances")
        return [_tenant_grant(self._held[grant.id], self._spent(grant.id)) for grant in self.grants]

    def _spent(self, grant_id: str) -> Decimal:
        """What the grant's allocations have spent, the revoked ones included, as Batteries reports it."""
        spent = (held.spent for held in self.allocations.values() if held.grant_id == grant_id)
        return sum(spent, Decimal(0))

    async def set_grant_status(self, grant_id: str, status: str) -> None:  # NOSONAR - async by protocol
        self._called("set_grant_status")
        grant = self._grant(grant_id)
        grant.record = grant.record.model_copy(update={"status": status})
        self._restate(grant)

    def _restate(self, grant: _Grant) -> None:
        """Keep the listed record as the grant is now: its total and its status."""
        grant.record = grant.record.model_copy(update={"total": format(grant.total, "f")})
        self.grants = [grant.record if listed.id == grant.record.id else listed for listed in self.grants]

    async def read_allocation(self, allocation_id: str) -> Allowance:
        self._called("read_allocation")
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


def _tenant_grant(grant: _Grant, spent: Decimal) -> TenantGrant:
    currency: Currency = "USD" if grant.record.currency == "USD" else "ETH"
    return TenantGrant(
        tenant_id=grant.record.id,
        name=grant.record.name,
        status=grant.record.status,
        total=Money(amount=grant.total, currency=currency),
        unallocated=Money(amount=grant.unallocated, currency=currency),
        spent=Money(amount=spent, currency=currency),
    )
