"""The payment ports of an engine that uses a payment clearinghouse: the remote adapters (D84, #168).

An enterprise's engine keeps its own jobs and records and reaches the clearinghouse for payments only.
Four of its five payment ports are here, each calling the tenant's own routes at the clearinghouse with
the tenant admin key (``adapters/clearinghouse_wire.py``). The fifth, signing, is inside the runner
transport: the gateway SDK's, pointed at the clearinghouse, with the actor's payment key
(``runtime/payment_defaults.py``).

The tenant's own routes name no tenant: the clearinghouse takes it from the key (D87 point 3, as amended
on 19 November 2026). So a command about another tenant cannot be sent at all: sent, it would act on this
tenant. It is refused here, before anything is sent, as the clearinghouse refused a path that named
another tenant: 403, "another tenant". The tenant is the wire's, ``LPB_CLEARINGHOUSE_TENANT_ID``.

- ``ClearinghouseAllocations`` is the ``AllocationManager``. The port names an allocation by its id and
  the routes name an actor, so the manager is asked for the one actor a command is about
  (``AllocationsByActor``). What only a clearinghouse admin does to a grant is refused here, plainly.
- ``ClearinghouseAllowances`` is the ``AllowanceReader``.
- ``ClearinghouseUsage`` is the ``UsageSource``: the tenant's own payment records and no other's.
- ``ClearinghouseCredentials`` is the ``CredentialResolver``: the payment key the clearinghouse issued
  for the actor, which the engine keeps in its secret store, and never any other secret.

The mapping, member by member, is in docs/contracts/payments.md#the-remote-adapters.
"""

import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import NoReturn

from pydantic import SecretStr

from livepeer_builder.adapters.batteries import segment
from livepeer_builder.adapters.clearinghouse_wire import ClearinghouseWire
from livepeer_builder.contracts import (
    ActorAllocation,
    ActorContext,
    AllocationRead,
    Allowance,
    GrantPage,
    GrantRecord,
    KeyedAllocation,
    Money,
    NewAllocationKey,
    PaymentKey,
    TenantGrant,
    UsagePage,
)
from livepeer_builder.contracts.payments import PAYMENT_KEY_PREFIX
from livepeer_builder.errors import (
    AccessDenied,
    InvalidRequest,
    NotProvisioned,
    ProviderError,
    ProviderUnavailable,
)
from livepeer_builder.protocols import ActorRepo, AllocationManager, SecretStore

_LOG = logging.getLogger(__name__)
_LISTS_EVERY_TENANT = "lists every tenant"  # what three members of the port would do, and may not
OWN_TENANT = "/v1/clearinghouse"  # the tenant's own routes: no tenant in the path, the key's is used
GRANT_PATH = OWN_TENANT + "/grant"
USAGE_PATH = OWN_TENANT + "/usage"


def allocation_path(actor_id: str, rest: str = "") -> str:
    """The actor's allocation route, among the tenant's own routes."""
    return OWN_TENANT + "/actors/" + segment(actor_id) + "/allocation" + rest


def own_tenant(wire: ClearinghouseWire, tenant_id: str) -> None:
    """Refuse a command about another tenant than the key's, before anything is sent. The clearinghouse
    would apply it to the key's own tenant, whose routes name none. A refusal (403), as the clearinghouse
    refused a path that named another tenant: applied nowhere."""
    if tenant_id != wire.tenant_id:
        raise ProviderError(
            403,
            f"resource belongs to another tenant: this engine's tenant at the clearinghouse is "
            f"{wire.tenant_id!r} (LPB_CLEARINGHOUSE_TENANT_ID), not {tenant_id!r}",
        )


def _money(amount: Money) -> dict[str, str]:
    return {"amount": format(amount.amount, "f"), "currency": amount.currency}


