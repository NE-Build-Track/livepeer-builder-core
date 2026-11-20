"""What happens to a payment key the provider returned: its secret is stored once, the actor is pointed at
it, and the key it replaces is retired (docs/contracts/payments.md).

Split from ``payments.py`` by responsibility: ``PaymentsService`` decides which provider operation a
command sends and under which idempotency key; ``PaymentKeys`` keeps the key that comes back, and says
what a command does when another command wrote the actor first (#115, C4).
"""

from livepeer_builder.contracts import ActorAllocation, NewAllocationKey, SecretRef
from livepeer_builder.errors import AlreadyAllotted, InvalidRequest, NotFound
from livepeer_builder.protocols import AllocationManager, SecretStore, WritableSecretStore
from livepeer_builder.services.actor_writes import ActorWrites
from livepeer_builder.services.ledger import RunningOperation, TakenOver

BEGAN_ON = "allocation_id"  # in a rotation's record: the allocation the actor held when it began
_LOST_TO = "lost_to_api_key_id"  # in a rotation's record: the key another rotation gave the actor first


class PaymentKeys:
    def __init__(self, secrets: SecretStore, writes: ActorWrites) -> None:
        self._secrets = secrets
        self._writes = writes

    async def stored(
        self, run: RunningOperation, tenant_id: str, actor_id: str, key: NewAllocationKey, sent_as: str
    ) -> SecretRef:
        """The new key's secret, stored once per key: its reference goes into the ledger's context, with the
        key it belongs to and the provider operation that returned it (``sent_as``, its idempotency key),
        before the actor points at it, so a resume reuses that copy instead of leaving it orphaned. A resume
        that got another key (a second allotment after a revoke) stores that key's secret."""
        if "new_secret_ref" in run.context and run.context.get("new_secret_key_id") == key.api_key_id:
            return SecretRef(ref=run.context["new_secret_ref"])
        ref = await self._secrets.put(_secret_name(tenant_id, actor_id), key.secret.get_secret_value())
        try:
            await run.remember(
                new_secret_ref=ref.ref, new_secret_key_id=key.api_key_id, new_secret_sent_as=sent_as
            )
        except TakenOver:  # a resume took over: it stores its own copy
            await self._secrets.delete(ref)
            raise
        return ref

    async def require_writable(
        self, run: RunningOperation, sent_as: str, holder: ActorAllocation | None = None
    ) -> None:
        """In the step that mints a key, just before the provider is called: a store that says it cannot
        keep the new key refuses the command (#112, C2). A store without the optional ``writable`` check
        is not asked. A run with nothing more to store is not refused (``_key_is_stored``): a repeat of a
        finished command never reaches this step, and a resume goes on to finish."""
        if self._key_is_stored(run, sent_as, holder):
            return
        if isinstance(self._secrets, WritableSecretStore) and not await self._secrets.writable():
            # only a fresh run knows that nothing was sent: a resumed one follows an unknown outcome
            sent = (
                "an earlier attempt may have reached the payment provider; repeat this request when the "
                "store can write"
                if run.resumed
                else "nothing was sent to the payment provider"
            )
            raise InvalidRequest(
                "the secret store cannot store a new payment key now (the built-in store needs "
                f"LPB_SECRET_KEY); {sent}"
            )

    @staticmethod
    def _key_is_stored(run: RunningOperation, sent_as: str, holder: ActorAllocation | None) -> bool:
        """Whether the key of the provider call about to be sent (``sent_as``, its idempotency key) is
        stored already. A record that notes the call its secret came from (``new_secret_sent_as``) answers
        by that note alone: a stored secret from another provider call excuses nothing, since a resume
        after a revoke or a second allotment, or under another ``LPB_ENGINE_ID``, sends a new one, whose key
        is not stored yet. Only a record without the note, written before it existed, is answered by the
        actor (``holder``, in a rotation): when it holds the key and secret the record notes, the provider
        call is still sent, replays that key, and leaves nothing to store."""
        ref = run.context.get("new_secret_ref")
        if ref is None:
            return False
        noted = run.context.get("new_secret_sent_as")
        if noted is not None:
            return noted == sent_as
        if holder is None:
            return False
        return (holder.api_key_id, holder.secret_ref.ref) == (run.context.get("new_secret_key_id"), ref)

    async def shared(self, actor: ActorAllocation, own: SecretRef) -> ActorAllocation:
        """The actor holding the key this allotment got. Another reference may have saved it with its
        own copy of the secret; this command's copy is then dropped."""
        if own != actor.secret_ref:
            await self._secrets.delete(own)
        return actor

    async def overtaken(
        self, actor_id: str, current: ActorAllocation | None, key: NewAllocationKey, ref: SecretRef
    ) -> ActorAllocation:
        """The answer of an allotment that did not write its row, because another command wrote the
        actor while it was at the provider: its row would undo a pause, a rotation or a revoke that
        finished meanwhile. The command that wrote first was an allotment that had read what this one
        read, so it sent the same actor-derived provider operation and got this same allocation and key:
        nothing this one minted is unrecorded, and only its own copy of the secret (``ref``) is extra.
        The answer comes from ``current``, the actor as stored now, by the rule a resumed run follows:
        the actor is this command's result while it holds the key this command got."""
        if current is not None and current.status != "revoked" and current.api_key_id == key.api_key_id:
            return await self.shared(current, ref)
        await self._secrets.delete(ref)  # the actor holds another key or is revoked: nothing points at it
        if current is not None and current.status != "revoked":  # rotated, or revoked and allotted again
            raise AlreadyAllotted(current)
        if current is not None and current.allocation_id == key.allocation_id:
            return (
                current  # allotted, then revoked: it stays revoked, and that is the answer, as for a top-up
            )
        raise NotFound(
            f"actor {actor_id!r} was revoked while this allotment was at the provider; it has no allocation"
        )

    def refuse_lost(self, run: RunningOperation, actor_id: str) -> None:
        """A rotation refused before, with everything of it revoked, gets the same answer again."""
        if _LOST_TO in run.context:
            raise _lost_rotation(actor_id, run.context[_LOST_TO])

    async def began_on(self, run: RunningOperation, actor: ActorAllocation) -> ActorAllocation:
        """``actor``, when it still holds the allocation the rotation began on. An actor revoked and
        allotted again holds another: the old key the record names is not that allocation's, so a
        rotation resumed there would mint a key on it and leave the key it had live. It is refused
        before any provider call, and a secret it stored for the revoked allocation's key is dropped. A
        record written before the allocation was noted is not checked."""
        began_on = run.context.get(BEGAN_ON, actor.allocation_id)
        if began_on == actor.allocation_id:
            return actor
        if "new_secret_ref" in run.context:
            await self._secrets.delete(SecretRef(ref=run.context["new_secret_ref"]))
        raise NotFound(
            f"this rotation began on allocation {began_on!r}, which actor {actor.actor_id!r} no longer "
            f"holds: it was revoked and allotted again (allocation {actor.allocation_id!r}). Rotate "
            "that allocation's key under a new reference"
        )

    async def rotated(
        self,
        run: RunningOperation,
        manager: AllocationManager,
        actor: ActorAllocation,
        key: NewAllocationKey,
        sent_as: str,
    ) -> ActorAllocation:
        """Store the rotation's key (``key``, which the provider replays on a resume), point ``actor`` at
        it and retire the key the rotation began from, which the record noted. The new key is written
        only over that old key: when another rotation gave the actor a key meanwhile, this one writes
        nothing, revokes its own key and is refused, ``InvalidRequest``."""
        previous_key = run.context["previous_api_key_id"]
        if actor.api_key_id != key.api_key_id:  # not stored yet
            ref = await self.stored(run, actor.tenant_id, actor.actor_id, key, sent_as)
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
        self, actor: ActorAllocation, api_key_id: str, ref: SecretRef, previous_key: str
    ) -> ActorAllocation:
        """The actor after the rotation's key write: only those columns are written, so a status another
        command set meanwhile stays (#115, C4). An actor revoked meanwhile stays revoked: the new key is on
        a revoked allocation, so its secret is dropped. The write applies only over ``previous_key``, the
        key the rotation began from: an actor that holds another key got it from a rotation that finished
        first, and is returned as stored, holding that key, with nothing written."""
        current = await self._writes.key(actor, api_key_id, ref, only_from=previous_key)
        if current is None:
            await self._secrets.delete(ref)
            raise NotFound(f"actor {actor.actor_id!r} has no allocation")
        return current


def _lost_rotation(actor_id: str, held: str) -> InvalidRequest:
    """The refusal of a rotation that another rotation of the same actor overtook (#115, C4)."""
    return InvalidRequest(
        f"another key rotation of actor {actor_id!r} finished after this one began: the actor holds key "
        f"{held!r} and this rotation's key is revoked. Rotate again under a new reference to replace it"
    )


def _secret_name(tenant_id: str, actor_id: str) -> str:
    return f"lpg/{tenant_id}/{actor_id}"
