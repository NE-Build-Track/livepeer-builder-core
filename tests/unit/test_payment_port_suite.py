"""#168's "Done when": one suite, run against the local adapters and the remote adapters (D84).

Every test here runs twice. ``local`` is an engine whose payment ports reach the payment provider
itself. ``remote`` is an enterprise's engine whose ports are the remote adapters, calling a standalone
clearinghouse's public listener in this process: the tenant routes, mounted to hand each allocation's
key over and store none, over an engine, a store and a secret store of the clearinghouse's own
(``tests/unit/payment_ports.py``). The same commands must leave the same money at the provider and the
same records in the engine, whichever way they went.

The suite covers ``AllocationManager`` (allot, top up, pause, resume, revoke, rotate, read the grant),
``AllowanceReader``, ``UsageSource`` and ``CredentialResolver``, the refusals, and **signing**, which
happens inside ``RunnerTransport``: its last section runs a whole fixed job and a whole live job, with a
real signer and runner on loopback sockets (``tests/unit/paying_ports.py``). Either engine signs at the
signer itself, with the allocation's own key; the clearinghouse's engine is in no payment's path.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from livepeer_gateway import remote_signer

from livepeer_builder.contracts import AllotRequest, JobCost, JobRequest, TopUpRequest, UsageRow
from livepeer_builder.errors import (
    AccessDenied,
    AlreadyAllotted,
    CursorMismatch,
    JobFailed,
    NotFound,
    NotProvisioned,
    ProviderError,
)
from livepeer_builder.services.ledger import definite_refusal
from tests.unit.livepeer_net import Net, Reply, start
from tests.unit.paying_ports import FIXED_APP, LIVE_APP, open_paying_ports
from tests.unit.payment_ports import SIDES, Ports, Side, open_ports, usd


@pytest.fixture(params=SIDES)
async def ports(request: pytest.FixtureRequest) -> AsyncIterator[Ports]:
    side: Side = request.param
    async with open_ports(side) as opened:
        yield opened


def allot_request(ports: Ports, actor_id: str, amount: str = "5", ref: str | None = None) -> AllotRequest:
    return AllotRequest(
        tenant_id=ports.tenant_id,
        actor_id=actor_id,
        amount=usd(amount),
        operation_ref=ref or f"allot-{actor_id}",
    )


async def allotted(ports: Ports, actor_id: str = "alice", amount: str = "5") -> str:
    """Allot ``actor_id`` an allocation and return the allocation's id."""
    answer = await ports.engine.payments.allot(ports.admin, allot_request(ports, actor_id, amount))
    return answer.allocation_id


def row(number: int, allocation_id: str) -> UsageRow:
    return UsageRow(
        id=f"row-{number}",
        event_id=f"event-{number}",
        status="applied",
        manifest_id=f"manifest-{number}",
        allocation_id=allocation_id,
        payment_session_id=f"session-{number}",
        request_id=None,
        pipeline="live-video",
        computed_fee_eth=None,
        computed_fee_usd=Decimal("0.01") * number,
        currency="usd",
        created_at=datetime(2026, 11, 19, 12, number, tzinfo=UTC),
    )


# -- AllocationManager: allot -------------------------------------------------------------------------------


async def test_allotting_takes_the_amount_from_the_tenant_grant_and_records_the_allocation(
    ports: Ports,
) -> None:
    answer = await ports.engine.payments.allot(ports.admin, allot_request(ports, "alice"))

    assert (answer.tenant_id, answer.actor_id, answer.status) == (ports.tenant_id, "alice", "active")
    held = ports.provider.allocations[answer.allocation_id]  # the provider made exactly this allocation
    assert (held.grant_id, held.granted, held.status) == (ports.tenant_id, Decimal(5), "active")
    assert len(ports.provider.allocations) == 1
    assert ports.provider.unallocated(ports.tenant_id) == Decimal(95)
    stored = await ports.engine.store.get_actor(ports.tenant_id, "alice")
    assert stored is not None and stored.allocation_id == answer.allocation_id


