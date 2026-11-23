"""A tenant's payment records at the clearinghouse (#166): what an enterprise's engine reads in place of
the payment provider's usage feed.

A tenant's own admin or auditor reads its usage, and a clearinghouse admin any tenant's. The tenant is
named to the access policy before the provider is asked, so another tenant's id is refused there (403).
The rows are the provider's, filtered by the tenant's grant, whose id is the tenant's id (D83): the
engine keeps no second copy and decides no row itself (``TenantUsageSource``).
"""

from livepeer_builder.contracts import ActorContext, Operation, ResourceRef, UsagePage
from livepeer_builder.protocols import AccessPolicy, TenantUsageSource


class TenantUsage:
    def __init__(self, source: TenantUsageSource, policy: AccessPolicy) -> None:
        self._source = source
        self._policy = policy

    async def page(
        self, reader: ActorContext, tenant_id: str, cursor: str | None = None, limit: int = 1000
    ) -> UsagePage:
        """One page of the tenant's payment records, oldest first, from ``cursor`` (``None`` for the
        first). It is the ``UsageSource`` port's page: the rows, the rows the source set aside, and the
        cursor of the next page (``""`` on the last). A cursor of another tenant is ``CursorMismatch``."""
        self._policy.check(reader, Operation.ADMIN_READ, ResourceRef(kind="tenant", tenant_id=tenant_id))
        return await self._source.tenant_page(tenant_id, cursor, limit)
