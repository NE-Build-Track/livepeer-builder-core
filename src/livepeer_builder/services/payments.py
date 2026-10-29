"""Payments: allocation modes, explicit provisioning, and the signer credential (docs/contracts/payments.md).

- ``per_application``: one allocation and key, configured by the operator. No provisioning calls.
- ``per_actor``: one allocation and key per actor, created only by an explicit admin command.
  An unprovisioned actor fails *before payment* with NotProvisioned. There is no fallback to a
  shared key and no automatic provisioning.

Every provider write that moves money carries an idempotency key derived from
(engine_id, operation, step), so a retried command never applies twice.
"""

import base64
import hashlib
from datetime import datetime

from livepeer_builder.contracts import (
    ActorContext,
    AllocationMode,
    AllocationStatus,
    Allowance,
    Currency,
    FundRequest,
    GrantRecord,
    KnownActor,
    Money,
    Operation,
    PaymentCredential,
    ProvisionedActor,
    ProvisionRequest,
    ResourceRef,
    SecretRef,
    SeenActor,
)
from livepeer_builder.errors import InvalidRequest, NotFound, NotProvisioned, ProviderUnavailable
from livepeer_builder.protocols import AccessPolicy, ActorRepo, AllocationManager, Clock, SecretStore


def idempotency_key(engine_id: str, operation: str, step: str) -> str:
    """base64url(sha256(engine_id:operation:step)): 43 chars, inside Batteries' 256-char limit."""
    digest = hashlib.sha256(f"{engine_id}:{operation}:{step}".encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


class GrantDesk:
    """Listing and creating grants: an optional capability (decision 7 in the DI refactor plan).

    Present only when a grants manager exists (``LPB_BATTERIES_OPERATOR_TOKEN``, or one supplied).
    The engine's own token can fund grants but not list or create them, so without the operator
    credential the desk is absent rather than failing at first use.
    """

    def __init__(self, manager: AllocationManager, policy: AccessPolicy, currency: Currency) -> None:
        self._manager = manager
        self._policy = policy
        self._currency = currency

    async def list_grants(self, admin: ActorContext) -> list[GrantRecord]:
        # The provisioning catalog: grants fund every application and carry none, so an auditor bound
        # to one application never reads them (John, #53 review).
        self._policy.check(admin, Operation.ADMIN_ALLOCATIONS)
        return await self._manager.list_grants()

    async def create_grant(self, admin: ActorContext, name: str, amount: Money) -> GrantRecord:
        self._policy.check(admin, Operation.ADMIN_ALLOCATIONS)
        if not name.strip():
            raise InvalidRequest("a grant name is required")
        if amount.currency != self._currency or amount.amount <= 0:
            raise InvalidRequest(f"a grant needs a positive amount in {self._currency}")
        return await self._manager.create_grant(name.strip(), amount)


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
        manager: AllocationManager | None = None,
        grants: AllocationManager | None = None,
        currency: Currency = "USD",
        shared_secret: SecretRef | None = None,
        shared_allocation_id: str | None = None,
    ) -> None:
        self.mode = mode
        self._store = store
        self._secrets = secrets
        self._policy = policy
        self._clock = clock
        self._engine_id = engine_id
        self._manager = manager
        self.grants: GrantDesk | None = GrantDesk(grants, policy, currency) if grants is not None else None
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
        """Create the actor's allocation and key in one atomic provider call. Repeats return the original."""
        self._policy.check(admin, Operation.ADMIN_ALLOCATIONS)
        if self.mode != "per_actor":
            raise InvalidRequest("provisioning applies only in per_actor mode")
        if request.amount.currency != self._currency:
            raise InvalidRequest(f"amount must be in {self._currency}")
        existing = await self._store.get_actor(request.application_id, request.actor_id)
        if existing is not None and existing.status != "revoked":
            return existing
        grant_id = await self._resolved_grant(admin, request.grant_id)
        if not grant_id:
            raise InvalidRequest("choose a grant")
        manager = self._require_manager()
        if not request.label:  # a person who signed in is labelled with their name (D48)
            seen = await self._store.get_sighting(request.application_id, request.actor_id)
            if seen is not None and seen.name:
                request = request.model_copy(update={"label": seen.name})
        operation = f"provision:{request.application_id}:{request.actor_id}"
        if existing is not None:  # re-provisioning after a revoke is a new operation
            operation += f":{existing.updated_at.isoformat()}"
        key = await manager.create_key_for_grant(
            grant_id,
            request.label or f"{request.application_id}/{request.actor_id}",
            request.amount,
            idempotency_key(self._engine_id, operation, "api-key"),
        )
        ref = await self._secrets.put(
            _secret_name(request.application_id, request.actor_id), key.secret.get_secret_value()
        )
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
        await self._require_manager().fund_allocation(
            actor.allocation_id,
            request.amount,
            idempotency_key(self._engine_id, f"fund:{actor.allocation_id}:{request.operation_ref}", "fund"),
        )
        return await self._update_status(actor, "active" if actor.status == "exhausted" else actor.status)

    async def fund_grant(self, admin: ActorContext, request: FundRequest) -> None:
        """Add funds to the named grant, or to the grant of the caller's own allocation."""
        self._policy.check(admin, Operation.ADMIN_ALLOCATIONS)
        if request.amount.currency != self._currency or request.amount.amount <= 0:
            raise InvalidRequest(f"funding must be a positive amount in {self._currency}")
        grant_id = await self._resolved_grant(admin, request.grant_id)
        if not grant_id:
            raise InvalidRequest("choose a grant")
        await self._require_manager().fund_grant(
            grant_id,
            request.amount,
            idempotency_key(self._engine_id, f"fund-grant:{grant_id}:{request.operation_ref}", "fund"),
        )

    async def set_status(
        self, admin: ActorContext, application_id: str, actor_id: str, status: AllocationStatus
    ) -> ProvisionedActor:
        self._policy.check(admin, Operation.ADMIN_ALLOCATIONS)
        if status not in ("active", "paused"):
            raise InvalidRequest("status must be active or paused; use revoke to revoke")
        actor = await self._require_actor(application_id, actor_id)
        await self._require_manager().set_allocation_status(actor.allocation_id, status)
        return await self._update_status(actor, status)

    async def revoke(self, admin: ActorContext, application_id: str, actor_id: str) -> ProvisionedActor:
        """Revoke the allocation (unused funds return to the grant) and forget the key."""
        self._policy.check(admin, Operation.ADMIN_ALLOCATIONS)
        actor = await self._require_actor(application_id, actor_id)
        await self._require_manager().revoke_allocation(actor.allocation_id)
        await self._secrets.delete(actor.secret_ref)
        return await self._update_status(actor, "revoked")

    async def rotate_key(
        self, admin: ActorContext, application_id: str, actor_id: str, operation_ref: str
    ) -> ProvisionedActor:
        """New key on the same allocation, store it, then revoke the old key.

        A repeat with the same ``operation_ref`` gets the same key back from the provider's replay. The
        actor already holds it, so the rotation is done: revoking ``actor.api_key_id`` then would revoke
        the live key (B1, D57)."""
        self._policy.check(admin, Operation.ADMIN_ALLOCATIONS)
        actor = await self._require_actor(application_id, actor_id)
        manager = self._require_manager()
        key = await manager.create_key(
            actor.allocation_id,
            f"{application_id}/{actor_id}",
            idempotency_key(self._engine_id, f"rotate:{actor.allocation_id}:{operation_ref}", "api-key"),
        )
        if key.api_key_id == actor.api_key_id:
            return actor
        ref = await self._secrets.put(_secret_name(application_id, actor_id), key.secret.get_secret_value())
        rotated = actor.model_copy(
            update={"api_key_id": key.api_key_id, "secret_ref": ref, "updated_at": self._clock.now()}
        )
        await self._store.save_actor(rotated)
        await manager.revoke_key(actor.api_key_id)
        await self._secrets.delete(actor.secret_ref)
        return rotated

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
        return await self.read_allowance(allocation_id)

    async def _require_actor(self, application_id: str, actor_id: str) -> ProvisionedActor:
        actor = await self._store.get_actor(application_id, actor_id)
        if actor is None or actor.status == "revoked":
            raise NotFound(f"actor {actor_id!r} is not provisioned")
        return actor

    async def _update_status(self, actor: ProvisionedActor, status: AllocationStatus) -> ProvisionedActor:
        updated = actor.model_copy(update={"status": status, "updated_at": self._now()})
        await self._store.save_actor(updated)
        return updated

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

    def _require_manager(self) -> AllocationManager:
        if self._manager is None:
            raise ProviderUnavailable("the payment provider's management API is not configured")
        return self._manager

    def _now(self) -> datetime:
        return self._clock.now()


def _secret_name(application_id: str, actor_id: str) -> str:
    return f"lpg/{application_id}/{actor_id}"


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
