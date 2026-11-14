"""Payments: allocation modes, explicit provisioning, and the signer credential (docs/contracts/payments.md).

- ``per_application``: one allocation and key, configured by the operator. No provisioning calls.
- ``per_actor``: one allocation and key per actor, created only by an explicit admin command.
  An unprovisioned actor fails *before payment* with NotProvisioned. There is no fallback to a
  shared key and no automatic provisioning.

Every provider write that moves money carries an idempotency key derived from
(engine_id, operation, step), so a retried command never applies twice.
"""

import logging

from pydantic import TypeAdapter

from livepeer_builder.contracts import (
    ActorContext,
    AllocationMode,
    AllocationStatus,
    Allowance,
    Currency,
    FundRequest,
    KnownActor,
    Operation,
    PaymentCredential,
    ProvisionedActor,
    ProvisionRequest,
    ResourceRef,
    SecretRef,
    SeenActor,
)
from livepeer_builder.errors import (
    AlreadyProvisioned,
    InvalidRequest,
    NotFound,
    NotProvisioned,
    ProviderError,
    ProviderUnavailable,
)
from livepeer_builder.protocols import (
    AccessPolicy,
    ActorRepo,
    AllocationManager,
    Clock,
    SecretStore,
    WritableSecretStore,
)
from livepeer_builder.services.actor_writes import ActorWrites
from livepeer_builder.services.grants import GrantDesk, idempotency_key
from livepeer_builder.services.ledger import OperationLedger, RunningOperation
from livepeer_builder.services.payment_keys import PaymentKeys
from livepeer_builder.utils import BODY_LIMIT

_LOG = logging.getLogger(__name__)
_ACTOR = TypeAdapter(ProvisionedActor)
_NOTHING = TypeAdapter(type(None))
_UNREADABLE = "the allowance could not be read"  # what a builder reads when the provider fails (D58)


