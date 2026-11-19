"""One actor's allocation, read with its amounts (#168).

``GET .../actors/{actor_id}/allocation`` answers the allocation's record and, as ``allowance``, what the
payment provider reports for it now: allotted, spent, available and the provider's status. It is the
read ``GET /v1/allowance`` makes, through the same ``AllowanceReader`` member, so the two routes cannot
mean different things by an amount. An enterprise's engine reads an actor's allowance from this route,
and so needs no work route of its clearinghouse.

The record is the engine's own and never depended on the provider. It still does not: when the provider
cannot be read, the amounts are unknown (``None``, never zero), the failure is logged with the provider's
words, and the record is answered.
"""

import logging
from collections.abc import Awaitable, Callable

from livepeer_builder.contracts import ActorAllocation, AllocationRead, Allowance
from livepeer_builder.errors import ProviderError, ProviderUnavailable
from livepeer_builder.utils import BODY_LIMIT

_LOG = logging.getLogger(__name__)


async def with_allowance(
    actor: ActorAllocation, read_allowance: Callable[[str], Awaitable[Allowance]]
) -> AllocationRead:
    """``actor``'s record with the allowance of the allocation it holds, read with ``read_allowance``."""
    allowance: Allowance | None = None
    try:
        allowance = await read_allowance(actor.allocation_id)
    except (ProviderError, ProviderUnavailable) as error:
        _LOG.warning(
            "allocation read without its allowance",
            extra={
                "event": "payments.allocation_allowance_unread",
                "tenant_id": actor.tenant_id,
                "actor_id": actor.actor_id,
                "allocation_id": actor.allocation_id,
                "provider_status": error.status_code if isinstance(error, ProviderError) else None,
                "detail": str(error)[:BODY_LIMIT],
            },
        )
    return AllocationRead.model_validate(actor.model_dump() | {"allowance": allowance})
