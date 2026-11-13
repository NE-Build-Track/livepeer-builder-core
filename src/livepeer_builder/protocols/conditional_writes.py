"""Optional store protocols: writes that carry their own condition (#115).

Each is ``runtime_checkable`` and optional. The packaged SQL store implements them; the engine asks with
``isinstance`` and falls back, as documented on each, for a supplied store written before they existed.
They live here, re-exported from ``livepeer_builder.protocols``, because that module is at its size limit.
"""

from datetime import datetime
from typing import Protocol, runtime_checkable
from uuid import UUID

from livepeer_builder.contracts import AllocationStatus, MeasuredUsage, ProvisionedActor, SecretRef


@runtime_checkable
class ActorCommandRepo(Protocol):
    """The admin commands' writes to an actor (#115, C4). Each writes only the columns named, in one
    statement, and only where the actor still holds ``allocation_id`` and is not ``revoked``: a command
    never undoes another that finished after it read the actor, and nothing moves an actor out of
    ``revoked``. Returns the actor as stored after the write, or ``None`` when the condition did not hold
    and nothing was written.

    Without it, the engine reads the actor again, checks the same conditions itself and calls
    ``save_actor`` with the whole row: a command that lands between that read and the save is still
    overwritten."""

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
    ) -> ProvisionedActor | None:
        """Write ``api_key_id``, ``secret_ref`` and ``updated_at``."""
        ...


@runtime_checkable
class UsageOnceRepo(Protocol):
    """A job's usage, written once (#115, C20). ``JobRepo.set_usage`` updates unconditionally, so two
    reports at the same moment both succeeded and the second replaced the first.

    Without it, the engine checks the job it read and calls ``set_usage``, as before: that race remains."""

    async def set_usage_once(self, job_id: UUID, usage: MeasuredUsage) -> bool:
        """Store ``usage`` only where the job has none and is ``succeeded``, in one statement. ``True`` when
        it was stored; ``False``, with nothing written, otherwise."""
        ...
