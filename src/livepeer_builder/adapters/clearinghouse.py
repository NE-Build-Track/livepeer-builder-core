"""The payment ports of an engine that uses a payment clearinghouse: the remote adapters (D84, #168).

An enterprise's engine keeps its own jobs and records and reaches the clearinghouse for payments only.
Three of its five payment ports are here, each calling the tenant's own routes at the clearinghouse with
the tenant admin key, through ``TenantWire``, which sends nothing for or with the key of another tenant:

- ``ClearinghouseAllocations`` is the ``AllocationManager``. The port names an allocation by its id and
  the routes name an actor, so the manager is asked for the one actor a command is about
  (``AllocationsByActor``). What only a clearinghouse admin does to a grant is refused here, plainly.
- ``ClearinghouseAllowances`` is the ``AllowanceReader``.
- ``ClearinghouseUsage`` is the ``UsageSource``: the tenant's own payment records and no other's.

The other two are the engine's own, as with self-hosted payments (D85 point 1, D87 point 1, as amended).
``CredentialResolver`` is ``PaymentsService``: an allotment or a payment key rotation at the clearinghouse
answers the allocation's own Batteries key once, the manager here returns it as the port's new key, and
the engine's ordinary command stores it in its own secret store. Signing is the runner transport at
``LPB_SIGNER_URL``, the clearinghouse's published signer, with that key.

**A key is handed over once.** The clearinghouse stores none, so an answer that carries no key
(``replayed``: it finished that command before) leaves the engine with nothing to store, unless the
engine's own record shows it already stored the key of that answer. It is never a success here: the
admin is told what replaces the key (``_ActorAllocation._handed``).

The mapping, member by member, is in docs/contracts/payments.md#the-remote-adapters.
"""

import base64
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass

from pydantic import SecretStr

from livepeer_builder.adapters.clearinghouse_errors import (
    acted_elsewhere,
    clearinghouse_admin_only,
    needs_the_actor,
    never_arrived,
    not_held,
    not_kept,
    said,
)
from livepeer_builder.adapters.clearinghouse_tenant import USAGE_PATH, TenantWire, allocation_path, own_tenant
from livepeer_builder.adapters.clearinghouse_wire import ClearinghouseWire
from livepeer_builder.contracts import (
    ActorAllocation,
    AllocationRead,
    Allowance,
    DeliveredAllocation,
    GrantPage,
    GrantRecord,
    Money,
    NewAllocationKey,
    TenantGrant,
    UsagePage,
)
from livepeer_builder.contracts.costs import storable_text
from livepeer_builder.errors import (
    InvalidRequest,
    ProviderError,
    ProviderUnavailable,
)
from livepeer_builder.protocols import ActorRepo, AllocationManager, SecretStore

_LISTS_EVERY_TENANT = "lists every tenant"  # what three members of the port would do, and may not


def _money(amount: Money) -> dict[str, str]:
    return {"amount": format(amount.amount, "f"), "currency": amount.currency}