class ClearinghouseAllocations:
    """``AllocationManager`` at a clearinghouse. A command about one actor's allocation goes through
    ``for_actor``; what this object answers itself is the grant."""

    def __init__(self, wire: ClearinghouseWire) -> None:
        self._wire = wire

    def for_actor(self, tenant_id: str, actor_id: str) -> AllocationManager:
        """The manager for one actor's allocation (``AllocationsByActor``). Each of its members refuses an
        actor of another tenant before it sends anything."""
        return _ActorAllocation(self, self._wire, tenant_id, actor_id)

    # -- the grant ----------------------------------------------------------------------------------
    # Every member below but ``read_grant`` is async by protocol and awaits nothing: it refuses, or has
    # nothing to do, before anything is sent. Each says so to SonarQube, as the provider fakes do.

    async def read_grant(self, grant_id: str) -> TenantGrant:
        """The tenant's grant. Another tenant's is refused (403) and never asked for."""
        own_tenant(self._wire, grant_id)
        return await self._wire.read_as(TenantGrant, GRANT_PATH)

    async def top_up_grant(  # NOSONAR - async by protocol (AllocationManager)
        self, grant_id: str, amount: Money, idempotency_key: str
    ) -> None:
        del grant_id, amount, idempotency_key
        _clearinghouse_admin_only("tops up a tenant's grant")

    async def set_grant_status(  # NOSONAR - async by protocol (AllocationManager)
        self, grant_id: str, status: str
    ) -> None:
        del grant_id, status
        _clearinghouse_admin_only("pauses or resumes a tenant")

    async def create_grant(  # NOSONAR - async by protocol (AllocationManager)
        self, name: str, amount: Money, metadata: Mapping[str, str] | None = None
    ) -> GrantRecord:
        del name, amount, metadata
        _clearinghouse_admin_only("onboards a tenant")

    async def list_grants(  # NOSONAR - async by protocol (AllocationManager)
        self,
    ) -> list[GrantRecord]:
        _clearinghouse_admin_only(_LISTS_EVERY_TENANT)

    async def grant_page(  # NOSONAR - async by protocol (AllocationManager)
        self, cursor: str | None
    ) -> GrantPage:
        del cursor
        _clearinghouse_admin_only(_LISTS_EVERY_TENANT)

    async def grant_balances(  # NOSONAR - async by protocol (AllocationManager)
        self,
    ) -> list[TenantGrant]:
        _clearinghouse_admin_only(_LISTS_EVERY_TENANT)

    # -- an allocation, with no actor named -----------------------------------------------------------

    async def create_key_for_grant(  # NOSONAR - async by protocol (AllocationManager)
        self, grant_id: str, name: str, amount: Money, idempotency_key: str
    ) -> NewAllocationKey:
        del grant_id, name, amount, idempotency_key
        _needs_the_actor("create_key_for_grant")

    async def create_key(  # NOSONAR - async by protocol (AllocationManager)
        self, allocation_id: str, name: str, idempotency_key: str
    ) -> NewAllocationKey:
        del allocation_id, name, idempotency_key
        _needs_the_actor("create_key")

    async def top_up_allocation(  # NOSONAR - async by protocol (AllocationManager)
        self, allocation_id: str, amount: Money, idempotency_key: str
    ) -> None:
        del allocation_id, amount, idempotency_key
        _needs_the_actor("top_up_allocation")

    async def set_allocation_status(  # NOSONAR - async by protocol (AllocationManager)
        self, allocation_id: str, status: str
    ) -> None:
        del allocation_id, status
        _needs_the_actor("set_allocation_status")

    async def revoke_allocation(  # NOSONAR - async by protocol (AllocationManager)
        self, allocation_id: str
    ) -> None:
        del allocation_id
        _needs_the_actor("revoke_allocation")

    async def read_allocation(  # NOSONAR - async by protocol (AllocationManager)
        self, allocation_id: str
    ) -> Allowance:
        del allocation_id
        _needs_the_actor("read_allocation")

    async def revoke_key(  # NOSONAR - async by protocol (AllocationManager); nothing is sent
        self, api_key_id: str
    ) -> None:
        """Nothing is sent: a rotation at the clearinghouse ends the old key in the same command."""
        del api_key_id


