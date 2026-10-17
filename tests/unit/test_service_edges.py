"""Service guard clauses: the branches that protect money and correctness when something is missing."""

import asyncio
import logging
from decimal import Decimal
from uuid import uuid4

import pytest

from livepeer_builder import BuilderEngine, EngineSettings
from livepeer_builder.adapters.secrets import EncryptedDbSecretStore
from livepeer_builder.adapters.static import StaticDiscoverySource
from livepeer_builder.contracts import (
    ActorContext,
    FundRequest,
    JobRequest,
    Money,
    ProvisionRequest,
    SyncCheckpoint,
    UsagePage,
)
from livepeer_builder.errors import (
    AccessDenied,
    CursorMismatch,
    InvalidRequest,
    JobFailed,
    NotFound,
    NotImplementedInMilestone,
    ProviderUnavailable,
    RunnerCallError,
)
from livepeer_builder.services.costs import CostSyncWorker, _balanced, add_fees
from livepeer_builder.services.discovery import DiscoveryService
from livepeer_builder.services.policy import DefaultPolicy
from livepeer_builder.store import SqlStore
from livepeer_builder.testing import FakeAllocationManager, FixedClock, ScriptedTransport, runner
from tests.conftest import EngineFactory

APP = "example/echo"


def usd(amount: str) -> Money:
    return Money(amount=Decimal(amount), currency="USD")


def provision(actor_id: str = "alice", amount: str = "5", grant_id: str = "grant_1") -> ProvisionRequest:
    return ProvisionRequest(
        application_id="default",
        actor_id=actor_id,
        amount=usd(amount),
        grant_id=grant_id,
    )


# -- payments ------------------------------------------------------------------------------------------


async def test_payments_guard_clauses(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, manager: FakeAllocationManager
) -> None:
    engine = await make_engine()
    with pytest.raises(InvalidRequest, match="must be in USD"):
        await engine.payments.provision(
            admin,
            ProvisionRequest(
                application_id="default", actor_id="a", amount=Money(amount=Decimal(1), currency="ETH")
            ),
        )
    with pytest.raises(NotFound):
        await engine.payments.fund(admin, "default", "ghost", FundRequest(amount=usd("1"), operation_ref="x"))
    with pytest.raises(NotFound, match="no allocation"):
        await engine.payments.allowance(alice)

    await engine.payments.provision(admin, provision())
    paused = await engine.payments.set_status(admin, "default", "alice", "paused")
    assert paused.status == "paused" and manager.allocations[paused.allocation_id].status == "paused"
    with pytest.raises(InvalidRequest, match="use revoke"):
        await engine.payments.set_status(admin, "default", "alice", "revoked")
    with pytest.raises(AccessDenied):
        await engine.payments.set_status(alice, "default", "alice", "active")


async def test_reprovisioning_after_revoke_is_a_new_operation(
    make_engine: EngineFactory, admin: ActorContext, manager: FakeAllocationManager, clock: FixedClock
) -> None:
    engine = await make_engine()
    first = await engine.payments.provision(admin, provision())
    await engine.payments.revoke(admin, "default", "alice")
    clock.advance(1)
    second = await engine.payments.provision(admin, provision())
    assert second.allocation_id != first.allocation_id  # not a replay of the revoked allocation
    assert len(manager.allocations) == 2


async def test_provisioning_needs_a_provider_and_a_grant(
    store: SqlStore, clock: FixedClock, admin: ActorContext
) -> None:
    secret_key = EncryptedDbSecretStore.generate_key()
    no_provider = await BuilderEngine.from_settings(
        EngineSettings(database_url="sqlite+aiosqlite:///:memory:", secret_key=secret_key, log_json=False),
        store=store, clock=clock, discovery_source=StaticDiscoverySource(),
    )  # fmt: skip
    with pytest.raises(ProviderUnavailable, match="management API"):
        await no_provider.payments.provision(admin, provision())
    no_grant = await BuilderEngine.from_settings(
        EngineSettings(database_url="sqlite+aiosqlite:///:memory:", secret_key=secret_key, log_json=False),
        store=store, clock=clock, discovery_source=StaticDiscoverySource(), allocation_manager=FakeAllocationManager(),
    )  # fmt: skip
    with pytest.raises(InvalidRequest, match="choose a grant"):
        await no_grant.payments.provision(admin, provision(grant_id=""))
    with pytest.raises(ProviderUnavailable, match="management API"):
        await no_provider.payments.fund_grant(
            admin, FundRequest(amount=usd("1"), operation_ref="g", grant_id="grant_1")
        )
    with pytest.raises(InvalidRequest, match="choose a grant"):
        await no_grant.payments.fund_grant(admin, FundRequest(amount=usd("1"), operation_ref="g"))


