"""Grants: the idempotency keys the engine sends Batteries, and the grant desk (D57).

Split from ``payments.py`` by responsibility: grants fund every application and are an optional
capability, while ``PaymentsService`` provisions and funds one actor's allocation.
"""

import base64
import hashlib

from pydantic import TypeAdapter

from livepeer_builder.contracts import ActorContext, Currency, GrantRecord, Money, Operation
from livepeer_builder.errors import InvalidRequest
from livepeer_builder.protocols import AccessPolicy, AllocationManager
from livepeer_builder.services.ledger import OperationLedger, RunningOperation

_GRANT = TypeAdapter(GrantRecord)
GRANT_TAG = "lpb_operation"  # the metadata key that tags a grant the engine created (D57, Q5)


def idempotency_key(engine_id: str, operation: str, step: str) -> str:
    """base64url(sha256(engine_id:operation:step)): 43 chars, inside Batteries' 256-char limit."""
    digest = hashlib.sha256(f"{engine_id}:{operation}:{step}".encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


class GrantDesk:
    """Listing and creating grants: an optional capability (decision 7 in the DI refactor plan).

    Present only when a grants manager exists (``LPB_BATTERIES_OPERATOR_TOKEN``, or one supplied).
    The engine's own token can fund grants but not list or create them, so without the operator
    credential the desk is absent rather than failing at first use.
    """

    def __init__(
        self,
        manager: AllocationManager,
        policy: AccessPolicy,
        currency: Currency,
        ledger: OperationLedger,
        engine_id: str,
    ) -> None:
        self._manager = manager
        self._engine_id = engine_id
        self._policy = policy
        self._currency = currency
        self._ledger = ledger

    async def list_grants(self, admin: ActorContext) -> list[GrantRecord]:
        # The provisioning catalog: grants fund every application and carry none, so an auditor bound to
        # one application never reads them (John, #53 review).
        self._policy.check(admin, Operation.ADMIN_ALLOCATIONS)
        return await self._manager.list_grants()

    async def create_grant(
        self, admin: ActorContext, name: str, amount: Money, *, operation_ref: str
    ) -> GrantRecord:
        """Create a grant, on the ledger (D57): a repeat of the reference gets the first grant back."""
        self._policy.check(admin, Operation.ADMIN_ALLOCATIONS)
        if not name.strip():
            raise InvalidRequest("a grant name is required")
        if amount.currency != self._currency or amount.amount <= 0:
            raise InvalidRequest(f"a grant needs a positive amount in {self._currency}")

        # Batteries has no idempotency key on grant creation (Q5): the tag finds an earlier run's grant
        tag = idempotency_key(
            self._engine_id, f"create-grant:{admin.application_id}:{admin.actor_id}:{operation_ref}", "grant"
        )

        async def steps(run: RunningOperation) -> GrantRecord:
            if run.resumed:
                found = await self._tagged_grant(run, tag)
                if found is not None:
                    return found
            return await self._manager.create_grant(name.strip(), amount, {GRANT_TAG: tag})

        request = {"name": name.strip(), "amount": amount}
        return await self._ledger.run(admin, "create_grant", operation_ref, request, steps, _GRANT)

    async def _tagged_grant(self, run: RunningOperation, tag: str) -> GrantRecord | None:
        """The grant an earlier run created, found by its tag, page by page from where the last resume
        stopped: the cursor is recorded after each page, so a resume that runs out of its budget in a slow
        catalog leaves the next one less to read. Pages already read lacked the tag, and a grant created
        later is always on a later page (Batteries lists oldest first)."""
        cursor = run.context.get("grant_list_cursor")
        while True:
            page = await self._manager.grant_page(cursor)
            for grant in page.grants:
                if grant.metadata.get(GRANT_TAG) == tag:
                    return grant
            if page.next_cursor is None:
                return None
            cursor = page.next_cursor
            await run.remember(grant_list_cursor=cursor)
