"""Grants at the payment provider: the idempotency keys the engine sends Batteries, the tag that finds
a grant the engine created (D57), the provider call that allots an allocation from a grant, and the
allocation manager a command about one actor is sent to.

A grant is a tenant's prepaid budget, and its id is the tenant's id (D83). ``tenants.py`` holds what an
admin does to a tenant; ``payments.py`` allots and tops up one actor's allocation from the grant.
"""

import base64
import hashlib

from livepeer_builder.contracts import AllotRequest, GrantRecord, NewAllocationKey
from livepeer_builder.errors import NotFound, ProviderError
from livepeer_builder.protocols import AllocationManager, AllocationsByActor, ReadyBeforeCommands
from livepeer_builder.services.ledger import RunningOperation
from livepeer_builder.utils import CursorTrail

GRANT_TAG = "lpb_operation"  # the metadata key that tags a grant the engine created (D57, Q5)


def idempotency_key(engine_id: str, operation: str, step: str) -> str:
    """base64url(sha256(engine_id:operation:step)): 43 chars, inside Batteries' 256-char limit."""
    digest = hashlib.sha256(f"{engine_id}:{operation}:{step}".encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


async def manager_for_command(
    manager: AllocationManager, tenant_id: str, actor_id: str, operation_ref: str | None = None
) -> AllocationManager:
    """The manager a command about one actor's allocation is sent to, made ready for it.

    Awaited before the command's record is begun. A manager that must learn something of its provider
    first learns it here (``ReadyBeforeCommands``, #165), outside the ledger's budget, which is sized for
    the command's own calls: its error refuses the command with no record and its reference free. A
    provider that names an actor and not an allocation id, as a payment clearinghouse does, is then told
    which (``AllocationsByActor``, #168), and an allotment's ``operation_ref`` with it. Batteries' client
    has neither, and is returned as it is."""
    if isinstance(manager, ReadyBeforeCommands):
        await manager.ready_for_commands()
    if isinstance(manager, AllocationsByActor):
        return manager.for_actor(tenant_id, actor_id, operation_ref)
    return manager


async def allotted_from_grant(
    manager: AllocationManager, request: AllotRequest, sent_as: str
) -> NewAllocationKey:
    """The allocation and its key, allotted from the tenant's grant: one id (D83). The provider's
    refusal is passed on with its reason (an allocation larger than the grant's unallocated balance
    is its 409); a grant it does not hold is said as the tenant that was never onboarded."""
    name = request.label or f"{request.tenant_id}/{request.actor_id}"
    try:
        return await manager.create_key_for_grant(request.tenant_id, name, request.amount, sent_as)
    except ProviderError as refused:
        if refused.status_code != 404:
            raise
        raise NotFound(
            f"tenant {request.tenant_id!r} has no grant at the payment provider: a tenant's id is its "
            "grant's id, and a clearinghouse admin onboards a tenant before anything is allotted to it"
        ) from refused


async def tagged_grant(manager: AllocationManager, run: RunningOperation, tag: str) -> GrantRecord | None:
    """The grant an earlier run created, found by its tag, page by page from where the last resume
    stopped: the cursor is recorded after each page, so a resume that runs out of its budget in a slow
    catalog leaves the next one less to read. Pages already read lacked the tag, and a grant created
    later is always on a later page (Batteries lists oldest first). A page that names a cursor the
    search was already at (the one it was asked with, or an earlier one) fails the search as an unknown
    outcome: following it would never end. The trail that finds such a cursor is recorded with the
    cursor, so a search that every resume cuts short after a page or two still finds it."""
    cursor = run.context.get("grant_list_cursor")
    trail = CursorTrail(cursor, run.context.get("grant_list_trail"))
    while True:
        page = await manager.grant_page(cursor)
        for grant in page.grants:
            if grant.metadata.get(GRANT_TAG) == tag:
                return grant
        if page.next_cursor is None:
            return None
        if trail.repeats(page.next_cursor):
            raise ProviderError(200, "the grant list's cursor did not advance")
        cursor = page.next_cursor
        await run.remember(grant_list_cursor=cursor, grant_list_trail=trail.keep())
