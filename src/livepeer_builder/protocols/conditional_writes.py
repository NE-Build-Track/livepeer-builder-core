"""Optional store protocols: writes that carry their own condition (#115).

Each is ``runtime_checkable`` and optional. The packaged SQL store implements them; the engine asks with
``isinstance`` and falls back, as documented on each, for a supplied store written before they existed.
They live here, re-exported from ``livepeer_builder.protocols``, because that module is at its size limit.
"""

from datetime import datetime
from typing import Protocol, runtime_checkable

from livepeer_builder.contracts import AllocationStatus, ProvisionedActor, SecretRef


@runtime_checkable
class ActorCommandRepo(Protocol):
    """The admin commands' writes to an actor (#115, C4). ``set_actor_status`` and ``set_actor_key`` each
    write only the columns named, in one statement, and only where the actor still holds
    ``allocation_id`` and is not ``revoked``: a command never undoes another that finished after it read
    the actor, and nothing moves an actor out of ``revoked``. A key write also requires the key the
    rotation began from, so two rotations never undo each other. They return the actor as stored after
    the write, or ``None`` when the condition did not hold and nothing was written. ``provision_actor``
    writes the whole row of a new allocation, only where the actor is as provisioning read it.

    A store has all three or is treated as having none. Without them, the engine reads the actor again,
    checks the same conditions itself and calls ``save_actor`` with the whole row: a command that lands
    between that read and the save is still overwritten."""

    async def set_actor_status(
        self,
        application_id: str,
        actor_id: str,
        allocation_id: str,
        status: AllocationStatus,
        updated_at: datetime,
        *,
        only_from: AllocationStatus | None = None,
    ) -> ProvisionedActor | None:
        """Write ``status`` and ``updated_at``; with ``only_from``, only where that is the stored status."""
        ...

    async def set_actor_key(
        self,
        application_id: str,
        actor_id: str,
        allocation_id: str,
        api_key_id: str,
        secret_ref: SecretRef,
        updated_at: datetime,
        *,
        only_from: str,
    ) -> ProvisionedActor | None:
        """Write ``api_key_id``, ``secret_ref`` and ``updated_at``, only where the stored key is
        ``only_from`` (the key the rotation began from) or already ``api_key_id`` (a repeat of this
        write). An actor holding any other key got it from a rotation that finished first."""
        ...

    async def provision_actor(self, actor: ProvisionedActor, *, replacing: str | None) -> bool:
        """Write the whole row of a newly provisioned actor, only where the actor is as provisioning read
        it. With ``replacing`` ``None`` it had no row: insert it. Otherwise it had a ``revoked`` row
        holding the allocation ``replacing``: replace that row, and no other (a revoked row changes only
        when a provisioning replaces it, and that brings another allocation). ``False``, with nothing
        written, when another command wrote the actor first."""
        ...
