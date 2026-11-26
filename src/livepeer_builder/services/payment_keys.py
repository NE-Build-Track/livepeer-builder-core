"""What happens to a payment key the provider returned: its secret is stored once, the actor is pointed at
it, and the key it replaces is retired (docs/contracts/payments.md).

Split from ``payments.py`` by responsibility: ``PaymentsService`` decides which provider operation a
command sends and under which idempotency key; ``PaymentKeys`` keeps the key that comes back, and says
what a command does when another command wrote the actor first (#115, C4).

Custody (D85 point 1, D87 point 1). A secret is held by the configured ``SecretStore`` or, for a
delivering command, by the work engine, which gets it once in the command's answer. Every reference is
kept with its custody, in the actor row and in the ledger's context, and every delete follows it: a
``secret_store`` handle is deleted on the configured store that issued it, and a ``work_engine`` receipt
never reaches a store. Nothing is read from the text of a reference (#186 review).

Two kinds of command mint a key: the admin ones, which keep it in the configured store, and the delivering
ones. A record notes which kind began it, and only that kind resumes it (``PaymentKeys.resumes``).

Two kinds of provider (``OneKeyPerAllocation``). Batteries keeps every key live until it is revoked, so
a rotation revokes the old key and an overtaken one revokes its own. A provider that keeps one key for
an allocation, a payment clearinghouse, ended them itself, and its own order of two rotations decides
which key the engine keeps (``PaymentKeys.overtaken_rotation``).
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import SecretStr

from livepeer_builder.contracts import (
    CREDENTIAL_CUSTODIES,
    ActorAllocation,
    CredentialCustody,
    NewAllocationKey,
    OperationRecord,
    SecretRef,
)
from livepeer_builder.errors import AlreadyAllotted, InvalidRequest, NotFound, OperationConflict
from livepeer_builder.protocols import (
    AllocationManager,
    OneKeyPerAllocation,
    SecretStore,
    WritableSecretStore,
)
from livepeer_builder.services.actor_writes import ActorWrites
from livepeer_builder.services.allocation_delivery import DeliverySecrets
from livepeer_builder.services.grants import stored_id
from livepeer_builder.services.ledger import RunningOperation, TakenOver

BEGAN_ON = "allocation_id"  # in a rotation's record: the allocation the actor held when it began
PREVIOUS_CUSTODY = "previous_credential_custody"  # in a rotation's record: who held the key it began from
_NEW_CUSTODY = "new_secret_custody"  # in a record: who holds the secret ``new_secret_ref`` names
_LOST_TO = "lost_to_api_key_id"  # in a rotation's record: the key another rotation gave the actor first
# In a rotation's record, noted before it points the actor at its key over another rotation's ended one:
# that key's secret and who holds it, so a resume drops it (``PaymentKeys.overtaken_rotation``).
_SUPERSEDED = "superseded_secret_ref"
_SUPERSEDED_CUSTODY = "superseded_credential_custody"
# In a record, from the moment it begins: who the command that began it keeps the key with, which is the
# kind of command it is. ``secret_store``: ``allot`` or ``rotate_payment_key``. ``work_engine``: their
# delivering twins.
BEGUN_FOR = "begun_for"
_DELIVERING = {"allot": "allot_for_delivery", "rotate_payment_key": "rotate_for_delivery"}
_KEEPS = {"secret_store": "stores the key in the engine's secret store", "work_engine": "hands the key over"}


@dataclass(frozen=True)
class HeldSecret:
    """A stored secret's reference and who holds it: the two travel together."""

    ref: SecretRef
    custody: CredentialCustody