async def test_a_repeat_of_an_allotment_allots_nothing_more(ports: Ports) -> None:
    request = allot_request(ports, "alice")
    first = await ports.engine.payments.allot(ports.admin, request)
    again = await ports.engine.payments.allot(ports.admin, request)

    assert again.allocation_id == first.allocation_id
    assert len(ports.provider.allocations) == 1
    assert ports.provider.unallocated(ports.tenant_id) == Decimal(95)


async def test_a_second_allotment_of_an_allotted_actor_is_refused_and_adds_nothing(ports: Ports) -> None:
    await allotted(ports)
    with pytest.raises(AlreadyAllotted):
        await ports.engine.payments.allot(ports.admin, allot_request(ports, "alice", "7", ref="again"))
    assert ports.provider.unallocated(ports.tenant_id) == Decimal(95)


async def test_an_allotment_of_zero_is_an_identity_with_no_money(ports: Ports) -> None:
    answer = await ports.engine.payments.allot(ports.admin, allot_request(ports, "zed", "0"))
    assert answer.status == "exhausted"
    assert ports.provider.unallocated(ports.tenant_id) == Decimal(100)


# -- AllocationManager: top up, pause, resume, revoke, rotate -----------------------------------------------


async def test_a_top_up_adds_to_the_allocation_once_however_often_it_is_repeated(ports: Ports) -> None:
    allocation_id = await allotted(ports)
    top_up = TopUpRequest(amount=usd(3), operation_ref="top-up-1")

    await ports.engine.payments.top_up(ports.admin, ports.tenant_id, "alice", top_up)
    await ports.engine.payments.top_up(ports.admin, ports.tenant_id, "alice", top_up)

    assert ports.provider.allocations[allocation_id].granted == Decimal(8)
    assert ports.provider.unallocated(ports.tenant_id) == Decimal(92)
    another = TopUpRequest(amount=usd(1), operation_ref="top-up-2")
    await ports.engine.payments.top_up(ports.admin, ports.tenant_id, "alice", another)
    assert ports.provider.allocations[allocation_id].granted == Decimal(9)


async def test_a_top_up_makes_an_exhausted_allocation_active(ports: Ports) -> None:
    await ports.engine.payments.allot(ports.admin, allot_request(ports, "zed", "0"))
    topped = await ports.engine.payments.top_up(
        ports.admin, ports.tenant_id, "zed", TopUpRequest(amount=usd(2), operation_ref="first-money")
    )
    assert topped.status == "active"


async def test_pausing_and_resuming_an_allocation_reaches_the_provider(ports: Ports) -> None:
    allocation_id = await allotted(ports)

    paused = await ports.engine.payments.set_status(ports.admin, ports.tenant_id, "alice", "paused")
    assert paused.status == "paused" and ports.provider.allocations[allocation_id].status == "paused"
    assert (await ports.engine.payments.allowance(ports.actor("alice"))).status == "paused"

    resumed = await ports.engine.payments.set_status(ports.admin, ports.tenant_id, "alice", "active")
    assert resumed.status == "active" and ports.provider.allocations[allocation_id].status == "active"


async def test_revoking_returns_what_was_not_spent_and_ends_the_payment_key(ports: Ports) -> None:
    allocation_id = await allotted(ports)
    key = await ports.credentials.credential(ports.actor("alice"))
    assert key.token is not None
    ports.provider.allocations[allocation_id].spent = Decimal(2)

    revoked = await ports.engine.payments.revoke(ports.admin, ports.tenant_id, "alice")

    assert revoked.status == "revoked" and ports.provider.allocations[allocation_id].status == "revoked"
    assert ports.provider.unallocated(ports.tenant_id) == Decimal(98)  # 5 allotted, 2 spent, 3 returned
    assert await ports.pays_for(key.token) is None
    with pytest.raises(NotProvisioned):
        await ports.credentials.credential(ports.actor("alice"))
    again = await ports.engine.payments.revoke(ports.admin, ports.tenant_id, "alice")  # a repeat is no error
    assert again.status == "revoked"
    assert ports.provider.unallocated(ports.tenant_id) == Decimal(98)


