"""A fake of the Batteries management API (``AllocationManager`` and ``GrantsManager``): allocations, keys,
grants and their idempotent replays. Split from ``fakes.py`` by responsibility."""

import asyncio
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

from pydantic import SecretStr

from livepeer_builder.contracts import (
    Allowance,
    Currency,
    GrantPage,
    GrantRecord,
    Money,
    NewAllocationKey,
    TenantGrant,
)
from livepeer_builder.errors import ProviderError


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

    A tenant's id is its grant's id (D83), so an allotment names a grant the fake must hold. ``add_grant``
    gives it one under an id the test chooses (a tenant such as ``"default"``); ``create_grant``, which
    onboarding a tenant calls, makes one under the next ``grant_<n>`` that no grant holds. An unknown
    grant is refused as Batteries refuses it (404), and so is an allocation or a top-up larger than the
    grant's unallocated balance (409, "insufficient unallocated grant balance"). A revoke returns what the
    allocation had not spent. A paused grant still takes allotments and top-ups, as at Batteries: pausing
    stops its payments. A call repeated under its idempotency key replays the first success only when it
    sends the same request; a changed request under a used key is refused (409), as Batteries refuses it
    (docs/references/batteries-api.md), so a resume that rebuilds its request differently fails here too.

    ``calls`` lists every call in order. ``fail_once(call, error)`` makes the next call of that method
    raise ``error`` before it does anything: a provider that timed out or refused, for the
    crash-between-steps tests (D57).
    """

    def __init__(self) -> None:
        self.allocations: dict[str, _Allocation] = {}
        self.keys: dict[str, tuple[str, str, bool]] = {}  # api_key_id -> (allocation, secret, revoked)
        self.replays: dict[str, object] = {}
        self._requests: dict[str, tuple[object, ...]] = {}  # idempotency key -> the request it was used with
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

    def _replays(self, idempotency_key: str, request: tuple[object, ...]) -> bool:
        """Whether a call under ``idempotency_key`` repeats a saved success. A different request under it
        is refused, as at Batteries; a call refused before is not saved, so its repeat runs again."""
        held = self._requests.get(idempotency_key)
        if held is None:
            return False
        if held != request:
            raise ProviderError(409, "the idempotency key was used with a different request")
        return True

    def _saved(self, idempotency_key: str, request: tuple[object, ...], answer: object) -> None:
        self._requests[idempotency_key] = request
        self.replays[idempotency_key] = answer

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
        request = ("create_key_for_grant", grant_id, name, amount)
        if self._replays(idempotency_key, request):
            return self.replays[idempotency_key]  # type: ignore[return-value]
        self._take(grant_id, amount.amount)
        allocation = f"alloc_{len(self.allocations) + 1}"
        status = "exhausted" if amount.amount == 0 else "active"
        self.allocations[allocation] = _Allocation(status, amount.currency, amount.amount, grant_id=grant_id)
        key = self._mint(allocation)
        self._saved(idempotency_key, request, key)
        return key

    async def create_key(self, allocation_id: str, name: str, idempotency_key: str) -> NewAllocationKey:
        self._called("create_key")
        request = ("create_key", allocation_id, name)
        if self._replays(idempotency_key, request):
            return self.replays[idempotency_key]  # type: ignore[return-value]
        key = self._mint(allocation_id)
        self._saved(idempotency_key, request, key)
        return key

    async def top_up_allocation(  # NOSONAR - async by protocol (AllocationManager); the fake awaits nothing
        self, allocation_id: str, amount: Money, idempotency_key: str
    ) -> None:
        self._called("top_up_allocation")
        request = ("top_up_allocation", allocation_id, amount)
        if self._replays(idempotency_key, request):
            return
        allocation = self.allocations[allocation_id]
        self._take(allocation.grant_id, amount.amount)
        self._saved(idempotency_key, request, True)
        allocation.granted += amount.amount
        if allocation.status == "exhausted":
            allocation.status = "active"

    async def top_up_grant(  # NOSONAR - async by protocol (AllocationManager); the fake awaits nothing
        self, grant_id: str, amount: Money, idempotency_key: str
    ) -> None:
        self._called("top_up_grant")
        request = ("top_up_grant", grant_id, amount)
        if self._replays(idempotency_key, request):
            return
        grant = self._grant(grant_id)
        self._saved(idempotency_key, request, True)
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
