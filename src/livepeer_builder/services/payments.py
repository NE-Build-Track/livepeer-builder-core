"""Payments: explicit allotment and the signer credential (docs/contracts/payments.md).

Each actor has its own allocation and key (``per_actor``, the only allocation mode: D82), created only
by an explicit admin command. An actor with no allocation fails *before payment* with NotProvisioned. There
is no shared key to fall back to and no automatic allotment.

An allocation is allotted from its tenant's grant, and a tenant's id is its grant's id (D83, #165): no
command takes a separate grant id. Every command names the tenant it acts on to the access policy, so a
tenant admin is confined to its own tenant there, before any lookup or provider call.

Every provider write that moves money carries an idempotency key derived from
(engine_id, operation, step), so a retried command never applies twice.
"""

from typing import Any

from pydantic import TypeAdapter

from livepeer_builder.contracts import (
    ActorAllocation,
    ActorContext,
    AllocationRead,
    AllocationStatus,
    AllotRequest,
    Allowance,
    Currency,
    DeliveredAllocation,
    KnownActor,
    Operation,
    PaymentKey,
    ResourceRef,
    TopUpRequest,
)
from livepeer_builder.errors import (
    AlreadyAllotted,
    InvalidRequest,
    NotFound,
    NotProvisioned,
    ProviderUnavailable,
)
from livepeer_builder.protocols import (
    AccessPolicy,
    ActorRepo,
    AllocationManager,
    AllowanceReader,
    Clock,
    SecretStore,
)
from livepeer_builder.services.actor_writes import ActorWrites
from livepeer_builder.services.allocation_delivery import DeliverySecrets, delivered
from livepeer_builder.services.allocation_read import for_a_builder, known, with_allowance
from livepeer_builder.services.grants import allotted_from_grant, idempotency_key, manager_for_command
from livepeer_builder.services.ledger import OperationLedger, RunningOperation
from livepeer_builder.services.payment_keys import BEGAN_ON, PREVIOUS_CUSTODY, PaymentKeys, recorded_secret
from livepeer_builder.services.tenant_usage import TenantUsage
from livepeer_builder.services.tenants import TenantDesk
from livepeer_builder.services.wallet import Wallet

_ACTOR = TypeAdapter(ActorAllocation)
_NOTHING = TypeAdapter(type(None))
_SENT_LABEL = "sent_label"  # in an allotment's record, from the moment it begins: the label its runs send


