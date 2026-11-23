"""The wallet report (D82, #167): what a clearinghouse admin watches the wallet by.

"Each clearinghouse has one wallet. Its deposit stays above the total of unspent grants, with a margin
for the ETH price. The clearinghouse admin watches it and funds it by hand" (the ISS-31 guide). The
report states the three things that sentence needs and decides nothing:

- the **deposit and reserve**, as the payment provider's on-chain listener has confirmed them, in ETH;
- the **total of unspent grants**, for each currency a grant is in: every grant's unallocated balance
  and what each of its allocations still holds, summed. An allocation that spent more than it held
  takes nothing off the rest. Nothing is converted between currencies, and no margin is computed;
- whether the **signer** answers.

Only a clearinghouse admin reads it. The addresses below are made up: no test holds a real one.
"""

from decimal import Decimal

import httpx
import pytest

from livepeer_builder import BuilderEngine, EngineSupply
from livepeer_builder.adapters.batteries import BatteriesClient
from livepeer_builder.adapters.sdk import SdkRunnerTransport
from livepeer_builder.contracts import (
    ActorContext,
    AllotRequest,
    EscrowAccount,
    Money,
    SignerReadiness,
    TenantGrant,
    UnspentGrants,
)
from livepeer_builder.errors import AccessDenied, ProviderUnavailable
from livepeer_builder.http import create_app
from livepeer_builder.protocols import EscrowSource, SignerProbe
from livepeer_builder.services.wallet import unspent_grants
from livepeer_builder.testing import FakeAllocationManager, ScriptedStreamTransport
from tests.conftest import EngineFactory

ACCOUNT = EscrowAccount(
    chain_id="42161",
    contract_address="0x" + "c0" * 20,
    sender="0x" + "5e" * 20,
    deposit=Money(amount=Decimal("1.5"), currency="ETH"),
    reserve=Money(amount=Decimal("0.25"), currency="ETH"),
    confirmed_through_block=412,
)


def money(amount: str, currency: str = "USD") -> Money:
    return Money.model_validate({"amount": amount, "currency": currency})


OPERATOR = ActorContext(actor_id="operator", credential_kind="clearinghouse_admin")


async def spend(
    engine: BuilderEngine,
    manager: FakeAllocationManager,
    tenant: str,
    allotted: Money,
    spent: str,
    actor: str = "actor",
) -> str:
    """Allot an actor of ``tenant`` an allocation from the tenant's grant, of which it has spent
    ``spent``. The fake provider reports that as Batteries does, on the allocation, whose id this
    answers. ``spent`` may be more than was allotted: Batteries charges after it authorizes."""
    request = AllotRequest(
        operation_ref=f"allot:{tenant}:{actor}",
        tenant_id=tenant,
        actor_id=f"{actor}-of-{tenant}",
        amount=allotted,
    )
    allocation = await engine.payments.allot(OPERATOR, request)
    assert allocation.allocation_id is not None
    manager.allocations[allocation.allocation_id].spent = Decimal(spent)
    return allocation.allocation_id


async def test_the_report_states_the_escrow_the_unspent_grants_and_the_signer(
    make_engine: EngineFactory, manager: FakeAllocationManager, admin: ActorContext
) -> None:
    manager.escrow = [ACCOUNT]
    engine = await make_engine()
    assert engine.payments.wallet is not None

    report = await engine.payments.wallet.report(admin)

    assert report.escrow == (ACCOUNT,)  # as the provider reports it: nothing summed, nothing converted
    assert report.signer == SignerReadiness(ready=True)
    # the fixture's one grant, "default": 1000 USD, none of it spent
    assert report.unspent_grants == (UnspentGrants(unspent=money("1000"), grants=1),)