class _ActorAllocation(ClearinghouseAllocations):
    """The same manager for one actor: each member that names an allocation calls that actor's route."""

    def __init__(
        self, whole: ClearinghouseAllocations, wire: ClearinghouseWire, tenant_id: str, actor_id: str
    ) -> None:
        super().__init__(wire)
        self._whole = whole
        self._tenant_id = tenant_id
        self._actor_id = actor_id

    def _path(self, rest: str = "") -> str:
        own_tenant(self._wire, self._tenant_id)  # before any request about this actor
        return allocation_path(self._actor_id, rest)

    async def create_key_for_grant(
        self, grant_id: str, name: str, amount: Money, idempotency_key: str
    ) -> NewAllocationKey:
        """Allot. The engine's idempotency key is the route's ``operation_ref``: a repeat here is a
        repeat there. An actor with no label of its own is sent with none, so the clearinghouse names
        the allocation by its own default."""
        if grant_id != self._tenant_id:  # a tenant's id is its grant's id (D83)
            raise InvalidRequest("an allocation is allotted from its own tenant's grant")
        path = self._path()
        label = "" if name == f"{self._tenant_id}/{self._actor_id}" else name
        body = {"amount": _money(amount), "label": label, "operation_ref": idempotency_key}
        return await self._keyed(await self._wire.write_as(KeyedAllocation, path, body))

    async def create_key(self, allocation_id: str, name: str, idempotency_key: str) -> NewAllocationKey:
        """Rotate the payment key: one command at the clearinghouse, which ends the old key itself."""
        del name
        await self._held(allocation_id)
        body = {"operation_ref": idempotency_key}
        rotated = await self._wire.write_as(KeyedAllocation, self._path("/rotate-payment-key"), body)
        return await self._keyed(rotated)

    async def top_up_allocation(self, allocation_id: str, amount: Money, idempotency_key: str) -> None:
        await self._held(allocation_id)
        await self._wire.write(
            self._path("/top-up"), {"amount": _money(amount), "operation_ref": idempotency_key}
        )

    async def set_allocation_status(self, allocation_id: str, status: str) -> None:
        await self._held(allocation_id)
        await self._wire.write(self._path("/status"), {"status": status})

    async def revoke_allocation(self, allocation_id: str) -> None:
        """Repeat-safe, as at the provider. An actor that holds another allocation at the clearinghouse
        was revoked and allotted again there, so the one named has ended already: the other is not this
        command's to revoke."""
        held = await self._wire.read_as(AllocationRead, self._path())
        if held.allocation_id == allocation_id and held.status != "revoked":
            await self._wire.write(self._path("/revoke"))

    async def read_allocation(self, allocation_id: str) -> Allowance:
        return await read_allowance(self._wire, self._tenant_id, self._actor_id, allocation_id)

    async def _held(self, allocation_id: str) -> ActorAllocation:
        """The actor's allocation at the clearinghouse, when it is the one the engine names. The routes
        act on whichever allocation the actor holds there, so a command about another one is refused
        before it is sent, as the provider answers an allocation id it does not hold."""
        held = await self._wire.read_as(AllocationRead, self._path())
        if held.allocation_id != allocation_id:
            raise _not_held(self._actor_id, allocation_id)
        return held

    async def _keyed(self, answer: KeyedAllocation) -> NewAllocationKey:
        """The payment key of an allotment or a rotation, as the port's new key. When the answer is a
        replay, the key was shown to an answer that never arrived: it is rotated once more, under a new
        reference, and that rotation's key is returned. A rotation moves no money."""
        issued = answer.payment_key
        if issued is not None and issued.token is None:
            _LOG.warning(
                "payment key shown to a lost answer; rotating it",
                extra={
                    "event": "clearinghouse.payment_key_recovered",
                    "tenant_id": self._tenant_id,
                    "actor_id": self._actor_id,
                    "allocation_id": answer.allocation_id,
                },
            )
            body = {"operation_ref": f"lost-payment-key:{uuid.uuid4().hex}"}
            try:
                answer = await self._wire.write_as(KeyedAllocation, self._path("/rotate-payment-key"), body)
            except ProviderError as refused:
                if refused.status_code != 404:
                    raise
                raise ProviderError(
                    409, f"allocation {answer.allocation_id} is revoked at the clearinghouse"
                ) from refused
            issued = answer.payment_key
        if issued is None or issued.token is None:
            # No refusal: the allocation exists there. The reference stays held until this is put right.
            raise ProviderError(200, "the clearinghouse answered with no payment key for the actor")
        return NewAllocationKey(
            allocation_id=answer.allocation_id, api_key_id=answer.api_key_id, secret=SecretStr(issued.token)
        )


