"""What happens to a payment key the provider returned: its secret is stored once, the actor is pointed at
it, and the key it replaces is retired (docs/contracts/payments.md).

Split from ``payments.py`` by responsibility: ``PaymentsService`` decides which provider operation a
command sends and under which idempotency key; ``PaymentKeys`` keeps the key that comes back, and says
what a command does when another command wrote the actor first (#115, C4).
"""

from livepeer_builder.contracts import NewAllocationKey, ProvisionedActor, SecretRef
from livepeer_builder.errors import InvalidRequest, NotFound
from livepeer_builder.protocols import AllocationManager, SecretStore
from livepeer_builder.services.actor_writes import ActorWrites
from livepeer_builder.services.ledger import RunningOperation, TakenOver

_LOST_TO = "lost_to_api_key_id"  # in a rotation's record: the key another rotation gave the actor first


class PaymentKeys:
    def __init__(self, secrets: SecretStore, writes: ActorWrites) -> None:
        self._secrets = secrets
        self._writes = writes

    async def stored(
        self, run: RunningOperation, application_id: str, actor_id: str, key: NewAllocationKey, sent_as: str
    ) -> SecretRef:
        """The new key's secret, stored once per key: its reference goes into the ledger's context, with the
        key it belongs to and the provider operation that returned it (``sent_as``, its idempotency key),
        before the actor points at it, so a resume reuses that copy instead of leaving it orphaned. A resume
        that got another key (a re-provision after a revoke) stores that key's secret."""
        if "new_secret_ref" in run.context and run.context.get("new_secret_key_id") == key.api_key_id:
            return SecretRef(ref=run.context["new_secret_ref"])
        ref = await self._secrets.put(_secret_name(application_id, actor_id), key.secret.get_secret_value())
        try:
            await run.remember(
                new_secret_ref=ref.ref, new_secret_key_id=key.api_key_id, new_secret_sent_as=sent_as
            )
        except TakenOver:  # a resume took over: it stores its own copy
            await self._secrets.delete(ref)
            raise
        return ref

    def refuse_lost(self, run: RunningOperation, actor_id: str) -> None:
        """A rotation refused before, with everything of it revoked, gets the same answer again."""
        if _LOST_TO in run.context:
            raise _lost_rotation(actor_id, run.context[_LOST_TO])

    async def rotated(
        self,
        run: RunningOperation,
        manager: AllocationManager,
        actor: ProvisionedActor,
        key: NewAllocationKey,
        sent_as: str,
    ) -> ProvisionedActor:
        """Store the rotation's key (``key``, which the provider replays on a resume), point ``actor`` at
        it and retire the key the rotation began from, which the record noted. The new key is written
        only over that old key: when another rotation gave the actor a key meanwhile, this one writes
        nothing, revokes its own key and is refused, ``InvalidRequest``."""
        previous_key = run.context["previous_api_key_id"]
        if actor.api_key_id != key.api_key_id:  # not stored yet
            ref = await self.stored(run, actor.application_id, actor.actor_id, key, sent_as)
            actor = await self._pointed_at(actor, key.api_key_id, ref, previous_key)
            if actor.api_key_id != key.api_key_id:
                # Another rotation's key: this one wrote nothing, so its own key must not stay live. The
                # revoke comes first: a key still live keeps its secret stored, and a revoke that fails
                # leaves the record open for a repeat, which gets the same key replayed and comes back here.
                await manager.revoke_key(key.api_key_id)
                await self._secrets.delete(ref)
        # The key this rotation began from is revoked in either case, unless the actor holds it: when this
        # rotation stored its key, failed here and was then replaced by a newer one, nothing else revokes it.
        if previous_key not in (key.api_key_id, actor.api_key_id):  # never the live key (B1)
            await manager.revoke_key(previous_key)  # repeat-safe upstream
        previous_secret = SecretRef(ref=run.context["previous_secret_ref"])
        if previous_secret != actor.secret_ref:
            await self._secrets.delete(previous_secret)
        if actor.api_key_id != key.api_key_id:
            # Noted, and never freed: under a free reference the provider would replay the revoked key.
            await run.remember(**{_LOST_TO: actor.api_key_id})
            raise _lost_rotation(actor.actor_id, actor.api_key_id)
        return actor

    async def _pointed_at(
        self, actor: ProvisionedActor, api_key_id: str, ref: SecretRef, previous_key: str
    ) -> ProvisionedActor:
        """The actor after the rotation's key write: only those columns are written, so a status another
        command set meanwhile stays (#115, C4). An actor revoked meanwhile stays revoked: the new key is on
        a revoked allocation, so its secret is dropped. The write applies only over ``previous_key``, the
        key the rotation began from: an actor that holds another key got it from a rotation that finished
        first, and is returned as stored, holding that key, with nothing written."""
        current = await self._writes.key(actor, api_key_id, ref, only_from=previous_key)
        if current is None:
            await self._secrets.delete(ref)
            raise NotFound(f"actor {actor.actor_id!r} is not provisioned")
        return current


def _lost_rotation(actor_id: str, held: str) -> InvalidRequest:
    """The refusal of a rotation that another rotation of the same actor overtook (#115, C4)."""
    return InvalidRequest(
        f"another key rotation of actor {actor_id!r} finished after this one began: the actor holds key "
        f"{held!r} and this rotation's key is revoked. Rotate again under a new reference to replace it"
    )


def _secret_name(application_id: str, actor_id: str) -> str:
    return f"lpg/{application_id}/{actor_id}"