async def test_unspent_is_what_is_not_allotted_and_what_is_allotted_and_not_spent_for_each_currency(
    make_engine: EngineFactory, manager: FakeAllocationManager, admin: ActorContext
) -> None:
    """Three USD grants and one ETH grant. What is allotted and not spent counts, and so does what is
    not allotted: both are still the tenant's to spend. A grant with no allocation counts whole. The
    currencies are never added together."""
    manager.add_grant("acme", 100)  # USD: 60 allotted, 30 of it spent
    manager.add_grant("idle", 7)  # USD: nothing allotted
    manager.add_grant("eth-tenant", "1.5", currency="ETH")  # ETH: 1 allotted, 0.5 of it spent
    engine = await make_engine(default_currency="USD")
    await spend(engine, manager, "acme", money("60"), spent="30")
    eth_engine = await make_engine(default_currency="ETH")
    await spend(eth_engine, manager, "eth-tenant", money("1", "ETH"), spent="0.5")
    assert engine.payments.wallet is not None

    report = await engine.payments.wallet.report(admin)

    assert report.unspent_grants == (
        UnspentGrants(unspent=money("1.0", "ETH"), grants=1),  # 0.5 not allotted, and 1 - 0.5
        # default 1000 not allotted; acme 40 not allotted, and 60 - 30; idle 7 not allotted
        UnspentGrants(unspent=money("1077"), grants=3),
    )


async def test_an_overdrawn_allocation_takes_nothing_off_what_its_grant_has_not_allotted(
    make_engine: EngineFactory, manager: FakeAllocationManager, admin: ActorContext
) -> None:
    """The provider charges after it authorizes and reserves nothing, so an allocation can spend more
    than it held while its grant still has a balance to allot (#181 review). That overdraft is charged
    already. What is not allotted can still be allotted and spent, so the wallet may have to pay for all
    of it: the grant's total less what was spent would report 30, which is too low."""
    engine = await make_engine()
    manager.add_grant("acme", 100)
    await spend(engine, manager, "acme", money("60"), spent="70")
    assert (await manager.read_grant("acme")).unallocated == money("40")
    assert engine.payments.wallet is not None

    report = await engine.payments.wallet.report(admin)

    # default 1000, and acme's 40 not allotted: its one allocation holds nothing more
    assert report.unspent_grants == (UnspentGrants(unspent=money("1040"), grants=2),)


async def test_an_overdrawn_allocation_takes_nothing_off_another_allocation_of_its_grant(
    make_engine: EngineFactory, manager: FakeAllocationManager, admin: ActorContext
) -> None:
    """Each allocation spends from what it holds and no other's. The grant's own sums cannot say this:
    60 allotted and 40 spent would read as 20 left in the allocations, where one still holds 30."""
    engine = await make_engine()
    manager.add_grant("acme", 100)
    await spend(engine, manager, "acme", money("30"), spent="40", actor="overdrawn")
    await spend(engine, manager, "acme", money("30"), spent="0", actor="unused")
    assert engine.payments.wallet is not None

    report = await engine.payments.wallet.report(admin)

    # default 1000; acme 40 not allotted, nothing left in the overdrawn allocation, 30 in the unused one
    assert report.unspent_grants == (UnspentGrants(unspent=money("1070"), grants=2),)


async def test_a_revoked_allocation_counts_once_as_what_it_returned_to_its_grant(
    make_engine: EngineFactory, manager: FakeAllocationManager, admin: ActorContext
) -> None:
    """A revoke returns what the allocation had not spent to the grant, to be allotted again, so it is
    in the unallocated balance and in no allocation. A charge that lands on the revoked allocation
    afterwards is spent already, and takes nothing off that balance."""
    engine = await make_engine()
    manager.add_grant("acme", 100)
    revoked = await spend(engine, manager, "acme", money("60"), spent="10")
    await engine.payments.revoke(OPERATOR, "acme", "actor-of-acme")
    assert manager.allocations[revoked].status == "revoked"
    assert engine.payments.wallet is not None

    # default 1000; acme 90 not allotted: the 40 never allotted and the 50 the revoke returned
    assert (await engine.payments.wallet.report(admin)).unspent_grants == (
        UnspentGrants(unspent=money("1090"), grants=2),
    )
    manager.allocations[revoked].spent = Decimal("15")  # a late charge, as Batteries records one
    assert (await engine.payments.wallet.report(admin)).unspent_grants == (
        UnspentGrants(unspent=money("1090"), grants=2),
    )