async def test_per_application_without_a_key_is_unpaid(
    make_engine: EngineFactory, alice: ActorContext
) -> None:
    engine = await make_engine(allocation_mode="per_application", shared_allocation_id="alloc_shared")
    unpaid = await engine.payments.credential(alice)  # off-chain: calls go unpaid
    assert unpaid.token is None and unpaid.allocation_id is None
    assert await engine.payments.allocation_for("default", "alice") == "alloc_shared"


# -- costs ---------------------------------------------------------------------------------------------


async def test_cost_service_guard_clauses(
    make_engine: EngineFactory,
    store: SqlStore,
    clock: FixedClock,
    admin: ActorContext,
    alice: ActorContext,
    bob: ActorContext,
) -> None:
    engine = await make_engine()
    with pytest.raises(AccessDenied):
        await engine.costs.sync_now(alice)
    with pytest.raises(NotFound):
        await engine.costs.for_job(alice, uuid4())
    assert await engine.costs.refresh_projection(uuid4()) is None
    with pytest.raises(NotFound, match="no allocation"):
        await engine.costs.reconcile(admin, "default", "nobody")

    await engine.payments.provision(admin, provision())
    engine.jobs._transport = ScriptedTransport([ScriptedTransport.ok()])
    job = (await engine.jobs.run(alice, JobRequest(app=APP))).job
    with pytest.raises(NotFound):
        await engine.costs.for_job(bob, job.id)  # another actor's cost is invisible


async def test_costs_without_a_provider(store: SqlStore, clock: FixedClock, admin: ActorContext) -> None:
    engine = await BuilderEngine.from_settings(
        EngineSettings(database_url="sqlite+aiosqlite:///:memory:", log_json=False),
        store=store, clock=clock, discovery_source=StaticDiscoverySource(),
    )  # fmt: skip
    with pytest.raises(ProviderUnavailable, match="no usage source"):
        await engine.costs.sync_once()
    engine.costs._allowances = None
    with pytest.raises(ProviderUnavailable, match="no payment provider"):
        await engine.costs.reconcile(admin, "default", None)