class ClearinghouseAllocations:
    """``AllocationManager`` at a clearinghouse. A command about one actor's allocation goes through
    ``for_actor``; what this object answers itself is the grant. ``actors`` and ``secrets`` are the
    engine's own records and secret store, read only to finish a command from the key the engine already
    stored."""

    def __init__(self, wire: ClearinghouseWire | TenantWire, actors: ActorRepo, secrets: SecretStore) -> None:
        self._wire = TenantWire.over(wire)
        self._actors = actors
        self._secrets = secrets

    def for_actor(self, tenant_id: str, actor_id: str, operation_ref: str | None = None) -> AllocationManager:
        """The manager for one actor's allocation (``AllocationsByActor``), for the admin command under
        ``operation_ref`` when it has one."""
        return _ActorAllocation(self._wire, self._actors, self._secrets, tenant_id, actor_id, operation_ref)

    async def ready_for_commands(self) -> None:
        """``ReadyBeforeCommands``: whose key the engine holds, read before a command's record begins."""
        await self._wire.confirm()

    # -- the grant ----------------------------------------------------------------------------------
    # Every member below but ``read_grant`` is async by protocol and awaits nothing: it refuses, or has
    # nothing to do, before anything is sent. Each says so to SonarQube, as the provider fakes do.

    async def read_grant(self, grant_id: str) -> TenantGrant:
        """The tenant's grant. Another tenant's is refused (403), asked for or answered by the key."""
        own_tenant(self._wire, grant_id)
        return await self._wire.grant()

    async def top_up_grant(  # NOSONAR - async by protocol (AllocationManager)
        self, grant_id: str, amount: Money, idempotency_key: str
    ) -> None:
        del grant_id, amount, idempotency_key
        clearinghouse_admin_only("tops up a tenant's grant")

    async def set_grant_status(  # NOSONAR - async by protocol (AllocationManager)
        self, grant_id: str, status: str
    ) -> None:
        del grant_id, status
        clearinghouse_admin_only("pauses or resumes a tenant")

    async def create_grant(  # NOSONAR - async by protocol (AllocationManager)
        self, name: str, amount: Money, metadata: Mapping[str, str] | None = None
    ) -> GrantRecord:
        del name, amount, metadata
        clearinghouse_admin_only("onboards a tenant")

    async def list_grants(  # NOSONAR - async by protocol (AllocationManager)
        self,
    ) -> list[GrantRecord]:
        clearinghouse_admin_only(_LISTS_EVERY_TENANT)

    async def grant_page(  # NOSONAR - async by protocol (AllocationManager)
        self, cursor: str | None
    ) -> GrantPage:
        del cursor
        clearinghouse_admin_only(_LISTS_EVERY_TENANT)

    async def grant_balances(  # NOSONAR - async by protocol (AllocationManager)
        self,
    ) -> list[TenantGrant]:
        clearinghouse_admin_only(_LISTS_EVERY_TENANT)

    # -- an allocation, with no actor named -----------------------------------------------------------

    async def create_key_for_grant(  # NOSONAR - async by protocol (AllocationManager)
        self, grant_id: str, name: str, amount: Money, idempotency_key: str
    ) -> NewAllocationKey:
        del grant_id, name, amount, idempotency_key
        needs_the_actor("create_key_for_grant")

    async def create_key(  # NOSONAR - async by protocol (AllocationManager)
        self, allocation_id: str, name: str, idempotency_key: str
    ) -> NewAllocationKey:
        del allocation_id, name, idempotency_key
        needs_the_actor("create_key")

    async def top_up_allocation(  # NOSONAR - async by protocol (AllocationManager)
        self, allocation_id: str, amount: Money, idempotency_key: str
    ) -> None:
        del allocation_id, amount, idempotency_key
        needs_the_actor("top_up_allocation")

    async def set_allocation_status(  # NOSONAR - async by protocol (AllocationManager)
        self, allocation_id: str, status: str
    ) -> None:
        del allocation_id, status
        needs_the_actor("set_allocation_status")

    async def revoke_allocation(  # NOSONAR - async by protocol (AllocationManager)
        self, allocation_id: str
    ) -> None:
        del allocation_id
        needs_the_actor("revoke_allocation")

    async def read_allocation(  # NOSONAR - async by protocol (AllocationManager)
        self, allocation_id: str
    ) -> Allowance:
        del allocation_id
        needs_the_actor("read_allocation")

    async def revoke_key(  # NOSONAR - async by protocol (AllocationManager)
        self, api_key_id: str
    ) -> None:
        del api_key_id
        needs_the_actor("revoke_key")

    async def holds_key(  # NOSONAR - async by protocol (OneKeyPerAllocation)
        self, allocation_id: str, api_key_id: str
    ) -> bool:
        del allocation_id, api_key_id
        needs_the_actor("holds_key")