async def test_an_actor_can_be_allotted_again_after_a_revoke(ports: Ports) -> None:
    first = await allotted(ports)
    await ports.engine.payments.revoke(ports.admin, ports.tenant_id, "alice")

    second = await ports.engine.payments.allot(ports.admin, allot_request(ports, "alice", "4", ref="anew"))

    assert second.allocation_id != first and second.status == "active"
    assert ports.provider.allocations[second.allocation_id].granted == Decimal(4)
    key = await ports.credentials.credential(ports.actor("alice"))
    assert key.token is not None and await ports.pays_for(key.token) == second.allocation_id


async def test_rotating_replaces_the_payment_key_on_the_same_allocation(ports: Ports) -> None:
    allocation_id = await allotted(ports)
    old = await ports.credentials.credential(ports.actor("alice"))

    rotated = await ports.engine.payments.rotate_payment_key(ports.admin, ports.tenant_id, "alice", "rot-1")
    new = await ports.credentials.credential(ports.actor("alice"))

    assert rotated.allocation_id == new.allocation_id == allocation_id
    assert old.token is not None and new.token is not None and new.token != old.token
    assert await ports.pays_for(new.token) == allocation_id
    assert await ports.pays_for(old.token) is None  # the old one stopped
    assert ports.provider.allocations[allocation_id].granted == Decimal(5)  # a rotation moves no money

    replayed = await ports.engine.payments.rotate_payment_key(ports.admin, ports.tenant_id, "alice", "rot-1")
    assert replayed.api_key_id == rotated.api_key_id
    assert (await ports.credentials.credential(ports.actor("alice"))).token == new.token


# -- AllocationManager: the grant ---------------------------------------------------------------------------


async def test_the_grant_is_read_with_its_total_its_unallocated_balance_and_what_was_spent(
    ports: Ports,
) -> None:
    allocation_id = await allotted(ports)
    ports.provider.allocations[allocation_id].spent = Decimal("1.5")

    grant = await ports.manager.read_grant(ports.tenant_id)

    assert (grant.tenant_id, grant.name, grant.status) == (ports.tenant_id, "one", "active")
    assert (grant.total, grant.unallocated, grant.spent) == (usd(100), usd(95), usd("1.5"))


# -- CredentialResolver -------------------------------------------------------------------------------------


async def test_the_resolver_returns_the_payment_key_that_pays_from_the_actor_allocation(ports: Ports) -> None:
    allocation_id = await allotted(ports)
    key = await ports.credentials.credential(ports.actor("alice"))

    assert key.allocation_id == allocation_id and key.token
    assert await ports.pays_for(key.token) == allocation_id
    again = await ports.credentials.credential(ports.actor("alice"))
    assert again == key  # resolved at call time, and the same until it is rotated


async def test_each_actor_has_a_payment_key_of_its_own(ports: Ports) -> None:
    alice, bob = await allotted(ports, "alice"), await allotted(ports, "bob")
    keys = [await ports.credentials.credential(ports.actor(name)) for name in ("alice", "bob")]
    assert [key.allocation_id for key in keys] == [alice, bob]
    assert keys[0].token != keys[1].token


# -- AllowanceReader ----------------------------------------------------------------------------------------


async def test_the_allowance_is_what_the_provider_reports_for_the_actor_allocation(ports: Ports) -> None:
    allocation_id = await allotted(ports)
    assert await ports.allowances.allocation_for(ports.tenant_id, "alice") == allocation_id
    assert await ports.allowances.allocation_for(ports.tenant_id, "nobody") is None

    fresh = await ports.engine.payments.allowance(ports.actor("alice"))
    assert (fresh.allocation_id, fresh.status) == (allocation_id, "active")
    assert (fresh.granted, fresh.spent, fresh.available) == (usd(5), usd(0), usd(5))

    ports.provider.allocations[allocation_id].spent = Decimal("1.25")
    spent = await ports.allowances.read_allowance(allocation_id)
    assert (spent.granted, spent.spent, spent.available) == (usd(5), usd("1.25"), usd("3.75"))


