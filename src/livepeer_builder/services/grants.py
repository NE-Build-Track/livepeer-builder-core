"""Grants at the payment provider: the idempotency keys the engine sends Batteries, and the tag that
finds a grant the engine created (D57).

A grant is a tenant's prepaid budget, and its id is the tenant's id (D83). ``tenants.py`` holds what an
admin does to a tenant; ``payments.py`` allots and tops up one actor's allocation from the grant.
"""

import base64
import hashlib

from livepeer_builder.contracts import GrantRecord
from livepeer_builder.errors import ProviderError
from livepeer_builder.protocols import AllocationManager
from livepeer_builder.services.ledger import RunningOperation

GRANT_TAG = "lpb_operation"  # the metadata key that tags a grant the engine created (D57, Q5)


def idempotency_key(engine_id: str, operation: str, step: str) -> str:
    """base64url(sha256(engine_id:operation:step)): 43 chars, inside Batteries' 256-char limit."""
    digest = hashlib.sha256(f"{engine_id}:{operation}:{step}".encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


async def tagged_grant(manager: AllocationManager, run: RunningOperation, tag: str) -> GrantRecord | None:
    """The grant an earlier run created, found by its tag, page by page from where the last resume
    stopped: the cursor is recorded after each page, so a resume that runs out of its budget in a slow
    catalog leaves the next one less to read. Pages already read lacked the tag, and a grant created
    later is always on a later page (Batteries lists oldest first). A page that answers the cursor it
    was asked with fails the search as an unknown outcome: asking again would never end."""
    cursor = run.context.get("grant_list_cursor")
    while True:
        page = await manager.grant_page(cursor)
        for grant in page.grants:
            if grant.metadata.get(GRANT_TAG) == tag:
                return grant
        if page.next_cursor is None:
            return None
        if page.next_cursor == cursor:
            raise ProviderError(200, "the grant list's cursor did not advance")
        cursor = page.next_cursor
        await run.remember(grant_list_cursor=cursor)
