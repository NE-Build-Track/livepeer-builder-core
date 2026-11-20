"""The payment keys a clearinghouse issues: one for each actor, shown once (D87, #166).

A payment key (``lpp_``) is what an enterprise's engine presents when it asks the clearinghouse to sign a
payment for one actor. It may ask for that actor's payments and nothing else: it is no access key, and
nothing but the signer's two paths reads one (``http/signing.py``). The clearinghouse swaps it for the
actor's Batteries key, which never leaves it (``payment_keys.py`` keeps that one).

Only the key's hash is kept (``PaymentKeyRepo``), as for an access key, so the token is in the answer of
the command that issued the key and nowhere else. The key is bound to the allocation it was issued for:

- allotting an allocation issues the actor's first key, unless that allocation already has one;
- rotating the payment key replaces it, and the old one stops in the statement that stores the new one;
- revoking the allocation removes it, and a revoked allocation's key finds nobody in any case.

Every change is logged (``payments.payment_key_issued``, ``payments.payment_key_replaced``,
``payments.payment_key_stopped``), never with the token or its hash.
"""

import logging
import secrets

from livepeer_builder.contracts import ActorAllocation, IssuedPaymentKey, KeyedAllocation
from livepeer_builder.protocols import Clock, PaymentKeyRepo
from livepeer_builder.utils import token_hash

_LOG = logging.getLogger(__name__)
PAYMENT_KEY_PREFIX = "lpp_"  # a public prefix, not a secret: an access key is lpk_, a Batteries key lpg_


class IssuedPaymentKeys:
    """``store`` is the engine's store. One that is no ``PaymentKeyRepo`` keeps no payment keys: nothing
    is issued, and nothing is recognized."""

    def __init__(self, store: object, clock: Clock) -> None:
        self._repo = store if isinstance(store, PaymentKeyRepo) else None
        self._clock = clock

    async def first(self, actor: ActorAllocation) -> list[str]:
        """The actor's first payment key for the allocation it holds, as a one-item list; empty when
        that allocation has a key already (an earlier run of the command issued it, and its token is
        never shown again) or the store keeps none."""
        return await self._issue(actor, "payments.payment_key_issued", only_if_absent=True)

    async def replaced(self, actor: ActorAllocation) -> list[str]:
        """A new payment key in place of the actor's old one, which stops at once; empty when the store
        keeps none."""
        return await self._issue(actor, "payments.payment_key_replaced", only_if_absent=False)

    async def stopped(self, actor: ActorAllocation) -> None:
        """The allocation is revoked: its payment key is removed."""
        if self._repo is None:
            return
        await self._repo.delete_payment_key(actor.tenant_id, actor.actor_id, actor.allocation_id)
        _audit("payments.payment_key_stopped", actor)

    async def actor_of(self, token: str) -> ActorAllocation | None:
        """The actor a payment key was issued for, while the key is live; otherwise ``None``."""
        if self._repo is None or not token.startswith(PAYMENT_KEY_PREFIX):
            return None
        return await self._repo.actor_by_payment_key(token_hash(token))

    def shown(self, actor: ActorAllocation, minted: list[str], *, replayed: bool) -> KeyedAllocation:
        """The command's answer: the allocation, with the token when this call issued the key. A repeat
        answered from the ledger shows none, whatever an earlier run issued."""
        key: IssuedPaymentKey | None = None
        if self._repo is not None:
            token = minted[0] if minted and not replayed else None
            key = IssuedPaymentKey(token=token, replayed=token is None)
        return KeyedAllocation.model_validate(actor.model_dump() | {"payment_key": key})

    async def _issue(self, actor: ActorAllocation, event: str, *, only_if_absent: bool) -> list[str]:
        if self._repo is None:
            return []
        token = PAYMENT_KEY_PREFIX + secrets.token_urlsafe(32)
        kept = await self._repo.put_payment_key(
            actor.tenant_id,
            actor.actor_id,
            actor.allocation_id,
            token_hash(token),
            self._clock.now(),
            only_if_absent=only_if_absent,
        )
        if not kept:
            return []
        _audit(event, actor)
        return [token]


def _audit(event: str, actor: ActorAllocation) -> None:
    """One payment key change, with whose key it is. Never the token or its hash."""
    _LOG.info(
        event.replace("payments.payment_key_", "payment key "),
        extra={
            "event": event,
            "tenant_id": actor.tenant_id,
            "actor_id": actor.actor_id,
            "allocation_id": actor.allocation_id,
        },
    )
