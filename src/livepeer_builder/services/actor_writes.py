"""Each admin command writes only its own actor columns, under a condition (#115, C4).

``top_up``, ``set_status``, ``revoke`` and ``rotate_payment_key`` read the actor, call the provider, then
write. They used to save the whole row from that read, so a command that finished in between was
overwritten: a rotation's new key by a pause, a pause by a rotation, a revoke by any of them. Now:

- a status command writes ``status`` and ``updated_at``;
- a key rotation writes ``api_key_id``, ``secret_ref``, ``credential_custody`` and ``updated_at``, and only
  over the key it began from: an actor holding another key got it from a rotation that finished first;
- a top-up moves ``exhausted`` to ``active`` and writes nothing else;
- no write applies to a ``revoked`` actor, or to an allocation other than the one the command acted on
  (one allotted after a revoke);
- allotment, the one command that writes the whole row, writes it only where the actor is as it read
  it: no row, or the ``revoked`` row it is replacing.

A store that has the conditional writes (``ActorCommandRepo``, as the packaged SQL store does) checks the
condition in the write itself. For a supplied store without them, the engine reads the actor again,
checks the same condition and saves the whole row with ``save_actor``: a command that lands between that
read and the save is still overwritten, which only the store's own write can prevent.

Two status commands write the same column, so no condition orders them: a pause and an activate at the
same moment could reach the provider in one order and the store in the other. A status command therefore
reads the provider's status back after its write and stores that (``status_read_back``, #158).
"""

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from livepeer_builder.contracts import (
    ActorAllocation,
    AllocationStatus,
    Allowance,
    CredentialCustody,
    SecretRef,
)
from livepeer_builder.errors import ProviderError, ProviderUnavailable
from livepeer_builder.protocols import ActorCommandRepo, ActorRepo, Clock
from livepeer_builder.utils import BODY_LIMIT

_LOG = logging.getLogger(__name__)
# The statuses a status command sets, and so the only ones it stores from a read of the provider. The
# provider reports others: ``revoked`` is a revoke's to store, with the key it deletes, and ``exhausted``
# there is an allocation with nothing left, which the engine's record does not follow.
_SET_BY_A_COMMAND: tuple[AllocationStatus, ...] = ("active", "paused")
# How often one status command reads the provider back. One read settles a command alone, and each
# further one follows another command that was at the provider meanwhile.
_READ_BACKS = 3