class ClearinghouseAllowances:
    """``AllowanceReader``. The allocation that pays for an actor is the engine's own record: it is the
    one whose payment key the engine holds. Its allowance is read at the clearinghouse."""

    def __init__(self, wire: ClearinghouseWire, actors: ActorRepo) -> None:
        self._wire = wire
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

    def __init__(self, wire: ClearinghouseWire) -> None:
        self._wire = wire

    async def page(self, cursor: str | None, limit: int = 1000) -> UsagePage:
        params = {"limit": str(limit)} | ({"cursor": cursor} if cursor else {})
        return await self._wire.read_as(UsagePage, USAGE_PATH, params)


class ClearinghouseCredentials:
    """``CredentialResolver``: the actor's payment key (``lpp_``), from the engine's own secret store.
    A stored secret that is no payment key is a Batteries key from when the engine ran self-hosted
    payments. It is never returned, so it is never sent to a clearinghouse."""

    def __init__(self, actors: ActorRepo, secrets: SecretStore) -> None:
        self._actors = actors
        self._secrets = secrets

    async def credential(self, actor: ActorContext) -> PaymentKey:
        allotted = await self._actors.get_actor(actor.tenant_id, actor.actor_id)
        if allotted is None or allotted.status == "revoked":
            raise NotProvisioned(f"actor {actor.actor_id!r} has no allocation; an admin must allot one")
        token = await self._secrets.get(allotted.secret_ref)
        if not token.startswith(PAYMENT_KEY_PREFIX):
            raise NotProvisioned(
                f"actor {actor.actor_id!r} holds no payment key of the clearinghouse: its allocation "
                "was allotted before this engine used one. An admin must allot it at the clearinghouse"
            )
        return PaymentKey(token=token, allocation_id=allotted.allocation_id)


@dataclass(frozen=True)
class ClearinghousePorts:
    """The four remote adapters over one wire. ``allocations`` is also the engine's grants manager: it
    reads the tenant's grant and refuses the rest."""

    allocations: ClearinghouseAllocations
    allowances: ClearinghouseAllowances
    usage: ClearinghouseUsage
    credentials: ClearinghouseCredentials

    @classmethod
    def over(cls, wire: ClearinghouseWire, actors: ActorRepo, secrets: SecretStore) -> "ClearinghousePorts":
        return cls(
            ClearinghouseAllocations(wire),
            ClearinghouseAllowances(wire, actors),
            ClearinghouseUsage(wire),
            ClearinghouseCredentials(actors, secrets),
        )


async def read_allowance(
    wire: ClearinghouseWire, tenant_id: str, actor_id: str, allocation_id: str
) -> Allowance:
    """The allowance of the allocation ``allocation_id``, which ``actor_id`` must hold now: read from the
    actor's allocation route, which answers it with the record. Another tenant's actor is refused (403)
    and never asked for. An allowance the clearinghouse could not read at its own provider (``null``) is
    ``ProviderUnavailable``: unknown, and never read as zero."""
    own_tenant(wire, tenant_id)
    read = await wire.read_as(AllocationRead, allocation_path(actor_id))
    if read.allocation_id != allocation_id:
        raise _not_held(actor_id, allocation_id)
    if read.allowance is None:
        raise ProviderUnavailable("the clearinghouse could not read the allowance at its payment provider")
    return read.allowance


def _not_held(actor_id: str, allocation_id: str) -> ProviderError:
    return ProviderError(
        404, f"the clearinghouse holds another allocation for actor {actor_id!r}, not {allocation_id!r}"
    )


def _clearinghouse_admin_only(what: str) -> NoReturn:
    raise AccessDenied(
        f"only a clearinghouse admin {what}, at the clearinghouse: this engine holds a tenant admin key"
    )


def _needs_the_actor(member: str) -> NoReturn:
    raise InvalidRequest(
        f"{member} needs the actor at a clearinghouse, whose routes name one: call it on "
        "for_actor(tenant_id, actor_id)"
    )