class PaymentsService:
    def __init__(
        self,
        *,
        mode: AllocationMode,
        store: ActorRepo,
        secrets: SecretStore,
        policy: AccessPolicy,
        clock: Clock,
        engine_id: str,
        ledger: OperationLedger,
        manager: AllocationManager | None = None,
        grants: AllocationManager | None = None,
        currency: Currency = "USD",
        shared_secret: SecretRef | None = None,
        shared_allocation_id: str | None = None,
    ) -> None:
        self.mode = mode
        self._store = store
        self._writes = ActorWrites(store, clock)
        self._secrets = secrets
        self._keys = PaymentKeys(secrets, self._writes)
        self._policy = policy
        self._clock = clock
        self._engine_id = engine_id
        self._ledger = ledger
        self._manager = manager
        self.grants: GrantDesk | None = (
            GrantDesk(grants, policy, currency, ledger, engine_id) if grants is not None else None
        )
        self._currency = currency
        self._shared_secret = shared_secret
        self._shared_allocation_id = shared_allocation_id

    # -- CredentialResolver ---------------------------------------------------------------------

    async def credential(self, actor: ActorContext) -> PaymentCredential:
        if self.mode == "per_application":
            if self._shared_secret is None:
                return PaymentCredential(token=None, allocation_id=None)  # unpaid (offchain)
            return PaymentCredential(
                token=await self._secrets.get(self._shared_secret),
                allocation_id=self._shared_allocation_id,
            )
        provisioned = await self._store.get_actor(actor.application_id, actor.actor_id)
        if provisioned is None or provisioned.status == "revoked":
            raise NotProvisioned(f"actor {actor.actor_id!r} has no allocation; an admin must provision it")
        return PaymentCredential(
            token=await self._secrets.get(provisioned.secret_ref),
            allocation_id=provisioned.allocation_id,
        )

    # -- AllowanceReader --------------------------------------------------------------------------

    async def allocation_for(self, application_id: str, actor_id: str | None) -> str | None:
        if self.mode == "per_application" or actor_id is None:
            return self._shared_allocation_id
        provisioned = await self._store.get_actor(application_id, actor_id)
        return provisioned.allocation_id if provisioned else None

    async def read_allowance(self, allocation_id: str) -> Allowance:
        return await self._require_manager().read_allocation(allocation_id)

    # -- admin commands (Operation.ADMIN_ALLOCATIONS) ---------------------------------------------

    async def provision(self, admin: ActorContext, request: ProvisionRequest) -> ProvisionedActor:
        """Create the actor's allocation and key in one atomic provider call, on the ledger (D57).

        A repeat of the reference gets the first answer. An actor already provisioned under another
        reference is ``AlreadyProvisioned``: nothing is applied, the amount included.
        """
        self._policy.check(admin, Operation.ADMIN_ALLOCATIONS)
        if self.mode != "per_actor":
            raise InvalidRequest("provisioning applies only in per_actor mode")
        if request.amount.currency != self._currency:
            raise InvalidRequest(f"amount must be in {self._currency}")
        manager = self._require_manager()  # refused before the ledger: nothing was sent

        async def steps(run: RunningOperation) -> ProvisionedActor:
            return await self._provision(run, admin, manager, request)

        target = request.model_dump(exclude={"operation_ref"})
        return await self._ledger.run(admin, "provision", request.operation_ref, target, steps, _ACTOR)

    async def _provision(
        self,
        run: RunningOperation,
        admin: ActorContext,
        manager: AllocationManager,
        request: ProvisionRequest,
    ) -> ProvisionedActor:
        existing = await self._store.get_actor(request.application_id, request.actor_id)
        if existing is not None and existing.status != "revoked":
            # The actor is this command's result only if it holds the key an earlier run of it got back (the
            # provider key is actor-derived, so another reference may have had that same key replayed).
            # Otherwise it is another reference's, or a re-provision after a revoke under a new key, and
            # this amount would be silently dropped.
            if run.context.get("new_secret_key_id") != existing.api_key_id:
                raise AlreadyProvisioned(existing)
            own = SecretRef(ref=run.context["new_secret_ref"])
            if own != existing.secret_ref:  # another reference saved the actor with its own copy
                await self._secrets.delete(own)
            return existing
        grant_id = await self._resolved_grant(admin, request.grant_id)
        if not grant_id:
            raise InvalidRequest("choose a grant")
        if not request.label:  # a person who signed in is labelled with their name (D48)
            seen = await self._store.get_sighting(request.application_id, request.actor_id)
            if seen is not None and seen.name:
                request = request.model_copy(update={"label": seen.name})
        # actor-derived, so two references for one actor still meet at the provider
        operation = f"provision:{request.application_id}:{request.actor_id}"
        if existing is not None:  # re-provisioning after a revoke is a new operation
            operation += f":{existing.updated_at.isoformat()}"
        sent_as = idempotency_key(self._engine_id, operation, "api-key")
        await self._require_writable_secrets(run, sent_as)
        key = await manager.create_key_for_grant(
            grant_id,
            request.label or f"{request.application_id}/{request.actor_id}",
            request.amount,
            sent_as,
        )
        ref = await self._keys.stored(run, request.application_id, request.actor_id, key, sent_as)
        now = self._clock.now()
        provisioned = ProvisionedActor(
            application_id=request.application_id,
            actor_id=request.actor_id,
            grant_id=grant_id,
            allocation_id=key.allocation_id,
            api_key_id=key.api_key_id,
            secret_ref=ref,
            currency=self._currency,
            status="exhausted" if request.amount.amount == 0 else "active",
            created_at=now,
            updated_at=now,
            label=request.label,
        )
        await self._store.save_actor(provisioned)
        return provisioned

    # -- admin reads (Operation.ADMIN_READ, D46) ----------------------------------------------------

    async def list_actors(
        self,
        admin: ActorContext,
        *,
        application_id: str | None = None,
        status: AllocationStatus | None = None,
        provisioned: bool | None = None,
        limit: int = 500,
    ) -> list[KnownActor]:
        """Every actor an operator can act on: the provisioned ones (gap G2) and the people an identity
        provider signed in, provisioned or not (D48). Every application's for an admin unless it names one;
        an auditor's own. ``status`` filters allocations, so it leaves out the unprovisioned."""
        scope = application_id or (None if admin.credential_kind == "admin" else admin.application_id)
        resource = None if scope is None else ResourceRef(kind="actor", application_id=scope)
        self._policy.check(admin, Operation.ADMIN_READ, resource)
        allocations = {(a.application_id, a.actor_id): a for a in await self._store.list_actors(scope)}
        sightings = {(s.application_id, s.actor_id): s for s in await self._store.list_sightings(scope)}
        known = [
            _known(key, allocations.get(key), sightings.get(key))
            for key in sorted(allocations.keys() | sightings.keys())
        ]
        return [
            actor
            for actor in known
            if (provisioned is None or actor.provisioned == provisioned)
            and (status is None or (actor.allocation is not None and actor.allocation.status == status))
        ][:limit]

    async def get_actor(self, admin: ActorContext, application_id: str, actor_id: str) -> ProvisionedActor:
        resource = ResourceRef(kind="actor", application_id=application_id, owner_actor_id=actor_id)
        self._policy.check(admin, Operation.ADMIN_READ, resource)  # before the lookup (D44)
        actor = await self._store.get_actor(application_id, actor_id)
        if actor is None:
            raise NotFound(f"actor {actor_id!r} is not provisioned")
        return actor

    async def fund(
        self, admin: ActorContext, application_id: str, actor_id: str, request: FundRequest
    ) -> ProvisionedActor:
        self._policy.check(admin, Operation.ADMIN_ALLOCATIONS)
        actor = await self._require_actor(application_id, actor_id)
        if request.amount.currency != actor.currency or request.amount.amount <= 0:
            raise InvalidRequest(f"funding must be a positive amount in {actor.currency}")
        manager = self._require_manager()

        async def steps(_: RunningOperation) -> ProvisionedActor:
            await manager.fund_allocation(
                actor.allocation_id,
                request.amount,
                idempotency_key(
                    self._engine_id, f"fund:{actor.allocation_id}:{request.operation_ref}", "fund"
                ),
            )
            return await self._writes.funded(actor)  # exhausted becomes active; nothing else (#115, C4)

        target = {"application_id": application_id, "actor_id": actor_id, "amount": request.amount}
        return await self._ledger.run(admin, "fund", request.operation_ref, target, steps, _ACTOR)

    async def fund_grant(self, admin: ActorContext, request: FundRequest) -> None:
        """Add funds to the named grant, or to the grant of the caller's own allocation."""
        self._policy.check(admin, Operation.ADMIN_ALLOCATIONS)
        if request.amount.currency != self._currency or request.amount.amount <= 0:
            raise InvalidRequest(f"funding must be a positive amount in {self._currency}")
        grant_id = await self._resolved_grant(admin, request.grant_id)
        if not grant_id:
            raise InvalidRequest("choose a grant")
        manager = self._require_manager()

        async def steps(_: RunningOperation) -> None:
            await manager.fund_grant(
                grant_id,
                request.amount,
                idempotency_key(self._engine_id, f"fund-grant:{grant_id}:{request.operation_ref}", "fund"),
            )

        target = {"grant_id": grant_id, "amount": request.amount}
        await self._ledger.run(admin, "fund_grant", request.operation_ref, target, steps, _NOTHING)

    async def set_status(
        self, admin: ActorContext, application_id: str, actor_id: str, status: AllocationStatus
    ) -> ProvisionedActor:
        self._policy.check(admin, Operation.ADMIN_ALLOCATIONS)
        if status not in ("active", "paused"):
            raise InvalidRequest("status must be active or paused; use revoke to revoke")
        actor = await self._require_actor(application_id, actor_id)
        await self._require_manager().set_allocation_status(actor.allocation_id, status)
        written = await self._writes.status(actor, status)
        if written is None:  # revoked while this command was at the provider: it stays revoked (#115, C4)
            raise NotFound(f"actor {actor_id!r} is not provisioned")
        return written

    async def revoke(self, admin: ActorContext, application_id: str, actor_id: str) -> ProvisionedActor:
        """Revoke the allocation (unused funds return to the grant) and forget the key.

        It takes no reference (D57): a repeat after it finished answers with the revoked actor and calls
        nothing, so a retry after a lost answer is not an error (D61). One cut short is still active here
        and runs again; Batteries' revoke is repeat-safe."""
        self._policy.check(admin, Operation.ADMIN_ALLOCATIONS)
        if (done := await self._store.get_actor(application_id, actor_id)) and done.status == "revoked":
            return done
        actor = await self._require_actor(application_id, actor_id)
        await self._require_manager().revoke_allocation(actor.allocation_id)
        await self._secrets.delete(actor.secret_ref)
        revoked = await self._writes.status(actor, "revoked")
        if revoked is None:  # another revoke wrote it first: answer as a repeat does
            return await self._store.get_actor(application_id, actor_id) or actor
        if revoked.secret_ref != actor.secret_ref:  # a rotation stored a new key meanwhile: forget it too
            await self._secrets.delete(revoked.secret_ref)
        return revoked

    async def rotate_key(
        self, admin: ActorContext, application_id: str, actor_id: str, operation_ref: str
    ) -> ProvisionedActor:
        """New key on the same allocation, store it, then revoke the old key, on the ledger (D57).

        The old key and secret are recorded when the rotation begins, and every step reads them from there,
        never from the actor as it is now: a resume after the new key was stored still revokes the old one,
        and a repeat never revokes the live key as "the old key" (B1). The new key is written only over
        that old key: a rotation that another one overtook is refused (``PaymentKeys.rotated``; #115, C4)."""
        self._policy.check(admin, Operation.ADMIN_ALLOCATIONS)
        actor = await self._require_actor(application_id, actor_id)
        previous = {"previous_api_key_id": actor.api_key_id, "previous_secret_ref": actor.secret_ref.ref}
        manager = self._require_manager()

        async def steps(run: RunningOperation) -> ProvisionedActor:
            return await self._rotate(run, manager, application_id, actor_id, operation_ref)

        target = {"application_id": application_id, "actor_id": actor_id}
        return await self._ledger.run(
            admin, "rotate_payment_key", operation_ref, target, steps, _ACTOR, context=previous
        )

    async def _rotate(
        self,
        run: RunningOperation,
        manager: AllocationManager,
        application_id: str,
        actor_id: str,
        operation_ref: str,
    ) -> ProvisionedActor:
        self._keys.refuse_lost(run, actor_id)
        actor = await self._require_actor(application_id, actor_id)
        sent_as = idempotency_key(self._engine_id, f"rotate:{actor.allocation_id}:{operation_ref}", "api-key")
        await self._require_writable_secrets(run, sent_as)
        key = await manager.create_key(  # the provider replays it on a resume
            actor.allocation_id, f"{application_id}/{actor_id}", sent_as
        )
        return await self._keys.rotated(run, manager, actor, key, sent_as)

    async def allowance(self, actor: ActorContext, actor_id: str | None = None) -> Allowance:
        target = actor_id or actor.actor_id
        self._policy.check(
            actor,
            Operation.COSTS_READ,
            ResourceRef(kind="allocation", application_id=actor.application_id, owner_actor_id=target),
        )
        allocation_id = await self.allocation_for(actor.application_id, target)
        if allocation_id is None:
            raise NotFound(f"no allocation pays for actor {target!r}")
        try:
            return await self.read_allowance(allocation_id)
        except (ProviderError, ProviderUnavailable) as error:  # a builder's read: provider words to the log
            status = error.status_code if isinstance(error, ProviderError) else None
            extra = {"provider_status": status, "detail": str(error)[:BODY_LIMIT]}
            _LOG.warning("allowance read failed", extra={"event": "payments.allowance_failed", **extra})
            if status is None:  # httpx's text can name the provider's host (D58)
                raise ProviderUnavailable(f"{_UNREADABLE}: the provider is unavailable") from error
            raise ProviderError(status, _UNREADABLE) from error

    async def _require_actor(self, application_id: str, actor_id: str) -> ProvisionedActor:
        actor = await self._store.get_actor(application_id, actor_id)
        if actor is None or actor.status == "revoked":
            raise NotFound(f"actor {actor_id!r} is not provisioned")
        return actor

    async def _resolved_grant(self, actor: ActorContext, requested: str) -> str:
        """The grant on the request, or the grant of this caller's own allocation."""
        chosen = requested.strip()
        if chosen:
            return chosen
        if self.mode != "per_actor":
            return ""
        provisioned = await self._store.get_actor(actor.application_id, actor.actor_id)
        if provisioned is None or provisioned.status == "revoked" or not provisioned.grant_id:
            return ""
        return provisioned.grant_id

    async def _require_writable_secrets(self, run: RunningOperation, sent_as: str) -> None:
        """In the step that mints a key, just before the provider is called: a store that says it cannot
        keep the new key refuses the command (#112, C2). A store without the optional ``writable`` check
        is not asked. A run that already stored the key of this same provider operation (``sent_as``, the
        idempotency key about to be sent) has nothing more to store, so it is not refused: a repeat of a
        finished command never reaches this step, and a resume goes on to finish. A stored secret from
        another provider operation excuses nothing: a resume after a revoke or a re-provision sends a new
        one, whose key is not stored yet."""
        if "new_secret_ref" in run.context and run.context.get("new_secret_sent_as") == sent_as:
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

    def _require_manager(self) -> AllocationManager:
        if self._manager is None:
            raise ProviderUnavailable("the payment provider's management API is not configured")
        return self._manager


def _known(key: tuple[str, str], allocation: ProvisionedActor | None, seen: SeenActor | None) -> KnownActor:
    name = seen.name if seen is not None and seen.name else None
    return KnownActor(
        application_id=key[0],
        actor_id=key[1],
        label=(allocation.label if allocation is not None else "") or name or "",
        name=name if seen is not None else None,
        allocation=allocation,
        first_seen_at=seen.first_seen_at if seen is not None else None,
        last_seen_at=seen.last_seen_at if seen is not None else None,
    )