class _ActorAllocation(ClearinghouseAllocations):
    """The same manager for one actor: each member that names an allocation calls that actor's route."""

    def __init__(
        self,
        wire: TenantWire,
        actors: ActorRepo,
        secrets: SecretStore,
        tenant_id: str,
        actor_id: str,
        operation_ref: str | None,
    ) -> None:
        super().__init__(wire.for_command(), actors, secrets)  # stopped by an answer for another tenant
        self._tenant_id = tenant_id
        self._actor_id = actor_id
        self._operation_ref = operation_ref

    def _path(self, rest: str = "") -> str:
        own_tenant(self._wire, self._tenant_id)  # before any request about this actor
        return allocation_path(self._actor_id, rest)

    async def create_key_for_grant(
        self, grant_id: str, name: str, amount: Money, idempotency_key: str
    ) -> NewAllocationKey:
        """Allot, and return the key the clearinghouse hands over. A repeat here is a repeat there
        (``_allotment_ref``). An actor with no label of its own is sent with none, so the clearinghouse
        names the allocation by its own default."""
        if grant_id != self._tenant_id:  # a tenant's id is its grant's id (D83)
            raise InvalidRequest("an allocation is allotted from its own tenant's grant")
        label = "" if name == f"{self._tenant_id}/{self._actor_id}" else name
        body = {
            "amount": _money(amount),
            "label": label,
            "operation_ref": self._allotment_ref(idempotency_key),
        }
        return await self._handed(await self._wire.write_as(DeliveredAllocation, self._path(), body))

    async def create_key(self, allocation_id: str, name: str, idempotency_key: str) -> NewAllocationKey:
        """Rotate the payment key, and return the new key the clearinghouse hands over: one command
        there, which ends the old key itself. The engine's idempotency key is the route's
        ``operation_ref``: it names the allocation and the admin's reference."""
        del name
        await self._held(allocation_id)
        body = {"operation_ref": idempotency_key}
        return await self._handed(
            await self._wire.write_as(DeliveredAllocation, self._path("/rotate-payment-key"), body),
            rotated=allocation_id,
        )

    async def revoke_key(self, api_key_id: str) -> None:
        """End one key. A clearinghouse has no route that revokes a key by its id: a rotation there ends
        the key before it, and a revoked allocation takes none. So the actor's allocation is read. A key
        it no longer holds has ended already, and nothing is sent. The key it holds now cannot be ended
        without minting another, which nobody asked for: that is refused, plainly. The engine's rotation
        never asks either (``OneKeyPerAllocation``); this is for a caller of the port alone."""
        held = await self._wire.read_as(AllocationRead, self._path())
        if held.status == "revoked" or held.api_key_id != api_key_id:
            return
        raise InvalidRequest(
            f"key {api_key_id!r} is the one allocation {said(held.allocation_id)} takes at the clearinghouse "
            "now, and a clearinghouse ends a key only by the rotation that replaces it: rotate the "
            f"payment key of actor {self._actor_id!r}"
        )

    async def holds_key(self, allocation_id: str, api_key_id: str) -> bool:
        """Whether the clearinghouse takes ``api_key_id`` for ``allocation_id`` now
        (``OneKeyPerAllocation``): the actor holds that allocation there, it is not revoked, and its key
        is this one. Of two rotations the clearinghouse ran one after the other, this is true of the
        later one's key alone."""
        held = await self._wire.read_as(AllocationRead, self._path())
        return (held.allocation_id, held.api_key_id) == (
            allocation_id,
            api_key_id,
        ) and held.status != "revoked"

    def _allotment_ref(self, idempotency_key: str) -> str:
        """An allotment's reference at the clearinghouse, which keys its own ledger on it for this tenant
        admin key. The engine's idempotency key names the engine and the actor and no reference (two
        references for one actor meet at Batteries by it), and the clearinghouse answers a reference it
        finished without the key, for good. So the admin's reference is folded in: a new reference here
        is a new command there, which the clearinghouse judges by its own record of the actor, and the
        repeat of one reference is the repeat of one command."""
        if self._operation_ref is None:
            return idempotency_key
        return _digest(idempotency_key, self._operation_ref)

    async def top_up_allocation(self, allocation_id: str, amount: Money, idempotency_key: str) -> None:
        await self._held(allocation_id)
        body = {"amount": _money(amount), "operation_ref": idempotency_key}
        self._acted_on(
            await self._wire.write_as(ActorAllocation, self._path("/top-up"), body),
            allocation_id,
            "added this top-up to",
            "The amount is on that allocation now: revoking that allocation at the clearinghouse returns "
            "what it has not spent to the grant",
        )

    async def set_allocation_status(self, allocation_id: str, status: str) -> None:
        await self._held(allocation_id)
        self._acted_on(
            await self._wire.write_as(ActorAllocation, self._path("/status"), {"status": status}),
            allocation_id,
            f"set the status {status!r} on",
            "Set that allocation's status again at the clearinghouse if this was not meant for it",
        )

    async def revoke_allocation(self, allocation_id: str) -> None:
        """Repeat-safe, as at the provider. An actor that holds another allocation at the clearinghouse
        was revoked and allotted again there, so the one named has ended already: the other is not this
        command's to revoke."""
        held = await self._wire.read_as(AllocationRead, self._path())
        if held.allocation_id == allocation_id and held.status != "revoked":
            self._acted_on(
                await self._wire.write_as(ActorAllocation, self._path("/revoke")),
                allocation_id,
                "revoked",
                "Repeat this revoke: the allocation it named has ended there, so the repeat sends "
                "nothing and records that here. The allocation revoked in its place is ended, and what "
                "it had not spent is back in the grant: whoever held it allots the actor again",
            )

    async def read_allocation(self, allocation_id: str) -> Allowance:
        return await read_allowance(self._wire, self._tenant_id, self._actor_id, allocation_id)

    async def _held(self, allocation_id: str) -> ActorAllocation:
        """The actor's allocation at the clearinghouse, when it is the one the engine names. The routes
        act on whichever allocation the actor holds there, so a command about another one is refused
        before it is sent, as the provider answers an allocation id it does not hold. This proves what
        the clearinghouse held at the read, and no more: the command's own answer is read too
        (``_acted_on``)."""
        held = await self._wire.read_as(AllocationRead, self._path())
        if held.allocation_id != allocation_id:
            raise not_held(self._actor_id, allocation_id)
        return held

    def _acted_on(self, answer: ActorAllocation, named: str, did: str, then: str) -> None:
        """Refuse to report a command as done when the clearinghouse applied it to another allocation
        than the one it named. The routes name an actor and take no allocation id, so a command lands on
        whichever allocation the actor holds when it arrives: a revoke and a new allotment there, between
        this adapter's read and its command, move it. Each route answers the allocation it acted on.

        No refusal: something was applied, so the operation ledger keeps the command's reference
        (``ProviderError`` with the answer's 2xx, as for any answer the engine cannot use). Raised before
        the engine writes anything, so its record of the actor is not changed as if the command it named
        had been done."""
        if answer.allocation_id != named:
            raise acted_elsewhere(self._actor_id, named, answer.allocation_id, did, then)

    async def _handed(self, answer: DeliveredAllocation, *, rotated: str | None = None) -> NewAllocationKey:
        """The key an allotment or a rotation handed over, as the port's new key: the engine's command
        stores it in the engine's own secret store. ``rotated`` is the allocation a rotation named.

        ``replayed`` says the answer carries none: the clearinghouse finished this reference before, and
        hands a key over once. When the engine's record of the actor names the allocation and the key of
        that answer, with the secret in its own store, the engine was handed it then and its run died
        afterwards: the command finishes from that copy. Otherwise the key never reached this engine. It
        is refused, with what replaces it, and nothing is stored or saved.

        A key handed over under an id the engine's records cannot keep is not taken (``not_kept``, #158):
        the answer that carries it, and the ``replayed`` one that names the same ids afterwards, are the
        same answer the engine cannot use."""
        allocation_id, api_key_id, key = answer.allocation_id, answer.api_key_id, answer.allocation_api_key
        replayed = answer.replayed
        del answer  # no error raised below has the answer in its frame, and no caller keeps it in one
        if rotated is not None and allocation_id != rotated:
            # Revoked and allotted again there between the adapter's read and its command: this key is
            # another allocation's, and the actor's record here is never pointed at it.
            del key
            raise acted_elsewhere(
                self._actor_id,
                rotated,
                allocation_id,
                "rotated the key of",
                "Its new key is in an answer this engine did not keep, and the key it had is ended: "
                "whoever holds that allocation rotates its key again",
            )
        kept = storable_text(allocation_id) and storable_text(api_key_id)
        if not kept and (key is not None or replayed):  # handed over now, or before: not taken either way
            del key
            raise not_kept(self._actor_id, rotation=rotated is not None)
        if key is not None and not replayed:
            return NewAllocationKey(allocation_id=allocation_id, api_key_id=api_key_id, secret=key)
        del key
        if not replayed:
            # No refusal: the allocation exists there, and its key is in the clearinghouse's own store.
            raise ProviderError(
                200,
                "the clearinghouse answered with no key and did not say the answer was a repeat: it "
                "keeps each allocation's key itself, as an engine that runs its own jobs does, and "
                "hands this engine none. LPB_CLEARINGHOUSE_URL must name an engine in its clearinghouse "
                "role (LPB_ROLE=clearinghouse). The allocation exists there",
            )
        own = await self._actors.get_actor(self._tenant_id, self._actor_id)
        if (
            own is not None
            and own.status != "revoked"
            and own.credential_custody == "secret_store"
            and (own.allocation_id, own.api_key_id) == (allocation_id, api_key_id)
        ):
            return NewAllocationKey(
                allocation_id=allocation_id,
                api_key_id=api_key_id,
                secret=SecretStr(await self._secrets.get(own.secret_ref)),
            )
        raise never_arrived(self._actor_id, allocation_id, rotation=rotated is not None)


