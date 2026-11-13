"""#115 C9: reconciliation balances again after a revoke and a re-provision.

Reconciliation compared the provider's ``spent`` for the actor's current allocation with every charge of
every job the actor ever ran. Once the actor was revoked and provisioned again, the jobs the first
allocation paid were still counted against the second, so it never balanced again. It now reconciles one
allocation at a time: only the attempts that allocation paid.
"""

from decimal import Decimal
from typing import Any

import httpx
import pytest

from livepeer_builder import BuilderEngine, EngineSupply
from livepeer_builder.contracts import (
    ActorContext,
    JobRequest,
    Money,
    ProvisionRequest,
    Reconciliation,
    UsageRow,
)
from livepeer_builder.errors import NotFound
from livepeer_builder.http import create_app
from livepeer_builder.protocols import AllocationCostSnapshot
from livepeer_builder.store import SqlStore
from livepeer_builder.testing import FakeAllocationManager, FakeUsageSource, FixedClock, ScriptedTransport
from tests.conftest import EngineFactory

APP = "example/echo"


def row(row_id: str, eth: str, allocation: str, manifest: str) -> UsageRow:
    return UsageRow(
        id=row_id, event_id=f"ev-{row_id}", status="applied", manifest_id=manifest, allocation_id=allocation,
        payment_session_id=f"sess-{row_id}", request_id=None, pipeline="live", computed_fee_eth=Decimal(eth),
        computed_fee_usd=None, currency="usd", created_at=FixedClock().now(),
    )  # fmt: skip


def provision(ref: str) -> ProvisionRequest:
    return ProvisionRequest(
        operation_ref=ref,
        application_id="default",
        actor_id="alice",
        amount=Money(amount=Decimal(1), currency="ETH"),
        grant_id="grant_1",
    )


async def two_allocations(
    engine: BuilderEngine,
    admin: ActorContext,
    alice: ActorContext,
    transport: ScriptedTransport,
    usage: FakeUsageSource,
    manager: FakeAllocationManager,
) -> tuple[str, str]:
    """Alice pays one job (0.2) on her first allocation, is revoked and provisioned again, and pays one
    job (0.3) and one forged charge (0.1) on the second. The provider reports what each spent."""
    first = (await engine.payments.provision(admin, provision("provision-1"))).allocation_id
    transport.steps = [ScriptedTransport.ok(manifest_ids=("m-1",))]
    await engine.jobs.run(alice, JobRequest(app=APP))
    await engine.payments.revoke(admin, "default", "alice")
    second = (await engine.payments.provision(admin, provision("provision-2"))).allocation_id
    assert second != first
    transport.steps = [ScriptedTransport.ok(manifest_ids=("m-2",))]
    await engine.jobs.run(alice, JobRequest(app=APP))
    usage.rows = [row("1", "0.2", first, "m-1"), row("2", "0.3", second, "m-2"), row("3", "0.1", second, "x")]
    await engine.costs.sync_once()
    manager.allocations[first].spent = Decimal("0.2")  # Batteries still reports a revoked allocation's spend
    manager.allocations[second].spent = Decimal("0.4")
    return first, second


def counted(report: Reconciliation) -> tuple[Decimal, Decimal]:
    return report.by_class["delivered"].fee_eth, report.unattributed.fee_eth


async def test_reconciliation_balances_after_a_revoke_and_a_re_provision(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, transport: ScriptedTransport,
    usage: FakeUsageSource, manager: FakeAllocationManager,
) -> None:  # fmt: skip
    engine = await make_engine(default_currency="ETH")
    first, second = await two_allocations(engine, admin, alice, transport, usage, manager)

    report = await engine.costs.reconcile(admin, "default", "alice")  # the current allocation
    assert counted(report) == (Decimal("0.3"), Decimal("0.1"))  # not the 0.2 the first allocation paid
    assert report.provider_spent == Money(amount=Decimal("0.4"), currency="ETH") and report.balanced is True
    assert report.allocation_id == second and report.other_allocations == (first,)


