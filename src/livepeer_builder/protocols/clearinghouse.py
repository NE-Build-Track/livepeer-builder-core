"""What the clearinghouse half needs beyond the five payment ports (#166).

Each is ``runtime_checkable`` and optional: the packaged parts implement them, the engine asks with
``isinstance``, and a deployment whose supplied part has none runs without what it serves, as documented
on each. They are re-exported from ``livepeer_builder.protocols``.
"""

from datetime import datetime
from typing import Protocol, runtime_checkable

from livepeer_builder.contracts import ActorAllocation


@runtime_checkable
class PaymentKeyRepo(Protocol):
    """Optional for a store: where a clearinghouse keeps the payment keys it issues (D87, #166). A
    payment key is what an enterprise's engine presents when it asks for a payment for one actor. Only
    the key's SHA-256 hash is kept, never the key, one for each actor, bound to the allocation it was
    issued for. The packaged SQL store has it.

    Without it the engine issues no payment key: allotment and rotation answer with ``payment_key``
    ``None``, and no payment key is recognized."""

    async def put_payment_key(
        self,
        tenant_id: str,
        actor_id: str,
        allocation_id: str,
        token_hash: str,
        created_at: datetime,
        *,
        only_if_absent: bool = False,
    ) -> bool:
        """Keep ``token_hash`` as the actor's payment key, in place of any it had: the old one stops at
        once. With ``only_if_absent``, only where the actor has no payment key for ``allocation_id`` (one
        issued for an earlier allocation is replaced). ``True`` when the hash was kept; ``False``, with
        nothing written, when that allocation already had a key."""
        ...

    async def delete_payment_key(self, tenant_id: str, actor_id: str, allocation_id: str) -> None:
        """Remove the actor's payment key when it was issued for ``allocation_id``; no error when not."""
        ...

    async def actor_by_payment_key(self, token_hash: str) -> ActorAllocation | None:
        """The actor whose payment key has this hash, while it still holds the allocation the key was
        issued for and that allocation is not revoked; otherwise ``None``."""
        ...