class PaymentKeys:
    """``secrets`` is the configured store. ``delivery``, set only for a delivering command, is that
    request's own buffer: a secret this object stores then goes there, and is ``work_engine``'s."""

    def __init__(
        self, secrets: SecretStore, writes: ActorWrites, *, delivery: DeliverySecrets | None = None
    ) -> None:
        self._retiring = secrets
        self._delivery = delivery
        self._writes = writes

    @property
    def custody(self) -> CredentialCustody:
        """Who holds a secret this object stores."""
        return "work_engine" if self._delivery is not None else "secret_store"

    def begins(self, context: Mapping[str, str] | None = None) -> dict[str, str]:
        """What a command's record notes as it begins, before any provider call: ``context``, and which
        kind of command this is. A run that died at any point has it."""
        return {**(context or {}), BEGUN_FOR: self.custody}

    def resumes(self, held: OperationRecord) -> None:
        """Refuse to resume a run the other kind of command began (Mike, 23 November 2026). Finished by
        the other kind, a delivering run's key would be handed to nobody, and an admin run's would stay
        in the store while the delivering caller is answered with none. The ledger asks before it takes
        the record over: nothing is sent or written, and the command that began the run still resumes it.

        A record written before the kind was noted is the kind its stored secret's custody implies. One
        that notes neither was begun by an admin command: there was no other kind then."""
        began = _custody(held.context, BEGUN_FOR if BEGUN_FOR in held.context else _NEW_CUSTODY)
        if began != self.custody:
            first, this = _command(held, began), _command(held, self.custody)
            raise OperationConflict(
                held,
                f"operation_ref {held.operation_ref!r} was begun by {first}, which {_KEEPS[began]}, and "
                f"has not finished. Only the command that began it resumes it: repeat {first}, not {this}",
            )

    async def stored(
        self, run: RunningOperation, tenant_id: str, actor_id: str, key: NewAllocationKey, sent_as: str
    ) -> HeldSecret:
        """The new key's secret, stored once per key: its reference goes into the ledger's context, with the
        key it belongs to, the provider operation that returned it (``sent_as``, its idempotency key) and
        its custody, before the actor points at it, so a resume reuses that copy instead of leaving it
        orphaned. A resume that got another key (a second allotment after a revoke) stores that key's
        secret.

        A resume is the kind of command that began the record (``resumes``), so the custody recorded with
        the reference is its own. A resume that delivers holds the secret the provider replayed under
        the recorded receipt, so its answer carries the key the first run never answered with.

        A key whose allocation id or key id the engine's records cannot keep is refused first
        (``stored_id``): no secret is stored, the record notes nothing and no actor is written."""
        stored_id(key.allocation_id, "an allocation id")
        stored_id(key.api_key_id, "a key id")
        if "new_secret_ref" in run.context and run.context.get("new_secret_key_id") == key.api_key_id:
            recorded = recorded_secret(run)
            self._redeliver(recorded, key)
            return recorded
        secret = key.secret.get_secret_value()
        if self._delivery is not None:
            ref = self._delivery.put(secret)
        else:
            ref = await self._retiring.put(_secret_name(tenant_id, actor_id), secret)
        held = HeldSecret(ref, self.custody)
        try:
            await run.remember(
                new_secret_ref=ref.ref,
                new_secret_key_id=key.api_key_id,
                new_secret_sent_as=sent_as,
                **{_NEW_CUSTODY: held.custody},
            )
        except TakenOver:  # a resume took over: it stores its own copy
            await self._drop(held)
            raise
        return held

    async def require_writable(
        self, run: RunningOperation, sent_as: str, holder: ActorAllocation | None = None
    ) -> None:
        """In the step that mints a key, just before the provider is called: a store that says it cannot
        keep the new key refuses the command (#112, C2). A store without the optional ``writable`` check
        is not asked, and neither is a delivering command's buffer, which keeps nothing. A run with
        nothing more to store is not refused (``_key_is_stored``): a repeat of a finished command never
        reaches this step, and a resume goes on to finish."""
        if self._key_is_stored(run, sent_as, holder) or self._delivery is not None:
            return
        if isinstance(self._retiring, WritableSecretStore) and not await self._retiring.writable():
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

    async def shared(
        self, actor: ActorAllocation, own: HeldSecret, key: NewAllocationKey | None = None
    ) -> ActorAllocation:
        """The actor holding the key this allotment got. Another reference may have saved it with its
        own copy of the secret; this command's copy is then dropped. With the actor's key in hand
        (``key``: the provider gave it to both references, or replayed it to a resume), a delivering
        command holds it under the actor's receipt, so its answer carries it too."""
        if own.ref != actor.secret_ref:
            await self._drop(own)
        if key is not None and key.api_key_id == actor.api_key_id:
            self._redeliver(HeldSecret(actor.secret_ref, actor.credential_custody), key)
        return actor

    def delivers(self, actor: ActorAllocation) -> bool:
        """Whether this command hands ``actor``'s key over in its answer: it delivers, and the work engine
        holds the actor's key. Only then does a resume ask the provider to replay the key."""
        return self._delivery is not None and actor.credential_custody == "work_engine"

    async def overtaken(
        self, actor_id: str, current: ActorAllocation | None, key: NewAllocationKey, own: HeldSecret
    ) -> ActorAllocation:
        """The answer of an allotment that did not write its row, because another command wrote the
        actor while it was at the provider: its row would undo a pause, a rotation or a revoke that
        finished meanwhile. The command that wrote first was an allotment that had read what this one
        read, so it sent the same actor-derived provider operation and got this same allocation and key:
        nothing this one minted is unrecorded, and only its own copy of the secret (``own``) is extra.
        The answer comes from ``current``, the actor as stored now, by the rule a resumed run follows:
        the actor is this command's result while it holds the key this command got."""
        if current is not None and current.status != "revoked" and current.api_key_id == key.api_key_id:
            return await self.shared(current, own, key)
        await self._drop(own)  # the actor holds another key or is revoked: nothing points at it
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
            await self._drop(recorded_secret(run))
        raise NotFound(
            f"this rotation began on allocation {began_on!r}, which actor {actor.actor_id!r} no longer "
            f"holds: it was revoked and allotted again (allocation {actor.allocation_id!r}). Rotate "
            "that allocation's key under a new reference"
        )

    async def rotation_key(
        self,
        run: RunningOperation,
        manager: AllocationManager,
        actor: ActorAllocation,
        name: str,
        sent_as: str,
    ) -> NewAllocationKey:
        """The key of the rotation's provider call (``sent_as``, its idempotency key). Batteries replays
        it to a resume, so it is asked each time. A provider that hands a key over once
        (``OneKeyPerAllocation``) answers the repeat of a finished command without it: when this record
        notes the stored key of that very call, the engine holds it, and it is read back from the store
        and not asked for. A noted secret that is gone was dropped by this rotation as it lost: the
        provider is asked, and says so."""
        if isinstance(manager, OneKeyPerAllocation) and run.context.get("new_secret_sent_as") == sent_as:
            noted = recorded_secret(run)
            if noted.custody == "secret_store":
                try:
                    secret = SecretStr(await self._retiring.get(noted.ref))
                except NotFound:
                    return await manager.create_key(actor.allocation_id, name, sent_as)
                key_id = run.context["new_secret_key_id"]
                return NewAllocationKey(allocation_id=actor.allocation_id, api_key_id=key_id, secret=secret)
        return await manager.create_key(actor.allocation_id, name, sent_as)

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
        nothing, revokes its own key and is refused, ``InvalidRequest``. With a provider that keeps one
        key for an allocation, its order decides instead (``overtaken_rotation``), and no key is revoked
        by its id."""
        previous_key = run.context["previous_api_key_id"]
        ends_keys_itself = isinstance(manager, OneKeyPerAllocation)
        if actor.api_key_id != key.api_key_id:  # not stored yet
            new = await self.stored(run, actor.tenant_id, actor.actor_id, key, sent_as)
            actor = await self._pointed_at(actor, key.api_key_id, new, previous_key)
            if actor.api_key_id != key.api_key_id and isinstance(manager, OneKeyPerAllocation):
                actor = await self.overtaken_rotation(run, manager, actor, key, new)
            elif actor.api_key_id != key.api_key_id:
                # Another rotation's key: this one wrote nothing, so its own key must not stay live. The
                # revoke comes first: a key still live keeps its secret stored, and a revoke that fails
                # leaves the record open for a repeat, which gets the same key replayed and comes back here.
                await manager.revoke_key(key.api_key_id)
                await self._drop(new)
        elif actor.secret_ref.ref == run.context.get("new_secret_ref"):
            # A resume after this rotation pointed the actor at its key: the answer the first run never
            # gave still carries the key, which the provider replayed.
            self._redeliver(HeldSecret(actor.secret_ref, actor.credential_custody), key)
        if _SUPERSEDED in run.context and run.context[_SUPERSEDED] != actor.secret_ref.ref:
            # The ended key this rotation replaced on the actor, noted before that write: its secret goes.
            ended = SecretRef(ref=run.context[_SUPERSEDED])
            await self._drop(HeldSecret(ended, _custody(run.context, _SUPERSEDED_CUSTODY)))
        # The key this rotation began from is revoked in either case, unless the actor holds it: when this
        # rotation stored its key, failed here and was then replaced by a newer one, nothing else revokes it.
        # A provider that keeps one key for an allocation ended it when it minted this one.
        if previous_key not in (key.api_key_id, actor.api_key_id) and not ends_keys_itself:  # never live (B1)
            await manager.revoke_key(previous_key)  # repeat-safe upstream
        previous = HeldSecret(
            SecretRef(ref=run.context["previous_secret_ref"]), _custody(run.context, PREVIOUS_CUSTODY)
        )
        if previous.ref != actor.secret_ref:
            await self._drop(previous)
        if actor.api_key_id != key.api_key_id:
            # Noted, and never freed: under a free reference the provider would replay the revoked key.
            await run.remember(**{_LOST_TO: actor.api_key_id})
            raise _lost_rotation(actor.actor_id, actor.api_key_id)
        return actor

    async def overtaken_rotation(
        self,
        run: RunningOperation,
        provider: OneKeyPerAllocation,
        actor: ActorAllocation,
        key: NewAllocationKey,
        new: HeldSecret,
    ) -> ActorAllocation:
        """The actor after a rotation that another one overtook in the engine, at a provider that keeps
        one key for an allocation: the provider's order decides which key the engine keeps (Mike, 23
        November 2026). ``actor`` holds the other rotation's key; ``key`` is this one's, stored as ``new``.

        While the allocation takes this rotation's key, the provider ran it last: the actor's key is
        ended, and the actor is pointed at this one, over the key it holds at that moment and no other.
        That key's secret is noted first, so a resume drops it (``rotated``). A third rotation that wrote
        the actor in between is asked about again. When the allocation does not take this key, the
        provider ended it: nothing is live to keep, and its secret is dropped. Nothing is ever minted
        or revoked here, and every step is safe to repeat."""
        while actor.api_key_id != key.api_key_id and await provider.holds_key(
            actor.allocation_id, key.api_key_id
        ):
            ended = {_SUPERSEDED: actor.secret_ref.ref, _SUPERSEDED_CUSTODY: actor.credential_custody}
            await run.remember(**ended)
            actor = await self._pointed_at(actor, key.api_key_id, new, actor.api_key_id)
        if actor.api_key_id != key.api_key_id:
            await self._drop(new)
        return actor

    async def _pointed_at(
        self, actor: ActorAllocation, api_key_id: str, new: HeldSecret, previous_key: str
    ) -> ActorAllocation:
        """The actor after the rotation's key write: only those columns are written, so a status another
        command set meanwhile stays (#115, C4). An actor revoked meanwhile stays revoked: the new key is on
        a revoked allocation, so its secret is dropped. The write applies only over ``previous_key``, the
        key the rotation began from: an actor that holds another key got it from a rotation that finished
        first, and is returned as stored, holding that key, with nothing written."""
        current = await self._writes.key(
            actor, api_key_id, new.ref, credential_custody=new.custody, only_from=previous_key
        )
        if current is None:
            await self._drop(new)
            raise NotFound(f"actor {actor.actor_id!r} has no allocation")
        return current

    async def _drop(self, held: HeldSecret) -> None:
        """Forget a secret where it is held: a ``secret_store`` handle on the configured store, which
        issued it; a ``work_engine`` receipt only in this request's buffer, never on a store."""
        if held.custody == "secret_store":
            await self._retiring.delete(held.ref)
        elif self._delivery is not None:
            self._delivery.delete(held.ref)

    def _redeliver(self, held: HeldSecret, key: NewAllocationKey) -> None:
        """A delivering command holds the key the provider gave it under ``held``'s receipt, when that
        receipt is the work engine's: the answer then carries it. Anything else is left as it is."""
        if self._delivery is not None and held.custody == "work_engine":
            self._delivery.hold(held.ref, key.secret.get_secret_value())


def recorded_secret(run: RunningOperation) -> HeldSecret:
    """The secret a run recorded storing, with the custody recorded beside it."""
    return HeldSecret(SecretRef(ref=run.context["new_secret_ref"]), _custody(run.context, _NEW_CUSTODY))


def _custody(context: dict[str, Any], name: str) -> CredentialCustody:
    """A custody recorded in a ledger context. A record written before custody was recorded names none:
    every secret then was the configured store's."""
    value = context.get(name, "secret_store")
    for custody in CREDENTIAL_CUSTODIES:
        if value == custody:
            return custody
    raise ValueError(f"the operation record names an unknown credential custody {value!r}")


def _command(held: OperationRecord, custody: CredentialCustody) -> str:
    """The name of the command of one kind that runs under ``held``'s ledger command."""
    return _DELIVERING.get(held.command, held.command) if custody == "work_engine" else held.command


def _lost_rotation(actor_id: str, held: str) -> InvalidRequest:
    """The refusal of a rotation that another rotation of the same actor overtook (#115, C4)."""
    return InvalidRequest(
        f"another key rotation of actor {actor_id!r} finished after this one began: the actor holds key "
        f"{held!r} and this rotation's key is revoked. Rotate again under a new reference to replace it"
    )


def _secret_name(tenant_id: str, actor_id: str) -> str:
    return f"lpg/{tenant_id}/{actor_id}"
