"""What is read of an actor's allocation: its record, and its amounts at the payment provider (#168).

Three reads, split from ``payments.py``, which decides who may read and which allocation is meant:

- ``with_allowance``: ``GET .../actors/{actor_id}/allocation`` answers the allocation's record and, as
  ``allowance``, what the payment provider reports for it now: allotted, spent, available and the
  provider's status. It is the read ``GET /v1/allowance`` makes, through the same ``AllowanceReader``
  member, so the two routes cannot mean different things by an amount. An enterprise's engine reads an
  actor's allowance from this route, and so needs no work route of its clearinghouse. The record is the
  engine's own and never depended on the provider. It still does not: when the provider cannot be read,
  the amounts are unknown (``None``, never zero), the failure is logged with the provider's words, and
  the record is answered.
- ``for_a_builder``: the allowance a builder reads (``GET /v1/allowance``). The provider's own words go
  to the log, and the builder reads the engine's (D58).
- ``known``: one row of an admin's actor list (D46, D48).
"""

import logging
from collections.abc import Awaitable, Callable

from livepeer_builder.contracts import ActorAllocation, AllocationRead, Allowance, KnownActor, SeenActor
from livepeer_builder.errors import ProviderError, ProviderUnavailable
from livepeer_builder.utils import BODY_LIMIT

_LOG = logging.getLogger(__name__)
_UNREADABLE = "the allowance could not be read"  # what a builder reads when the provider fails (D58)


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


async def for_a_builder(
    read_allowance: Callable[[str], Awaitable[Allowance]], allocation_id: str
) -> Allowance:
    """The allowance of ``allocation_id`` as a builder reads it: a provider failure keeps its kind and its
    status, and its words go to the log."""
    try:
        return await read_allowance(allocation_id)
    except (ProviderError, ProviderUnavailable) as error:  # a builder's read: provider words to the log
        status = error.status_code if isinstance(error, ProviderError) else None
        extra = {"provider_status": status, "detail": str(error)[:BODY_LIMIT]}
        _LOG.warning("allowance read failed", extra={"event": "payments.allowance_failed", **extra})
        if status is None:  # httpx's text can name the provider's host (D58)
            raise ProviderUnavailable(f"{_UNREADABLE}: the provider is unavailable") from error
        raise ProviderError(status, _UNREADABLE) from error


def known(key: tuple[str, str], allocation: ActorAllocation | None, seen: SeenActor | None) -> KnownActor:
    """One row of the actor list: the allocation an admin allotted, the person an identity provider
    signed in, or both."""
    name = seen.name if seen is not None and seen.name else None
    return KnownActor(
        tenant_id=key[0],
        actor_id=key[1],
        label=(allocation.label if allocation is not None else "") or name or "",
        name=name if seen is not None else None,
        allocation=allocation,
        first_seen_at=seen.first_seen_at if seen is not None else None,
        last_seen_at=seen.last_seen_at if seen is not None else None,
    )
