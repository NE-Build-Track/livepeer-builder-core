"""The wallet report: what a clearinghouse admin watches the wallet by (D82, #167).

"Each clearinghouse has one wallet. Its deposit stays above the total of unspent grants, with a margin
for the ETH price. The clearinghouse admin watches it and funds it by hand." This module states the three
things that sentence needs, and decides nothing:

- the **deposit and reserve**, as the payment provider's own on-chain listener has confirmed them
  (``EscrowSource``). The engine reads no chain and holds no RPC address;
- the **total of unspent grants**, for each currency a grant is in;
- whether the **signer** answers its identity call (``SignerProbe``).

Each amount stays in its own currency. Nothing is converted between ETH and a grant's currency, no
margin is computed and nothing is refused by these figures: a person judges. Only a clearinghouse admin
reads the report (``admin.tenants``): it is every tenant's money and the wallet's.
"""

from decimal import Decimal

from livepeer_builder.contracts import (
    ActorContext,
    Currency,
    Money,
    Operation,
    TenantGrant,
    UnspentGrants,
    WalletReport,
)
from livepeer_builder.protocols import AccessPolicy, AllocationManager, EscrowSource, SignerProbe


class Wallet:
    def __init__(
        self, escrow: EscrowSource, grants: AllocationManager, signer: SignerProbe, policy: AccessPolicy
    ) -> None:
        self._escrow = escrow
        self._grants = grants
        self._signer = signer
        self._policy = policy

    async def report(self, admin: ActorContext) -> WalletReport:
        """The escrow, the unspent grants and the signer's readiness, each read now. They are three
        reads and not one moment: a payment or a top-up that lands between them is in one figure and
        not in another. A provider that fails one read fails the report: no part is served as if the
        rest were known. A signer that does not answer is an answer (``ready: false``), not an error."""
        self._policy.check(admin, Operation.ADMIN_TENANTS)  # before any read (D44)
        escrow = await self._escrow.escrow_accounts()
        unspent = unspent_grants(await self._grants.grant_balances())
        return WalletReport(
            escrow=escrow, unspent_grants=unspent, signer=await self._signer.signer_readiness()
        )


def unspent_grants(grants: list[TenantGrant]) -> tuple[UnspentGrants, ...]:
    """What the tenants may still spend, for each currency a grant is in, by currency.

    A grant's unspent amount is what is not yet allotted (``unallocated``) and what is allotted and not
    yet spent (``allocated_unspent``), both of which the wallet may still have to pay for. It is not the
    total less what was spent: the provider charges after it authorizes and reserves nothing, so an
    allocation can spend more than it held. That overdraft is charged already, and it takes nothing off
    what another allocation holds or off what the grant has left to allot. A figure too low is the unsafe
    one: the deposit is held above this total (D82). A grant never counts below zero, so it never hides
    another grant's unspent amount. Every grant counts, whatever its status: a paused grant can be
    resumed."""
    totals: dict[Currency, tuple[Decimal, int]] = {}
    for grant in grants:
        currency = grant.total.currency
        amount, count = totals.get(currency, (Decimal(0), 0))
        unspent = max(grant.unallocated.amount + grant.allocated_unspent.amount, Decimal(0))
        totals[currency] = (amount + unspent, count + 1)
    return tuple(
        UnspentGrants(unspent=Money(amount=amount, currency=currency), grants=count)
        for currency, (amount, count) in sorted(totals.items())
    )