async def test_an_earlier_allocation_reconciles_like_the_current_one(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, transport: ScriptedTransport,
    usage: FakeUsageSource, manager: FakeAllocationManager,
) -> None:  # fmt: skip
    engine = await make_engine(default_currency="ETH")
    first, second = await two_allocations(engine, admin, alice, transport, usage, manager)

    earlier = await engine.costs.reconcile(admin, "default", "alice", first)
    assert earlier.allocation_id == first and earlier.other_allocations == (second,)
    assert counted(earlier) == (Decimal("0.2"), Decimal("0"))
    assert earlier.provider_spent == Money(amount=Decimal("0.2"), currency="ETH") and earlier.balanced is True

    named = await engine.costs.reconcile(admin, "default", "alice", second)
    assert named == await engine.costs.reconcile(admin, "default", "alice")


async def test_an_allocation_that_never_paid_for_the_actor_is_not_found(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, transport: ScriptedTransport,
    usage: FakeUsageSource, manager: FakeAllocationManager,
) -> None:  # fmt: skip
    """Otherwise an auditor of one application could read what any allocation spent."""
    engine = await make_engine(default_currency="ETH")
    await two_allocations(engine, admin, alice, transport, usage, manager)
    other = await engine.payments.provision(
        admin, provision("provision-bob").model_copy(update={"actor_id": "bob"})
    )
    for unknown in (other.allocation_id, "alloc_none"):
        with pytest.raises(NotFound, match="never paid"):
            await engine.costs.reconcile(admin, "default", "alice", unknown)
    with pytest.raises(NotFound, match="no allocation pays"):
        await engine.costs.reconcile(admin, "default", "nobody")


async def test_an_actor_with_one_allocation_lists_no_other(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, transport: ScriptedTransport,
    usage: FakeUsageSource, manager: FakeAllocationManager,
) -> None:  # fmt: skip
    engine = await make_engine(default_currency="ETH")
    only = (await engine.payments.provision(admin, provision("provision-1"))).allocation_id
    assert (await engine.costs.reconcile(admin, "default", "alice")).other_allocations == ()
    transport.steps = [ScriptedTransport.ok(manifest_ids=("m-1",))]
    await engine.jobs.run(alice, JobRequest(app=APP))
    usage.rows = [row("1", "0.2", only, "m-1")]
    await engine.costs.sync_once()
    manager.allocations[only].spent = Decimal("0.2")
    report = await engine.costs.reconcile(admin, "default", "alice")
    assert report.other_allocations == () and report.balanced is True


async def test_a_shared_allocation_counts_only_what_it_paid(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, bob: ActorContext,
    transport: ScriptedTransport, usage: FakeUsageSource, manager: FakeAllocationManager,
) -> None:  # fmt: skip
    """``per_application``: the operator replaced the shared allocation. Each one reconciles on its own,
    over every actor of the application."""
    one_eth = Money(amount=Decimal(1), currency="ETH")
    old, new = [
        (await manager.create_key_for_grant("grant_1", name, one_eth, name)).allocation_id for name in "ab"
    ]
    before = await make_engine(
        allocation_mode="per_application", shared_payment_key="lpg_a", shared_allocation_id=old
    )
    transport.steps = [ScriptedTransport.ok(manifest_ids=("m-1",))]
    await before.jobs.run(alice, JobRequest(app=APP))
    engine = await make_engine(
        allocation_mode="per_application", shared_payment_key="lpg_b", shared_allocation_id=new
    )
    transport.steps = [ScriptedTransport.ok(manifest_ids=("m-2",))]
    await engine.jobs.run(bob, JobRequest(app=APP))
    usage.rows = [row("1", "0.2", old, "m-1"), row("2", "0.3", new, "m-2")]
    await engine.costs.sync_once()
    manager.allocations[old].spent, manager.allocations[new].spent = Decimal("0.2"), Decimal("0.3")

    current = await engine.costs.reconcile(admin, "default", None)
    assert (current.allocation_id, current.other_allocations) == (new, (old,))
    assert counted(current) == (Decimal("0.3"), Decimal("0")) and current.balanced is True
    earlier = await engine.costs.reconcile(admin, "default", None, old)
    assert counted(earlier) == (Decimal("0.2"), Decimal("0")) and earlier.balanced is True


