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
    Money,
    Operation,
    ProvisionedActor,
    ProvisionRequest,
    ResourceRef,
    SecretRef,
)
from livepeer_builder.errors import InvalidRequest, NotFound, NotProvisioned, ProviderUnavailable
from livepeer_builder.protocols import AccessPolicy, ActorRepo, AllocationManager, Clock, SecretStore


def idempotency_key(engine_id: str, operation: str, step: str) -> str:
    """base64url(sha256(engine_id:operation:step)): 43 chars, inside Batteries' 256-char limit."""
    digest = hashlib.sha256(f"{engine_id}:{operation}:{step}".encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


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
        grant_id: str | None = None,
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
        self._grants = grants or manager
        self._grant_id = grant_id
        self._currency = currency
        self._shared_secret = shared_secret
        self._shared_allocation_id = shared_allocation_id

    # -- CredentialResolver ---------------------------------------------------------------------

    async def credential(self, actor: ActorContext) -> str | None:
        if self.mode == "per_application":
            if self._shared_secret is None:
                return None  # unpaid (offchain) operation
            return await self._secrets.get(self._shared_secret)
        provisioned = await self._store.get_actor(actor.application_id, actor.actor_id)
        if provisioned is None or provisioned.status == "revoked":
            raise NotProvisioned(f"actor {actor.actor_id!r} has no allocation; an admin must provision it")
        return await self._secrets.get(provisioned.secret_ref)

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
        manager = self._require_manager()
        grant_id = request.grant_id or self._grant_id
        if not grant_id:
            raise ProviderUnavailable("choose a grant")
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
        )
        await self._store.save_actor(provisioned)
        return provisioned

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

    async def list_grants(self, admin: ActorContext) -> list[GrantRecord]:
        self._policy.check(admin, Operation.ADMIN_ALLOCATIONS)
        client = self._grants or self._require_manager()
        return await client.list_grants()

    async def create_grant(self, admin: ActorContext, name: str, amount: Money) -> GrantRecord:
        self._policy.check(admin, Operation.ADMIN_ALLOCATIONS)
        if not name.strip():
            raise InvalidRequest("a grant name is required")
        if amount.currency != self._currency or amount.amount <= 0:
            raise InvalidRequest(f"a grant needs a positive amount in {self._currency}")
        client = self._grants or self._require_manager()
        return await client.create_grant(name.strip(), amount)

    async def fund_grant(self, admin: ActorContext, request: FundRequest) -> None:
        """Add funds to the configured grant (``LPB_GRANT_ID``). An actor key cannot call this."""
        self._policy.check(admin, Operation.ADMIN_ALLOCATIONS)
        if request.amount.currency != self._currency or request.amount.amount <= 0:
            raise InvalidRequest(f"funding must be a positive amount in {self._currency}")
        manager = self._require_manager()
        if self._grant_id is None:
            raise ProviderUnavailable("no grant_id is configured for provisioning")
        await manager.fund_grant(
            self._grant_id,
            request.amount,
            f"fund-grant:{self._grant_id}:{request.operation_ref}",
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
        """New key on the same allocation, store it, then revoke the old key."""
        self._policy.check(admin, Operation.ADMIN_ALLOCATIONS)
        actor = await self._require_actor(application_id, actor_id)
        manager = self._require_manager()
        key = await manager.create_key(
            actor.allocation_id,
            f"{application_id}/{actor_id}",
            idempotency_key(self._engine_id, f"rotate:{actor.allocation_id}:{operation_ref}", "api-key"),
        )
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

    def _require_manager(self) -> AllocationManager:
        if self._manager is None:
            raise ProviderUnavailable("the payment provider's management API is not configured")
        return self._manager

    def _now(self) -> datetime:
        return self._clock.now()


def _secret_name(application_id: str, actor_id: str) -> str:
    return f"lpg/{application_id}/{actor_id}"