class PaymentsService:
    def __init__(
        self,
        *,
        store: ActorRepo,
        secrets: SecretStore,
        policy: AccessPolicy,
        clock: Clock,
        engine_id: str,
        ledger: OperationLedger,
        manager: AllocationManager | None = None,
        tenants: TenantDesk | None = None,
        usage: TenantUsage | None = None,
        wallet: Wallet | None = None,
        currency: Currency = "USD",
        allowances: AllowanceReader | None = None,
    ) -> None:
        self._store = store
        self._writes = ActorWrites(store, clock)
        self._secrets = secrets
        self._keys = PaymentKeys(secrets, self._writes)
        self._policy = policy
        self._clock = clock
        self._engine_id = engine_id
        self._ledger = ledger
        self._manager = manager
        self.tenants = tenants  # None: the tenant desk is off (no grants manager)
        self.usage = usage  # None: no source reads one tenant's usage (#166)
        self.wallet = wallet  # None: a part cannot report the escrow or probe the signer (#167)
        self._currency = currency
        self._allowances = allowances  # None: the engine's own records and its provider

    # -- delivering commands (D85 point 1, D87 point 1) ----------------------------------------------
    #
    # The work engine holds the key: it gets it once, in the answer. Each call has its own buffer and its
    # own PaymentKeys, passed to the same steps the admin commands run, so concurrent commands never share
    # a plaintext key and nothing on this service is swapped for the call.

    async def allot_for_delivery(self, admin: ActorContext, request: AllotRequest) -> DeliveredAllocation:
        secrets = DeliverySecrets()
        try:
            keys = PaymentKeys(self._secrets, self._writes, delivery=secrets)
            allocation = await self._allot_with(admin, request, keys)
            return await delivered(self._store, allocation, secrets)
        finally:
            secrets.clear()

    async def rotate_for_delivery(
        self, admin: ActorContext, tenant_id: str, actor_id: str, operation_ref: str
    ) -> DeliveredAllocation:
        secrets = DeliverySecrets()
        try:
            keys = PaymentKeys(self._secrets, self._writes, delivery=secrets)
            allocation = await self._rotate_with(admin, tenant_id, actor_id, operation_ref, keys)
            return await delivered(self._store, allocation, secrets)
        finally:
            secrets.clear()

    # -- CredentialResolver ---------------------------------------------------------------------

    async def credential(self, actor: ActorContext) -> PaymentKey:
        allotted = await self._store.get_actor(actor.tenant_id, actor.actor_id)
        if allotted is None or allotted.status == "revoked":
            raise NotProvisioned(f"actor {actor.actor_id!r} has no allocation; an admin must allot one")
        if allotted.credential_custody == "work_engine":  # never asked of the store (#186 review)
            raise NotProvisioned("allocation credential is held by the work engine")
        return PaymentKey(
            token=await self._secrets.get(allotted.secret_ref),
            allocation_id=allotted.allocation_id,
        )

    # -- AllowanceReader --------------------------------------------------------------------------

    async def allocation_for(self, tenant_id: str, actor_id: str) -> str | None:
        if self._allowances is not None:
            return await self._allowances.allocation_for(tenant_id, actor_id)
        allotted = await self._store.get_actor(tenant_id, actor_id)
        return allotted.allocation_id if allotted else None

    async def read_allowance(self, allocation_id: str) -> Allowance:
        if self._allowances is not None:
            return await self._allowances.read_allowance(allocation_id)
        return await self._require_manager().read_allocation(allocation_id)

    # -- admin commands (Operation.ADMIN_ALLOCATIONS) ---------------------------------------------

    async def allot(self, admin: ActorContext, request: AllotRequest) -> ActorAllocation:
        """Create the actor's allocation and key in one atomic provider call, on the ledger (D57).

        A repeat of the reference gets the first answer. An actor already allotted under another
        reference is ``AlreadyAllotted``: nothing is applied, the amount included. The row is written
        only where the actor is still as this command read it, so it never undoes a command that
        finished while it was at the provider (``PaymentKeys.overtaken``; #115, C4).
        """
        return await self._allot_with(admin, request, self._keys)

    async def _allot_with(
        self, admin: ActorContext, request: AllotRequest, keys: PaymentKeys
    ) -> ActorAllocation:
        """``allot``, keeping the key with ``keys``: the configured store's, or a delivering call's."""
        self._policy.check(
            admin, Operation.ADMIN_ALLOCATIONS, _allocation(request.tenant_id, request.actor_id)
        )
        if request.amount.currency != self._currency:
            raise InvalidRequest(f"amount must be in {self._currency}")
        # refused before the ledger: nothing was sent. A manager that keeps a ledger by reference is told it
        manager = await self._manager_for(request.tenant_id, request.actor_id, request.operation_ref)

        async def steps(run: RunningOperation) -> ActorAllocation:
            return await self._allot(run, admin, manager, request, keys)

        # The grant stays in what is hashed, as it was while a request named one (the fingerprint is a
        # format: tests/unit/test_hashed_forms.py). It is the tenant's id.
        target = request.model_dump(exclude={"operation_ref"}) | {"grant_id": request.tenant_id}

        async def as_recorded() -> dict[str, Any]:
            """The request as a release before #165 recorded it, when it named a grant: the grant the
            actor's record names. The repeat of such an allotment still matches its record and is
            answered from it; one that matches neither form is a conflict. Neither is sent anywhere."""
            actor = await self._store.get_actor(request.tenant_id, request.actor_id)
            return target | {"grant_id": request.tenant_id if actor is None else actor.grant_id}

        return await self._ledger.run(
            admin,
            "allot",
            request.operation_ref,
            target,
            steps,
            _ACTOR,
            context=keys.begins({_SENT_LABEL: await self._label(request)}),  # before any provider call
            earlier_request=as_recorded,
            resumable=keys.resumes,
        )

    async def _allot(
        self,
        run: RunningOperation,
        admin: ActorContext,
        manager: AllocationManager,
        request: AllotRequest,
        keys: PaymentKeys,
    ) -> ActorAllocation:
        existing = await self._store.get_actor(request.tenant_id, request.actor_id)
        if existing is not None and existing.status != "revoked":
            # The actor is this command's result only if it holds the key an earlier run of it got back (the
            # provider key is actor-derived, so another reference may have had that same key replayed).
            # Otherwise it is another reference's, or a second allotment after a revoke under a new key, and
            # this amount would be silently dropped.
            if run.context.get("new_secret_key_id") != existing.api_key_id:
                raise AlreadyAllotted(existing)
            # An earlier run wrote the actor and its answer was lost. A delivering resume sends that run's
            # provider call again, under the idempotency key it recorded: Batteries replays the original
            # success, the secret included, and never expires it (docs/references/batteries-api.md), so
            # nothing is allotted twice and the answer carries the key. It replays only the request that run
            # sent and refuses any other (409): the name sent was the label the record noted, which is the
            # label the actor was saved with (all a record from before the label was noted has).
            replayed = None
            sent_as = run.context.get("new_secret_sent_as")
            if keys.delivers(existing) and sent_as is not None:
                sent = request.model_copy(update={"label": run.context.get(_SENT_LABEL, existing.label)})
                replayed = await allotted_from_grant(manager, sent, sent_as)
            return await keys.shared(existing, recorded_secret(run), replayed)
        if existing is not None and existing.grant_id != request.tenant_id:
            # A record from before a tenant's id was its grant's id (#165). Its allocation was allotted
            # from another grant, and a new one is never allotted from a grant the record does not name.
            raise InvalidRequest(
                f"actor {request.actor_id!r} was allotted from grant {existing.grant_id!r}, which is not "
                f"tenant {request.tenant_id!r}'s: a tenant's id is its grant's id, and an allocation is "
                "never moved to another grant"
            )
        # A resume sends the label the record noted: the sign-in name may have changed, and Batteries refuses
        # another name under the first run's key for good. A record from before the note reads it again.
        label = run.context[_SENT_LABEL] if _SENT_LABEL in run.context else await self._label(request)
        request = request.model_copy(update={"label": label})
        # Actor-derived, so two references for one actor still meet at the provider. This text is hashed
        # into the provider's idempotency key, here and in the other commands: it keeps its first wording,
        # or a command resumed after an upgrade would apply twice (tests/unit/test_hashed_forms.py).
        operation = f"provision:{request.tenant_id}:{request.actor_id}"
        if existing is not None:  # allotting again after a revoke is a new operation
            operation += f":{existing.updated_at.isoformat()}"
        sent_as = idempotency_key(self._engine_id, operation, "api-key")
        await keys.require_writable(run, sent_as)
        key = await allotted_from_grant(manager, request, sent_as)
        new = await keys.stored(run, request.tenant_id, request.actor_id, key, sent_as, manager)
        now = self._clock.now()
        allotted = ActorAllocation(
            tenant_id=request.tenant_id,
            actor_id=request.actor_id,
            grant_id=request.tenant_id,
            allocation_id=key.allocation_id,
            api_key_id=key.api_key_id,
            secret_ref=new.ref,
            currency=self._currency,
            status="exhausted" if request.amount.amount == 0 else "active",
            created_at=now,
            updated_at=now,
            label=request.label,
            credential_custody=new.custody,  # the custody the secret was stored under, never a default
        )
        if not await self._writes.allotted(allotted, replacing=existing):
            current = await self._store.get_actor(request.tenant_id, request.actor_id)
            return await keys.overtaken(request.actor_id, current, key, new)
        return allotted

    async def _label(self, request: AllotRequest) -> str:
        """The label an allotment is saved with, and names its allocation by at the provider: the
        request's, else the name the person last signed in with (D48), else none."""
        seen = None if request.label else await self._store.get_sighting(request.tenant_id, request.actor_id)
        return request.label or (seen.name if seen is not None else "")

    # -- admin reads (Operation.ADMIN_READ, D46) ----------------------------------------------------

    async def list_actors(
        self,
        admin: ActorContext,
        *,
        tenant_id: str | None = None,
        status: AllocationStatus | None = None,
        allotted: bool | None = None,
        limit: int = 500,
    ) -> list[KnownActor]:
        """Every actor an operator can act on: the allotted ones (gap G2) and the people an identity
        provider signed in, allotted or not (D48). Every tenant's for an admin unless it names one;
        an auditor's own. ``status`` filters allocations, so it leaves out the unallotted."""
        scope = tenant_id or (None if admin.credential_kind == "clearinghouse_admin" else admin.tenant_id)
        resource = None if scope is None else ResourceRef(kind="actor", tenant_id=scope)
        self._policy.check(admin, Operation.ADMIN_READ, resource)
        allocations = {(a.tenant_id, a.actor_id): a for a in await self._store.list_actors(scope)}
        sightings = {(s.tenant_id, s.actor_id): s for s in await self._store.list_sightings(scope)}
        listed = [
            known(key, allocations.get(key), sightings.get(key))
            for key in sorted(allocations.keys() | sightings.keys())
        ]
        return [
            actor
            for actor in listed
            if (allotted is None or actor.allotted == allotted)
            and (status is None or (actor.allocation is not None and actor.allocation.status == status))
        ][:limit]

    async def get_actor(self, admin: ActorContext, tenant_id: str, actor_id: str) -> ActorAllocation:
        resource = ResourceRef(kind="actor", tenant_id=tenant_id, owner_actor_id=actor_id)
        self._policy.check(admin, Operation.ADMIN_READ, resource)  # before the lookup (D44)
        actor = await self._store.get_actor(tenant_id, actor_id)
        if actor is None:
            raise NotFound(f"actor {actor_id!r} has no allocation")
        return actor

    async def get_allocation(self, admin: ActorContext, tenant_id: str, actor_id: str) -> AllocationRead:
        """The actor's record with the amounts the provider reports for its allocation (#168)."""
        return await with_allowance(await self.get_actor(admin, tenant_id, actor_id), self.read_allowance)

    async def top_up(
        self, admin: ActorContext, tenant_id: str, actor_id: str, request: TopUpRequest
    ) -> ActorAllocation:
        self._policy.check(admin, Operation.ADMIN_ALLOCATIONS, _allocation(tenant_id, actor_id))
        actor = await self._require_actor(tenant_id, actor_id)
        if request.amount.currency != actor.currency or request.amount.amount <= 0:
            raise InvalidRequest(f"a top-up must be a positive amount in {actor.currency}")
        manager = await self._manager_for(tenant_id, actor_id)

        async def steps(_: RunningOperation) -> ActorAllocation:
            await manager.top_up_allocation(
                actor.allocation_id,
                request.amount,
                idempotency_key(
                    self._engine_id, f"fund:{actor.allocation_id}:{request.operation_ref}", "fund"
                ),
            )
            return await self._writes.topped_up(actor)  # exhausted becomes active; nothing else (#115, C4)

        target = {"tenant_id": tenant_id, "actor_id": actor_id, "amount": request.amount}
        return await self._ledger.run(admin, "top_up", request.operation_ref, target, steps, _ACTOR)

    async def top_up_grant(self, admin: ActorContext, tenant_id: str, request: TopUpRequest) -> None:
        """Add to a tenant's grant, whose id is the tenant's id. Only a clearinghouse admin enlarges a
        grant (``admin.tenants``): a tenant admin divides its own."""
        self._policy.check(admin, Operation.ADMIN_TENANTS, ResourceRef(kind="tenant", tenant_id=tenant_id))
        if request.amount.currency != self._currency or request.amount.amount <= 0:
            raise InvalidRequest(f"a top-up must be a positive amount in {self._currency}")
        manager = self._require_manager()

        async def steps(_: RunningOperation) -> None:
            await manager.top_up_grant(
                tenant_id,
                request.amount,
                idempotency_key(self._engine_id, f"fund-grant:{tenant_id}:{request.operation_ref}", "fund"),
            )

        target = {"grant_id": tenant_id, "amount": request.amount}  # hashed as before: the grant's id
        await self._ledger.run(admin, "top_up_grant", request.operation_ref, target, steps, _NOTHING)

    async def set_status(
        self, admin: ActorContext, tenant_id: str, actor_id: str, status: AllocationStatus
    ) -> ActorAllocation:
        self._policy.check(admin, Operation.ADMIN_ALLOCATIONS, _allocation(tenant_id, actor_id))
        if status not in ("active", "paused"):
            raise InvalidRequest("status must be active or paused; use revoke to revoke")
        actor = await self._require_actor(tenant_id, actor_id)
        manager = await self._manager_for(tenant_id, actor_id)
        await manager.set_allocation_status(actor.allocation_id, status)
        written = await self._writes.status(actor, status)
        if written is None:  # revoked while this command was at the provider: it stays revoked (#115, C4)
            raise NotFound(f"actor {actor_id!r} has no allocation")
        return written

    async def revoke(self, admin: ActorContext, tenant_id: str, actor_id: str) -> ActorAllocation:
        """Revoke the allocation (unused funds return to the grant) and forget the key.

        It takes no reference (D57): a repeat after it finished answers with the revoked actor and calls
        nothing, so a retry after a lost answer is not an error (D61). One cut short is still active here
        and runs again; Batteries' revoke is repeat-safe.

        Only a secret the configured store holds is deleted there, before the actor is marked revoked: a
        store that fails then leaves the actor active, and a retry deletes it. A credential the work engine
        holds is never sent to the store, so a store that refuses handles it never issued cannot keep a
        revoked allocation listed as active (#186 review)."""
        self._policy.check(admin, Operation.ADMIN_ALLOCATIONS, _allocation(tenant_id, actor_id))
        if (done := await self._store.get_actor(tenant_id, actor_id)) and done.status == "revoked":
            return done
        actor = await self._require_actor(tenant_id, actor_id)
        await (await self._manager_for(tenant_id, actor_id)).revoke_allocation(actor.allocation_id)
        await self._forget(actor)
        revoked = await self._writes.status(actor, "revoked")
        if revoked is None:  # another revoke wrote it first: answer as a repeat does
            stored = await self._store.get_actor(tenant_id, actor_id)
            if stored is not None and stored.allocation_id == actor.allocation_id:
                return stored
            # Allotted again since: the row is another allocation's, perhaps live. The answer is the
            # allocation this call revoked, never an active actor as the result of a revoke.
            return actor.model_copy(update={"status": "revoked", "updated_at": self._clock.now()})
        if revoked.secret_ref != actor.secret_ref:  # a rotation stored a new key meanwhile: forget it too
            await self._forget(revoked)  # by its own custody, which may differ from the first row's
        return revoked

    async def _forget(self, actor: ActorAllocation) -> None:
        """Delete the actor's secret where the configured store holds it; a work engine's is not ours."""
        if actor.credential_custody == "secret_store":
            await self._secrets.delete(actor.secret_ref)

    async def rotate_payment_key(
        self, admin: ActorContext, tenant_id: str, actor_id: str, operation_ref: str
    ) -> ActorAllocation:
        """New key on the same allocation, store it, then revoke the old key, on the ledger (D57).

        The old key and secret are recorded when the rotation begins, with the allocation, and every step
        reads them from there, never from the actor as it is now: a resume after the new key was stored
        still revokes the old one, a repeat never revokes the live key as "the old key" (B1), and a resume
        on an allocation allotted since is refused. The new key is written only over
        that old key: a rotation that another one overtook is refused (``PaymentKeys.rotated``; #115, C4).
        The old key's custody is recorded with it, so its secret is deleted where it is held."""
        return await self._rotate_with(admin, tenant_id, actor_id, operation_ref, self._keys)

    async def _rotate_with(
        self, admin: ActorContext, tenant_id: str, actor_id: str, operation_ref: str, keys: PaymentKeys
    ) -> ActorAllocation:
        """``rotate_payment_key``, keeping the key with ``keys``: the configured store's, or a delivering
        call's."""
        self._policy.check(admin, Operation.ADMIN_ALLOCATIONS, _allocation(tenant_id, actor_id))
        actor = await self._require_actor(tenant_id, actor_id)
        previous = {"previous_api_key_id": actor.api_key_id, "previous_secret_ref": actor.secret_ref.ref}
        previous[BEGAN_ON] = actor.allocation_id
        previous[PREVIOUS_CUSTODY] = actor.credential_custody
        manager = await self._manager_for(tenant_id, actor_id)

        async def steps(run: RunningOperation) -> ActorAllocation:
            return await self._rotate(run, manager, tenant_id, actor_id, operation_ref, keys)

        target = {"tenant_id": tenant_id, "actor_id": actor_id}
        return await self._ledger.run(
            admin,
            "rotate_payment_key",
            operation_ref,
            target,
            steps,
            _ACTOR,
            context=keys.begins(previous),
            resumable=keys.resumes,
        )

    async def _rotate(
        self,
        run: RunningOperation,
        manager: AllocationManager,
        tenant_id: str,
        actor_id: str,
        operation_ref: str,
        keys: PaymentKeys,
    ) -> ActorAllocation:
        keys.refuse_lost(run, actor_id)
        actor = await keys.began_on(run, await self._require_actor(tenant_id, actor_id))
        sent_as = idempotency_key(self._engine_id, f"rotate:{actor.allocation_id}:{operation_ref}", "api-key")
        await keys.require_writable(run, sent_as, actor)
        # the provider replays it on a resume, or the engine reads back the one it was handed once
        key = await keys.rotation_key(run, manager, actor, f"{tenant_id}/{actor_id}", sent_as)
        return await keys.rotated(run, manager, actor, key, sent_as)

    async def allowance(self, actor: ActorContext, actor_id: str | None = None) -> Allowance:
        target = actor_id or actor.actor_id
        self._policy.check(
            actor,
            Operation.COSTS_READ,
            ResourceRef(kind="allocation", tenant_id=actor.tenant_id, owner_actor_id=target),
        )
        allocation_id = await self.allocation_for(actor.tenant_id, target)
        if allocation_id is None:
            raise NotFound(f"no allocation pays for actor {target!r}")
        return await for_a_builder(self.read_allowance, allocation_id)

    async def _require_actor(self, tenant_id: str, actor_id: str) -> ActorAllocation:
        actor = await self._store.get_actor(tenant_id, actor_id)
        if actor is None or actor.status == "revoked":
            raise NotFound(f"actor {actor_id!r} has no allocation")
        return actor

    def _require_manager(self) -> AllocationManager:
        if self._manager is None:
            raise ProviderUnavailable("the payment provider's management API is not configured")
        return self._manager

    async def _manager_for(
        self, tenant_id: str, actor_id: str, operation_ref: str | None = None
    ) -> AllocationManager:
        """The manager for a command about one actor's allocation, made ready for it. Awaited before the
        command's record is begun: what the manager must first learn of its provider spends none of the
        ledger's budget, and its refusal leaves no record (``manager_for_command``, #165)."""
        return await manager_for_command(self._require_manager(), tenant_id, actor_id, operation_ref)


def _allocation(tenant_id: str, actor_id: str) -> ResourceRef:
    """What an allocation command acts on, for the policy: a tenant admin is confined by it (#165)."""
    return ResourceRef(kind="allocation", tenant_id=tenant_id, owner_actor_id=actor_id)