async def test_reconciliation_reads_what_the_provider_says_the_allocation_spent(ports: Ports) -> None:
    allocation_id = await allotted(ports)
    ports.provider.allocations[allocation_id].spent = Decimal("0.5")
    report = await ports.engine.costs.reconcile(ports.admin, ports.tenant_id, "alice")
    assert report.allocation_id == allocation_id and report.provider_spent == usd("0.5")


# -- UsageSource --------------------------------------------------------------------------------------------


async def test_the_usage_is_paged_to_the_last_page_in_order(ports: Ports) -> None:
    allocation_id = await allotted(ports)
    rows = [row(number, allocation_id) for number in range(1, 6)]
    ports.provider_usage.rows = list(rows)
    ports.provider_usage.grant_of = {allocation_id: ports.tenant_id}

    read: list[UsageRow] = []
    cursor: str | None = None
    pages = 0
    while True:
        page = await ports.usage.page(cursor, limit=2)
        read += page.items
        pages += 1
        if not page.next_cursor:
            break
        cursor = page.next_cursor

    assert read == rows and pages == 3
    assert read[0].computed_fee_usd == Decimal("0.01")  # exact, as the provider stated it


async def test_a_cursor_the_provider_refuses_is_a_cursor_mismatch(ports: Ports) -> None:
    with pytest.raises(CursorMismatch):
        await ports.usage.page("no-such-cursor")


async def test_cost_sync_reads_the_usage_through_the_port(ports: Ports) -> None:
    allocation_id = await allotted(ports)
    ports.provider_usage.rows = [row(number, allocation_id) for number in range(1, 4)]
    ports.provider_usage.grant_of = {allocation_id: ports.tenant_id}

    report = await ports.engine.costs.sync_now(ports.admin)

    assert report.rows_seen == 3 and report.pages == 2


# -- the refusals -------------------------------------------------------------------------------------------


async def test_a_tenant_admin_cannot_allot_in_another_tenant_and_nothing_reaches_the_provider(
    ports: Ports,
) -> None:
    calls, sent = list(ports.provider.calls), len(ports.requests)
    request = AllotRequest(
        tenant_id=ports.other_tenant, actor_id="mallory", amount=usd(1), operation_ref="not-mine"
    )
    with pytest.raises(AccessDenied):
        await ports.engine.payments.allot(ports.tenant_admin(), request)
    with pytest.raises(AccessDenied):
        await ports.engine.payments.top_up(
            ports.tenant_admin(),
            ports.other_tenant,
            "mallory",
            TopUpRequest(amount=usd(1), operation_ref="x"),
        )
    assert ports.provider.calls == calls and len(ports.requests) == sent
    assert ports.provider.unallocated(ports.other_tenant) == Decimal(100)


async def test_an_allocation_larger_than_what_the_grant_has_left_is_refused_and_frees_the_reference(
    ports: Ports,
) -> None:
    await allotted(ports, "alice", "60")
    too_much = allot_request(ports, "bob", "41", ref="bob-1")

    with pytest.raises(ProviderError) as refused:
        await ports.engine.payments.allot(ports.admin, too_much)

    assert refused.value.status_code == 409 and "insufficient" in refused.value.message
    assert definite_refusal(refused.value)
    assert len(ports.provider.allocations) == 1 and ports.provider.unallocated(ports.tenant_id) == Decimal(40)
    assert await ports.engine.store.get_actor(ports.tenant_id, "bob") is None
    with pytest.raises(NotProvisioned):
        await ports.credentials.credential(ports.actor("bob"))
    # nothing was applied, so the same reference may carry an amount the grant covers
    fits = await ports.engine.payments.allot(ports.admin, allot_request(ports, "bob", "40", ref="bob-1"))
    assert fits.status == "active" and ports.provider.unallocated(ports.tenant_id) == Decimal(0)