async def test_an_overdraft_in_one_currency_or_one_grant_takes_nothing_off_another(
    make_engine: EngineFactory, manager: FakeAllocationManager, admin: ActorContext
) -> None:
    engine = await make_engine()
    manager.add_grant("usd-tenant", 100)
    manager.add_grant("usd-other", 20)
    manager.add_grant("eth-tenant", "1.5", currency="ETH")
    await spend(engine, manager, "usd-tenant", money("60"), spent="70")
    await spend(engine, manager, "usd-other", money("20"), spent="5")
    eth_engine = await make_engine(default_currency="ETH")
    await spend(eth_engine, manager, "eth-tenant", money("1", "ETH"), spent="1.000000000000000001")
    assert engine.payments.wallet is not None

    report = await engine.payments.wallet.report(admin)

    assert report.unspent_grants == (
        UnspentGrants(unspent=money("0.5", "ETH"), grants=1),  # 0.5 not allotted; the rest overdrawn by a wei
        # default 1000; usd-tenant 40 not allotted; usd-other 20 - 5
        UnspentGrants(unspent=money("1055"), grants=3),
    )


async def test_a_paused_grant_counts_and_an_overdrawn_one_counts_as_zero(
    make_engine: EngineFactory, manager: FakeAllocationManager, admin: ActorContext
) -> None:
    """A paused grant can be resumed, so what it has left is still owed. The provider charges after it
    authorizes, so an allocation can spend more than it held: that grant has nothing left, and its
    overdraft does not hide what another grant has left."""
    manager.add_grant("paused", 50)
    manager.add_grant("overdrawn", 5)
    engine = await make_engine()
    await manager.set_grant_status("paused", "paused")
    await spend(engine, manager, "overdrawn", money("5"), spent="6")  # one more than its grant's total
    assert engine.payments.wallet is not None

    report = await engine.payments.wallet.report(admin)

    # default 1000, paused 50, overdrawn 0: nothing left to allot, and its one allocation holds nothing
    assert report.unspent_grants == (UnspentGrants(unspent=money("1050"), grants=3),)


def test_a_grant_stated_below_zero_never_hides_what_another_has_left() -> None:
    """The packaged providers state no balance below zero. A supplied ``AllocationManager`` that does
    (a grant whose top-up it has not read yet) has that balance counted as zero: it takes nothing off
    another grant, and nothing off what the same grant's allocations still hold, which is still payable.
    A figure too low is the unsafe one."""

    def stated(tenant: str, unallocated: str, allocated_unspent: str) -> TenantGrant:
        return TenantGrant(
            tenant_id=tenant,
            name=tenant,
            status="active",
            total=money("10"),
            unallocated=money(unallocated),
            spent=money("0"),
            allocated_unspent=money(allocated_unspent),
        )

    grants = [stated("behind", "-5", "2"), stated("whole", "4", "6")]
    assert unspent_grants(grants) == (UnspentGrants(unspent=money("12"), grants=2),)
    assert unspent_grants([stated("below", "3", "-1")]) == (UnspentGrants(unspent=money("3"), grants=1),)


async def test_a_provider_with_no_grant_and_no_escrow_reports_none(
    make_engine: EngineFactory, admin: ActorContext
) -> None:
    """A Batteries whose on-chain listener is off reports no escrow account. That is an empty list, never
    a deposit of zero."""
    engine = await make_engine(supply=EngineSupply(grants_manager=FakeAllocationManager()))
    assert engine.payments.wallet is not None
    report = await engine.payments.wallet.report(admin)
    assert (report.escrow, report.unspent_grants) == ((), ())


async def test_a_signer_that_does_not_answer_is_reported_and_is_no_error(
    make_engine: EngineFactory, transport: ScriptedStreamTransport, admin: ActorContext
) -> None:
    transport.signer = SignerReadiness(ready=False, reason="unavailable")
    engine = await make_engine()
    assert engine.payments.wallet is not None
    report = await engine.payments.wallet.report(admin)
    assert report.signer == SignerReadiness(ready=False, reason="unavailable")


@pytest.mark.parametrize("failing", ["escrow_accounts", "grant_balances"])
async def test_a_provider_that_fails_a_read_fails_the_report(
    make_engine: EngineFactory, manager: FakeAllocationManager, admin: ActorContext, failing: str
) -> None:
    """No part is served as if the rest were known: a deposit beside no total would read as "enough"."""
    engine = await make_engine()
    manager.fail_once(failing, ProviderUnavailable("Batteries unreachable"))
    assert engine.payments.wallet is not None
    with pytest.raises(ProviderUnavailable):
        await engine.payments.wallet.report(admin)


