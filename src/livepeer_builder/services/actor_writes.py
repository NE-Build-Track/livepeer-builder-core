"""Each admin command writes only its own actor columns, under a condition (#115, C4).

``fund``, ``set_status``, ``revoke`` and ``rotate_key`` read the actor, call the provider, then write. They
used to save the whole row from that read, so a command that finished in between was overwritten: a
rotation's new key by a pause, a pause by a rotation, a revoke by any of them. Now:

- a status command writes ``status`` and ``updated_at``;
- a key rotation writes ``api_key_id``, ``secret_ref`` and ``updated_at``;
- a fund moves ``exhausted`` to ``active`` and writes nothing else;
- no write applies to a ``revoked`` actor, or to an allocation other than the one the command acted on
  (one provisioned after a revoke).

A store that has the conditional writes (``ActorCommandRepo``, as the packaged SQL store does) checks the
condition in the write itself. For a supplied store without them, the engine reads the actor again,
checks the same condition and saves the whole row with ``save_actor``: a command that lands between that
read and the save is still overwritten, which only the store's own write can prevent.
"""

from typing import Any

from livepeer_builder.contracts import AllocationStatus, ProvisionedActor, SecretRef
from livepeer_builder.protocols import ActorCommandRepo, ActorRepo, Clock


class ActorWrites:
    """A command's write to the actor it read. ``None`` means the condition did not hold and nothing was
    written: the actor was revoked, or revoked and provisioned again, since the command read it."""

    def __init__(self, store: ActorRepo, clock: Clock) -> None:
        self._store = store
        self._clock = clock

    async def status(
        self, actor: ProvisionedActor, status: AllocationStatus, *, only_from: AllocationStatus | None = None
    ) -> ProvisionedActor | None:
        """Write ``status``; with ``only_from``, only where that is the stored status."""
        if isinstance(self._store, ActorCommandRepo):
            return await self._store.set_actor_status(
                actor.application_id,
                actor.actor_id,
                actor.allocation_id,
                status,
                self._clock.now(),
                only_from=only_from,
            )
        current = await self._held(actor)
        if current is None or only_from not in (None, current.status):
            return None
        return await self._saved(current, {"status": status})

    async def key(
        self, actor: ProvisionedActor, api_key_id: str, secret_ref: SecretRef
    ) -> ProvisionedActor | None:
        """Point the actor at a new payment key."""
        if isinstance(self._store, ActorCommandRepo):
            return await self._store.set_actor_key(
                actor.application_id,
                actor.actor_id,
                actor.allocation_id,
                api_key_id,
                secret_ref,
                self._clock.now(),
            )
        current = await self._held(actor)
        if current is None:
            return None
        return await self._saved(current, {"api_key_id": api_key_id, "secret_ref": secret_ref})

    async def funded(self, actor: ProvisionedActor) -> ProvisionedActor:
        """After a fund: ``exhausted`` becomes ``active``. Any other status stays as it is stored, one
        another command wrote meanwhile included, and the answer is the actor as stored."""
        activated = await self.status(actor, "active", only_from="exhausted")
        return activated or await self._store.get_actor(actor.application_id, actor.actor_id) or actor

    async def _held(self, actor: ProvisionedActor) -> ProvisionedActor | None:
        """Without the store's conditional write: the actor as stored now, when it still holds the
        allocation the command acted on and is not revoked."""
        current = await self._store.get_actor(actor.application_id, actor.actor_id)
        if current is None or current.allocation_id != actor.allocation_id or current.status == "revoked":
            return None
        return current

    async def _saved(self, current: ProvisionedActor, changes: dict[str, Any]) -> ProvisionedActor:
        updated = current.model_copy(update=changes | {"updated_at": self._clock.now()})
        await self._store.save_actor(updated)
        return updated