async def test_a_top_up_larger_than_what_the_grant_has_left_is_refused(ports: Ports) -> None:
    allocation_id = await allotted(ports, "alice", "90")
    with pytest.raises(ProviderError) as refused:
        await ports.engine.payments.top_up(
            ports.admin, ports.tenant_id, "alice", TopUpRequest(amount=usd(11), operation_ref="too-much")
        )
    assert refused.value.status_code == 409 and definite_refusal(refused.value)
    assert ports.provider.allocations[allocation_id].granted == Decimal(90)


async def test_an_actor_with_no_allocation_is_refused_on_every_command_and_read(ports: Ports) -> None:
    calls, sent = list(ports.provider.calls), len(ports.requests)
    payments = ports.engine.payments
    with pytest.raises(NotFound):
        await payments.top_up(
            ports.admin, ports.tenant_id, "nobody", TopUpRequest(amount=usd(1), operation_ref="t")
        )
    with pytest.raises(NotFound):
        await payments.set_status(ports.admin, ports.tenant_id, "nobody", "paused")
    with pytest.raises(NotFound):
        await payments.rotate_payment_key(ports.admin, ports.tenant_id, "nobody", "r")
    with pytest.raises(NotFound):
        await payments.revoke(ports.admin, ports.tenant_id, "nobody")
    with pytest.raises(NotFound):
        await payments.allowance(ports.actor("nobody"))
    with pytest.raises(NotProvisioned):
        await ports.credentials.credential(ports.actor("nobody"))
    assert ports.provider.calls == calls and len(ports.requests) == sent


async def test_a_grant_the_provider_does_not_hold_is_a_tenant_that_was_never_onboarded(ports: Ports) -> None:
    request = AllotRequest(
        tenant_id="no-such-grant", actor_id="alice", amount=usd(1), operation_ref="nowhere"
    )
    with pytest.raises((NotFound, ProviderError, AccessDenied)) as refused:
        await ports.engine.payments.allot(ports.admin, request)
    assert definite_refusal(refused.value)
    assert not ports.provider.allocations


# -- signing, inside RunnerTransport ------------------------------------------------------------------------


@pytest.fixture
async def net() -> AsyncIterator[Net]:
    async for value in start():
        yield value


@pytest.fixture(params=SIDES)
async def paying(request: pytest.FixtureRequest, net: Net) -> AsyncIterator[Ports]:
    side: Side = request.param
    async with open_paying_ports(side, net) as opened:
        yield opened


def batteries_key(ports: Ports, allocation_id: str) -> str:
    """The key the provider minted for the allocation: what the signer must be called with."""
    [secret] = [secret for held, secret, revoked in ports.provider.keys.values() if held == allocation_id]
    return secret


async def cost_of(ports: Ports, job_id: UUID, allocation_id: str, manifest_id: str, fee: str) -> JobCost:
    """The job's cost once the provider's usage row for its payment has been synced through the port."""
    usage_row = row(len(ports.provider_usage.rows) + 1, allocation_id).model_copy(
        update={"manifest_id": manifest_id, "computed_fee_usd": Decimal(fee)}
    )
    ports.provider_usage.rows.append(usage_row)
    ports.provider_usage.grant_of[allocation_id] = ports.tenant_id
    await ports.engine.costs.sync_now(ports.admin)
    return await ports.engine.costs.for_job(ports.actor("alice"), job_id)