class ClearinghouseAllowances:
    """``AllowanceReader``. The allocation that pays for an actor is the engine's own record: it is the
    one whose payment key the engine holds. Its allowance is read at the clearinghouse."""

    def __init__(self, wire: ClearinghouseWire | TenantWire, actors: ActorRepo) -> None:
        self._wire = TenantWire.over(wire)
        self._actors = actors

    async def allocation_for(self, tenant_id: str, actor_id: str) -> str | None:
        allotted = await self._actors.get_actor(tenant_id, actor_id)
        return allotted.allocation_id if allotted else None

    async def read_allowance(self, allocation_id: str) -> Allowance:
        """The clearinghouse reads an actor's allocation, so the actor is found first, among the
        tenant's actors this engine has allotted."""
        tenant_id = self._wire.tenant_id
        for actor in await self._actors.list_actors(tenant_id):
            if actor.allocation_id == allocation_id:
                return await read_allowance(self._wire, tenant_id, actor.actor_id, allocation_id)
        raise ProviderError(404, f"no actor of tenant {tenant_id!r} holds allocation {allocation_id!r} now")


class ClearinghouseUsage:
    """``UsageSource``: the tenant's payment records, oldest first. The page is the port's own."""

    def __init__(self, wire: ClearinghouseWire | TenantWire) -> None:
        self._wire = TenantWire.over(wire)

    async def page(self, cursor: str | None, limit: int = 1000) -> UsagePage:
        params = {"limit": str(limit)} | ({"cursor": cursor} if cursor else {})
        return await self._wire.read_as(UsagePage, USAGE_PATH, params)