async def test_reconciliation_survives_an_unavailable_provider(
    make_engine: EngineFactory,
    admin: ActorContext,
    manager: FakeAllocationManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = await make_engine()
    await engine.payments.provision(admin, provision())

    async def unavailable(_: str) -> object:
        raise ProviderUnavailable("down")

    monkeypatch.setattr(manager, "read_allocation", unavailable)
    result = await engine.costs.reconcile(admin, "default", "alice")
    assert result.provider_spent is None and result.balanced is None  # unknown, never "balanced"


def test_balance_check_by_currency() -> None:
    from livepeer_builder.contracts import Fees

    priced = [Fees(fee_eth=Decimal("0.1"), fee_usd=Decimal("2"), row_count=1)]
    assert _balanced(None, priced) is None
    assert _balanced(usd("2"), priced) is True
    assert _balanced(Money(amount=Decimal("0.1"), currency="ETH"), priced) is True
    unpriced = [Fees(fee_eth=Decimal("0.1"), fee_usd=None, row_count=1)]
    assert _balanced(usd("2"), unpriced) is None  # can't compare USD without USD prices
    assert add_fees([]).fee_usd == Decimal(0)


async def test_a_second_cursor_rejection_is_raised(store: SqlStore) -> None:
    class AlwaysRejects:
        async def page(self, cursor: str | None, limit: int = 1000) -> UsagePage:
            raise CursorMismatch("invalid cursor")

    await store.apply_usage((), SyncCheckpoint(resume_cursor="stale"))
    with pytest.raises(CursorMismatch):
        await CostSyncWorker(AlwaysRejects(), store).sync_once()


# -- discovery -----------------------------------------------------------------------------------------


async def test_discovery_keeps_the_last_good_snapshot(clock: FixedClock, alice: ActorContext) -> None:
    class Flaky:
        def __init__(self) -> None:
            self.fail = False

        async def fetch(self) -> tuple:  # type: ignore[type-arg]
            if self.fail:
                raise RuntimeError("signer down")
            return (runner(price=None),)

    source = Flaky()
    discovery = DiscoveryService(source, DefaultPolicy(), clock, interval_s=0.01)
    assert discovery.offerings(alice) == []  # nothing observed yet
    await discovery.refresh()
    source.fail = True
    snapshot = await discovery.refresh()
    assert snapshot.last_error == "signer down" and len(snapshot.runners) == 1
    (offering,) = discovery.offerings(alice)
    assert offering.rate_low is None  # no priced runners: no comparable rate

    stop = asyncio.Event()
    task = asyncio.create_task(discovery.run(stop))
    await asyncio.sleep(0.03)
    stop.set()
    await task


# -- jobs ----------------------------------------------------------------------------------------------


async def test_job_guard_clauses(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext
) -> None:
    engine = await make_engine()
    with pytest.raises(InvalidRequest, match="must start with"):
        await engine.jobs.run(alice, JobRequest(app=APP, path="no-slash"))
    with pytest.raises(NotImplementedInMilestone):
        await engine.jobs.submit(alice, JobRequest(app=APP))
    with pytest.raises(NotFound):
        await engine.jobs.get(alice, uuid4())

    await engine.payments.provision(admin, provision())
    engine.jobs._transport = ScriptedTransport(
        [RunnerCallError("http", "boom", payment_sent=False, status_code=500)]
    )
    with pytest.raises(JobFailed) as failed:
        await engine.jobs.run(alice, JobRequest(app=APP))
    with pytest.raises(InvalidRequest, match="succeeded job"):
        await engine.jobs.report_usage(alice, failed.value.job.id, {"x": 1})


async def test_usage_needs_a_quantity(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext
) -> None:
    engine = await make_engine()
    await engine.payments.provision(admin, provision())
    engine.jobs._transport = ScriptedTransport([ScriptedTransport.ok()])
    job = (await engine.jobs.run(alice, JobRequest(app=APP))).job
    with pytest.raises(InvalidRequest, match="non-negative"):
        await engine.jobs.report_usage(alice, job.id, {"tokens": -1})


async def test_engine_error_mid_call_leaves_the_job_uncertain(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, store: SqlStore
) -> None:
    engine = await make_engine()
    await engine.payments.provision(admin, provision())

    class Crashing(ScriptedTransport):
        async def call(self, r, request, credential):  # type: ignore[no-untyped-def]
            raise KeyError("bug in a transport")

    engine.jobs._transport = Crashing()
    with pytest.raises(KeyError):
        await engine.jobs.run(alice, JobRequest(app=APP))
    (job,) = await store.list_jobs("default", "alice")
    assert job.state == "uncertain"  # the attempt was in flight: it may have paid


async def test_failure_to_close_a_job_is_logged(
    make_engine: EngineFactory,
    admin: ActorContext,
    alice: ActorContext,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    engine = await make_engine()
    await engine.payments.provision(admin, provision())

    class Crashing(ScriptedTransport):
        async def call(self, r, request, credential):  # type: ignore[no-untyped-def]
            raise KeyError("bug")

    async def broken_finish(*_: object) -> None:
        raise RuntimeError("database gone")

    engine.jobs._transport = Crashing()
    monkeypatch.setattr(engine.jobs._store, "finish_job", broken_finish)
    with caplog.at_level(logging.ERROR), pytest.raises(KeyError):
        await engine.jobs.run(alice, JobRequest(app=APP))
    assert any(getattr(r, "event", "") == "jobs.close_failed" for r in caplog.records)


async def test_payment_refused_after_signing_is_a_payment_failure(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext
) -> None:
    engine = await make_engine(runners=(runner(url="https://r1"), runner(url="https://r2")))
    await engine.payments.provision(admin, provision())
    engine.jobs._transport = ScriptedTransport(
        [RunnerCallError("payment_refused", "late refusal", payment_sent=True, manifest_ids=("m1",))]
    )
    with pytest.raises(JobFailed) as failed:
        await engine.jobs.run(alice, JobRequest(app=APP))
    assert failed.value.job.failure is not None and failed.value.job.failure.kind == "payment"
    assert len(failed.value.job.attempts) == 1  # paid: never failed over