async def test_a_fixed_job_and_a_live_job_are_paid_through_each_set_of_adapters(
    paying: Ports, net: Net, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A whole job of each kind, from the engine's ``jobs.run`` to its record and its cost. The same
    runner and the same signer serve both engines, and the signer is called with the Batteries key of
    the actor's allocation either way, by the engine that runs the job: the key its own Batteries call
    minted, or the one the clearinghouse handed over at allotment. Nothing of a payment reaches the
    clearinghouse's engine."""
    ports, alice = paying, paying.actor("alice")
    allocation_id = await allotted(ports)
    signs_with = f"Bearer {batteries_key(ports, allocation_id)}"

    # fixed work: one payment, up front
    fixed = await ports.engine.jobs.run(alice, JobRequest(app=FIXED_APP, payload={"prompt": "hello"}))
    assert (fixed.status_code, fixed.job.state) == (200, "succeeded")
    [attempt] = fixed.job.attempts
    assert attempt.payment_sent and attempt.allocation_id == allocation_id
    assert [session.manifest_id for session in attempt.sessions] == ["m-1"]
    assert net.log.count("sign m-1") == 1 and "request pay-m-1" in net.log
    assert {headers.get("Authorization") for headers in net.signer_headers} == {signs_with}
    cost = await cost_of(ports, fixed.job.id, allocation_id, "m-1", "0.25")
    assert cost.status == "observed" and cost.fees.fee_usd == Decimal("0.25")
    assert [attempt_cost.cost_class for attempt_cost in cost.attempts] == ["delivered"]

    # live work: a payment each cycle, for as long as the work runs
    monkeypatch.setattr(remote_signer, "PAYMENT_INTERVAL_S", 0.02)
    net.replies = [Reply(hang_s=0.5)]
    live = await ports.engine.jobs.run(alice, JobRequest(app=LIVE_APP, payload={"n": 1}))
    assert (live.status_code, live.job.state) == (200, "succeeded")
    assert [session.manifest_id for session in live.job.attempts[0].sessions] == ["m-2"]
    assert net.log.count("sign m-2") >= 3  # the opening payment, then the cycles
    assert all(call["type"] == "live" for call in net.signer_requests[1:])
    assert {headers.get("Authorization") for headers in net.signer_headers} == {signs_with}
    cost = await cost_of(ports, live.job.id, allocation_id, "m-2", "0.5")
    assert cost.status == "observed" and cost.billable.fee_usd == Decimal("0.5")

    # what the clearinghouse's listener saw of all that (remote): the tenant's routes, each with the tenant
    # admin key. No signer path, and no actor's key
    for path, headers in ports.seen:
        assert path.startswith("/v1/clearinghouse/"), path
        assert headers.get("Authorization") == f"Bearer {ports.token}"
    assert (ports.side == "remote") is bool(ports.seen)


async def test_a_job_the_signer_refuses_fails_with_its_reason_and_nothing_paid(
    paying: Ports, net: Net
) -> None:
    """An empty allocation is the signer's 402, whoever runs the signer, and the job records it:
    ``payment``, ``allowance_exhausted``, nothing paid."""
    ports, alice = paying, paying.actor("alice")
    await allotted(ports)
    net.sign = [402]

    with pytest.raises(JobFailed) as failed:
        await ports.engine.jobs.run(alice, JobRequest(app=FIXED_APP, payload={}))

    recorded = await ports.engine.jobs.get(alice, failed.value.job.id)
    assert recorded.state == "failed" and recorded.failure is not None
    failure = recorded.failure
    assert (failure.kind, failure.reason, failure.status_code) == ("payment", "allowance_exhausted", 402)
    assert not failure.payment_sent and [attempt.payment_sent for attempt in recorded.attempts] == [False]
    assert "request pay-m-1" not in net.log  # the runner did no work


async def test_an_actor_with_no_allocation_never_reaches_a_signer(paying: Ports, net: Net) -> None:
    with pytest.raises(JobFailed) as failed:
        await paying.engine.jobs.run(paying.actor("nobody"), JobRequest(app=FIXED_APP, payload={}))
    assert failed.value.job.failure is not None and failed.value.job.failure.kind == "not_provisioned"
    assert net.log == [] and net.signer_headers == []
