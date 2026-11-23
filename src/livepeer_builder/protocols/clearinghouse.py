"""What the clearinghouse half needs beyond the five payment ports (#166).

Each is ``runtime_checkable`` and optional: the packaged parts implement them, the engine asks with
``isinstance``, and a deployment whose supplied part has none runs without what it serves, as documented
on each. They are re-exported from ``livepeer_builder.protocols``.
"""

from typing import Protocol, runtime_checkable

from livepeer_builder.contracts import EscrowAccount, SignerReadiness, UsagePage


@runtime_checkable
class TenantUsageSource(Protocol):
    """Optional for a usage source: one tenant's payment records, and nothing of another's (#166). A
    tenant's id is its grant's id (D83), so the source filters by that grant. ``BatteriesUsageSource``
    has it, with Batteries' own ``grant_id`` filter.

    A page is the ``UsageSource`` port's: its rows, the rows the source set aside, and a cursor bound to
    the tenant, which another tenant's read refuses with ``CursorMismatch``. Without it the engine serves
    no tenant's usage: the usage route is absent."""

    async def tenant_page(self, tenant_id: str, cursor: str | None, limit: int = 1000) -> UsagePage: ...


@runtime_checkable
class DiscoveryDocumentSource(Protocol):
    """Optional for a discovery source: the livepeer/node ``/discovery`` document its last successful
    fetch read, as it was served (#166). ``HttpDiscoverySource`` has it. A clearinghouse serves that
    document to the engines of its tenants, which read it as they read node's own.

    ``None`` until a fetch has succeeded. A fetch that fails leaves the last good document in place, as
    the engine keeps its last good snapshot. Without it (a supplied source that reads no such document)
    the engine serves no runner list: the discovery route is absent."""

    def document(self) -> bytes | None: ...


@runtime_checkable
class EscrowSource(Protocol):
    """Optional for a grants manager: the deposit and reserve of the clearinghouse's wallet, as the
    payment provider's own on-chain listener has confirmed them (#167). ``BatteriesClient`` has it, from
    Batteries' escrow report, which the grants credential reads with ``escrow.read``.

    One account for each sender the provider follows, and none when it follows none (Batteries'
    listener is off unless the deployment turns it on). The engine reads no chain and holds no RPC
    address. Without it (a supplied grants manager that reports no escrow) the engine serves no wallet
    report: the wallet route is absent."""

    async def escrow_accounts(self) -> tuple[EscrowAccount, ...]: ...


@runtime_checkable
class SignerProbe(Protocol):
    """Optional for a runner transport: whether the signer it pays through answers its identity call now
    (#167). ``SdkRunnerTransport`` has it. The call is the one every payment begins with; it needs no
    credential and signs no payment. Without it (a supplied transport that cannot ask) the engine serves
    no wallet report: the wallet route is absent."""

    async def signer_readiness(self) -> SignerReadiness: ...