@dataclass(frozen=True)
class ClearinghousePorts:
    """The three remote adapters over one wire, ``tenant``, which whoever else sends the tenant admin key
    asks whose key it is. ``allocations`` is the grants manager too: it reads the grant, and no more."""

    allocations: ClearinghouseAllocations
    allowances: ClearinghouseAllowances
    usage: ClearinghouseUsage
    tenant: TenantWire

    @classmethod
    def over(cls, wire: ClearinghouseWire, actors: ActorRepo, secrets: SecretStore) -> "ClearinghousePorts":
        own = TenantWire(wire)
        return cls(
            ClearinghouseAllocations(own, actors, secrets),
            ClearinghouseAllowances(own, actors),
            ClearinghouseUsage(own),
            own,
        )


async def read_allowance(wire: TenantWire, tenant_id: str, actor_id: str, allocation_id: str) -> Allowance:
    """The allowance of the allocation ``allocation_id``, which ``actor_id`` must hold now: read from the
    actor's allocation route, which answers it with the record. Another tenant's actor is refused (403)
    and never asked for. An allowance the clearinghouse could not read at its own provider (``null``) is
    ``ProviderUnavailable``: unknown, and never read as zero."""
    own_tenant(wire, tenant_id)
    read = await wire.read_as(AllocationRead, allocation_path(actor_id))
    if read.allocation_id != allocation_id:
        raise not_held(actor_id, allocation_id)
    if read.allowance is None:
        raise ProviderUnavailable("the clearinghouse could not read the allowance at its payment provider")
    return read.allowance


def _digest(*parts: str) -> str:
    """base64url(sha256(parts joined by ``:``)): 43 characters, as the engine's idempotency keys are."""
    digest = hashlib.sha256(":".join(parts).encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")