async def test_the_rest_route_takes_the_allocation_as_a_typed_query_id(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, transport: ScriptedTransport,
    usage: FakeUsageSource, manager: FakeAllocationManager,
) -> None:  # fmt: skip
    engine = await make_engine(default_currency="ETH")
    first, second = await two_allocations(engine, admin, alice, transport, usage, manager)
    key = await engine.keys.bootstrap_admin()
    app = create_app(engine, manage_lifecycle=False)
    headers = {"Authorization": f"Bearer {key.token}"}
    route = "/v1/admin/reconciliation"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://engine") as client:
        current = await client.get(route, params={"actor_id": "alice"}, headers=headers)
        earlier = await client.get(
            route, params={"actor_id": "alice", "allocation_id": first}, headers=headers
        )
        unknown = await client.get(
            route, params={"actor_id": "alice", "allocation_id": "nope"}, headers=headers
        )
        long = await client.get(
            route, params={"actor_id": "alice", "allocation_id": "x" * 201}, headers=headers
        )
    assert current.status_code == 200
    assert (current.json()["allocation_id"], current.json()["other_allocations"]) == (second, [first])
    assert earlier.status_code == 200 and earlier.json()["balanced"] is True
    assert (earlier.json()["allocation_id"], earlier.json()["other_allocations"]) == (first, [second])
    assert unknown.status_code == 404 and long.status_code == 422


# -- a supplied store whose cost snapshot cannot say which allocation paid an attempt ----------------------


class _NoAttemptAllocations:
    """A supplied store from before #115: everything the SQL store does, except that its cost snapshot
    lacks ``attempt_allocations``."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        if name == "attempt_allocations":
            raise AttributeError(name)
        if name == "cost_snapshot":
            return self._snapshot
        return getattr(self._inner, name)

    def _snapshot(self) -> Any:
        return _Opened(self._inner.cost_snapshot())


class _Opened:
    def __init__(self, opening: Any) -> None:
        self._opening = opening

    async def __aenter__(self) -> _NoAttemptAllocations:
        reads = await self._opening.__aenter__()
        assert isinstance(reads, AllocationCostSnapshot)  # the packaged snapshot has the read
        return _NoAttemptAllocations(reads)

    async def __aexit__(self, *exc: object) -> None:
        await self._opening.__aexit__(*exc)


async def test_a_store_that_cannot_say_which_allocation_paid_reconciles_as_before(
    make_engine: EngineFactory, store: SqlStore, admin: ActorContext, alice: ActorContext,
    transport: ScriptedTransport, usage: FakeUsageSource, manager: FakeAllocationManager,
) -> None:  # fmt: skip
    """The read is optional for a store's cost snapshot (``AllocationCostSnapshot``). Without it the engine
    still reconciles the current allocation, counting every attempt of the actor as it did before, lists
    no other allocation, and knows no earlier one to reconcile."""
    supplied = _NoAttemptAllocations(store)
    engine = await make_engine(default_currency="ETH", supply=EngineSupply(store=supplied))
    first, second = await two_allocations(engine, admin, alice, transport, usage, manager)

    report = await engine.costs.reconcile(admin, "default", "alice")
    assert report.allocation_id == second and report.other_allocations == ()
    assert counted(report) == (Decimal("0.5"), Decimal("0.1"))  # both allocations' jobs: the old behaviour
    assert report.balanced is False
    assert await engine.costs.reconcile(admin, "default", "alice", second) == report
    with pytest.raises(NotFound, match="never paid"):
        await engine.costs.reconcile(admin, "default", "alice", first)