class ActorWrites:
    """A command's write to the actor it read. ``None`` means the condition did not hold and nothing was
    written: the actor was revoked, or revoked and allotted again, since the command read it."""

    def __init__(self, store: ActorRepo, clock: Clock) -> None:
        self._store = store
        self._clock = clock

    async def status(
        self, actor: ActorAllocation, status: AllocationStatus, *, only_from: AllocationStatus | None = None
    ) -> ActorAllocation | None:
        """Write ``status``; with ``only_from``, only where that is the stored status."""
        if isinstance(self._store, ActorCommandRepo):
            return await self._store.set_actor_status(
                actor.tenant_id,
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

    async def status_read_back(
        self,
        actor: ActorAllocation,
        status: AllocationStatus,
        read: Callable[[str], Awaitable[Allowance]],
    ) -> ActorAllocation | None:
        """Write ``status``, which the provider has just taken, then store what the provider reports, and
        answer with the actor as stored when that is done.

        ``read`` reads the allocation at the provider. When it reports the other status a command sets,
        another status command reached the provider after this one: that status is stored and the
        provider read again, ``_READ_BACKS`` times at most. The write comes before the read, so the
        command that writes the record last has read the provider after that write and found what it
        wrote: whichever command the provider took last, the record ends on its status. A read that
        fails, or reports a status no status command sets, leaves the record as written.

        The answer is never one of this command's own writes. Another command can finish while a read of
        the provider is on its way back, and what that read reports is never written over it: so the row
        is read once more at the end, and that row is the answer, with the status, the key and the secret
        reference it holds then. ``None`` when the actor was revoked, or revoked and allotted again, at
        any point since the command read it."""
        written = await self.status(actor, status)
        reads = 0
        while written is not None and reads < _READ_BACKS:
            reported = await _reported(actor, read)
            reads += 1
            if reported is None or reported == written.status:
                break
            written = await self.status(actor, reported)
        if written is None:
            return None
        return await self._held(actor)

    async def key(
        self,
        actor: ActorAllocation,
        api_key_id: str,
        secret_ref: SecretRef,
        *,
        credential_custody: CredentialCustody,
        only_from: str,
    ) -> ActorAllocation | None:
        """Point the actor at a new payment key, and record who holds its credential, only where it still
        holds the key ``only_from`` (the one the rotation began from) or holds the new key already. An
        actor that holds another key got it from a rotation that finished first: nothing is written, and
        the answer is the actor as stored, holding that key."""
        if isinstance(self._store, ActorCommandRepo):
            written = await self._store.set_actor_key(
                actor.tenant_id,
                actor.actor_id,
                actor.allocation_id,
                api_key_id,
                secret_ref,
                self._clock.now(),
                only_from,
                credential_custody=credential_custody,
            )
            return written or await self._held(actor)  # nothing written: read why
        current = await self._held(actor)
        if current is None or current.api_key_id not in (only_from, api_key_id):
            return current
        changes = {"api_key_id": api_key_id, "secret_ref": secret_ref}
        return await self._saved(current, changes | {"credential_custody": credential_custody})

    async def allotted(self, actor: ActorAllocation, *, replacing: ActorAllocation | None) -> bool:
        """Write a newly allotted actor's whole row, only where the actor is still as allotment read
        it: no row (``replacing`` is ``None``) or the ``revoked`` row ``replacing``. ``False`` means
        another command wrote the actor first and nothing was written."""
        if isinstance(self._store, ActorCommandRepo):
            allocation_id = replacing.allocation_id if replacing is not None else None
            return await self._store.allot_actor(actor, replacing=allocation_id)
        if await self._store.get_actor(actor.tenant_id, actor.actor_id) != replacing:
            return False
        await self._store.save_actor(actor)
        return True

    async def topped_up(self, actor: ActorAllocation) -> ActorAllocation:
        """After a top-up: ``exhausted`` becomes ``active``. Any other status stays as it is stored, one
        another command wrote meanwhile included, and the answer is the actor as stored."""
        activated = await self.status(actor, "active", only_from="exhausted")
        return activated or await self._store.get_actor(actor.tenant_id, actor.actor_id) or actor

    async def _held(self, actor: ActorAllocation) -> ActorAllocation | None:
        """The actor as stored now, when it still holds the allocation the command acted on and is not
        revoked: what a write is checked against without the store's conditional write, and what a status
        command answers with."""
        current = await self._store.get_actor(actor.tenant_id, actor.actor_id)
        if current is None or current.allocation_id != actor.allocation_id or current.status == "revoked":
            return None
        return current

    async def _saved(self, current: ActorAllocation, changes: dict[str, Any]) -> ActorAllocation:
        updated = current.model_copy(update=changes | {"updated_at": self._clock.now()})
        await self._store.save_actor(updated)
        return updated


async def _reported(
    actor: ActorAllocation, read: Callable[[str], Awaitable[Allowance]]
) -> AllocationStatus | None:
    """The status the provider reports for ``actor``'s allocation, when it is one a status command sets.
    ``None`` otherwise, and when the provider cannot be read: the command it follows is done, so the
    failure is logged and is not the command's."""
    try:
        status = (await read(actor.allocation_id)).status
    except (ProviderError, ProviderUnavailable) as error:
        _LOG.warning(
            "status stored without the provider's read-back",
            extra={
                "event": "payments.status_read_back_failed",
                "tenant_id": actor.tenant_id,
                "actor_id": actor.actor_id,
                "allocation_id": actor.allocation_id,
                "provider_status": error.status_code if isinstance(error, ProviderError) else None,
                "detail": str(error)[:BODY_LIMIT],
            },
        )
        return None
    return next((known for known in _SET_BY_A_COMMAND if known == status), None)