@pytest.mark.parametrize("kind", ["tenant_admin", "auditor", "actor", "service"])
async def test_only_a_clearinghouse_admin_reads_the_wallet(
    make_engine: EngineFactory, manager: FakeAllocationManager, transport: ScriptedStreamTransport, kind: str
) -> None:
    """It is every tenant's money and the wallet's. Refused before the provider or the signer is asked."""
    engine = await make_engine()
    asked: list[str] = []

    async def signer_readiness() -> SignerReadiness:
        asked.append("signer")
        return SignerReadiness(ready=True)

    transport.signer_readiness = signer_readiness  # type: ignore[method-assign]
    caller = ActorContext.model_validate({"actor_id": "someone", "credential_kind": kind})
    assert engine.payments.wallet is not None
    with pytest.raises(AccessDenied):
        await engine.payments.wallet.report(caller)
    assert not manager.calls and not asked


WALLET = "/v1/clearinghouse/wallet"


async def test_the_wallet_route_answers_a_clearinghouse_admin_with_the_report(
    make_engine: EngineFactory, manager: FakeAllocationManager, transport: ScriptedStreamTransport
) -> None:
    manager.escrow = [ACCOUNT]
    manager.add_grant("eth-tenant", "1.5", currency="ETH")
    transport.signer = SignerReadiness(ready=False, reason="no_identity")
    engine = await make_engine()
    assert engine.keys is not None
    admin = {"Authorization": f"Bearer {(await engine.keys.bootstrap_admin()).token}"}
    app = create_app(engine, manage_lifecycle=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://engine") as client:
        answer = await client.get(WALLET, headers=admin)
        assert (await client.get(WALLET)).status_code == 401
    assert answer.status_code == 200, answer.text
    # every field of the answer: amounts as decimal strings, each in its own currency
    assert answer.json() == {
        "escrow": [
            {
                "chain_id": "42161",
                "contract_address": "0x" + "c0" * 20,
                "sender": "0x" + "5e" * 20,
                "deposit": {"amount": "1.5", "currency": "ETH"},
                "reserve": {"amount": "0.25", "currency": "ETH"},
                "confirmed_through_block": 412,
            }
        ],
        "unspent_grants": [
            {"unspent": {"amount": "1.5", "currency": "ETH"}, "grants": 1},
            {"unspent": {"amount": "1000", "currency": "USD"}, "grants": 1},
        ],
        "signer": {"ready": False, "reason": "no_identity"},
    }


class NoEscrow(FakeAllocationManager):
    """A supplied grants manager that reports no escrow: the ``EscrowSource`` part is not there."""

    escrow_accounts = None  # type: ignore[assignment]


class NoProbe(ScriptedStreamTransport):
    """A supplied transport that cannot ask the signer: the ``SignerProbe`` part is not there."""

    signer_readiness = None  # type: ignore[assignment]


@pytest.mark.parametrize(
    "supply",
    [
        EngineSupply(grants_manager=NoEscrow()),
        EngineSupply(transport=NoProbe()),
        EngineSupply(grants_manager=None),  # the tenant desk is off
    ],
    ids=["no escrow source", "no signer probe", "no grants manager"],
)
async def test_a_supplied_part_that_cannot_report_means_no_wallet_route(
    make_engine: EngineFactory, supply: EngineSupply
) -> None:
    """As a usage source that reads no tenant's usage means no usage route (#166): nothing is served
    half known."""
    engine = await make_engine(supply=supply)
    assert engine.payments.wallet is None
    assert engine.keys is not None
    admin = {"Authorization": f"Bearer {(await engine.keys.bootstrap_admin()).token}"}
    app = create_app(engine, manage_lifecycle=False)
    assert WALLET not in app.openapi()["paths"]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://engine") as client:
        assert (await client.get(WALLET, headers=admin)).status_code == 404


async def test_the_packaged_parts_report_the_wallet() -> None:
    """The Batteries client and the SDK transport are what a deployment runs with: both have their part,
    so a deployment with a grants credential serves the route."""
    assert isinstance(BatteriesClient("https://batteries.test", "t"), EscrowSource)
    assert isinstance(SdkRunnerTransport("https://signer.test"), SignerProbe)
    assert isinstance(FakeAllocationManager(), EscrowSource)
    assert isinstance(ScriptedStreamTransport(), SignerProbe)
