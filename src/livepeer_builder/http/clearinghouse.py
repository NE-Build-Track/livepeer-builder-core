"""The clearinghouse half's routes, all under ``/v1/clearinghouse/tenants``: tenants and their grants,
a tenant's actors and their allocations (allot, top up, status, revoke, rotate the payment key), and a
tenant's access keys.

One unit, mounted by itself or beside the work set (D84). It is typed against ``ClearinghouseApi``, so it
cannot run a job. Routes are thin: each authenticates, then makes one engine call, and authorization
lives in the services' AccessPolicy checks: the path names the tenant, the service passes it to the
policy, and another tenant's admin is refused there (403) before any lookup or provider call (#165).

The set is two groups, each one function, so a deployment can serve them on different listeners (#167):

- ``mount_tenant_routes``: what a tenant's own admin or auditor calls, for its own tenant;
- ``mount_clearinghouse_admin_routes``: what only a clearinghouse admin does (onboarding, the tenant
  list, a grant's top-up and a tenant's status).

A tenant's id is its grant's id (D83): no route or body takes a separate grant id.
"""

from typing import Annotated

from fastapi import Depends, FastAPI, Path, Query, Response

from livepeer_builder.api import ClearinghouseApi
from livepeer_builder.contracts import (
    AccessKey,
    ActorAllocation,
    ActorContext,
    AllocationStatus,
    AllotRequest,
    IssuedKey,
    KeyedAllocation,
    KnownActor,
    OnboardedTenant,
    TenantGrant,
    TenantState,
    TopUpRequest,
)
from livepeer_builder.contracts.common import StoredText
from livepeer_builder.http.auth import actor_dependency
from livepeer_builder.http.bodies import (
    AllotBody,
    OnboardBody,
    RotateBody,
    RotateKeyBody,
    StatusBody,
    TenantKeyBody,
    TenantStatusBody,
)
from livepeer_builder.http.params import GrantTenantId, PathId
from livepeer_builder.http.replies import replay, replayed

TENANTS = "/v1/clearinghouse/tenants"
TENANT = TENANTS + "/{tenant_id}"
ALLOCATION = TENANT + "/actors/{actor_id}/allocation"
KEYS = TENANT + "/keys"


def mount_clearinghouse_routes(app: FastAPI, engine: ClearinghouseApi) -> None:
    """Add the clearinghouse half's routes to ``app``: both groups."""
    mount_tenant_routes(app, engine)
    mount_clearinghouse_admin_routes(app, engine)


def mount_clearinghouse_admin_routes(app: FastAPI, engine: ClearinghouseApi) -> None:
    """Add what only a clearinghouse admin does: onboarding, the tenant list, a grant's top-up and a
    tenant's status. Onboarding, the list and the status need the tenant desk."""
    Actor = Annotated[ActorContext, Depends(actor_dependency(engine))]

    @app.post(TENANT + "/grant/top-up", status_code=204)
    async def top_up_grant(actor: Actor, tenant_id: GrantTenantId, body: TopUpRequest) -> Response:
        await engine.payments.top_up_grant(actor, tenant_id, body)
        return Response(status_code=204, headers=replayed())

    if engine.payments.tenants is None:  # the tenant desk is off: none of its routes (decision 7)
        return
    desk = engine.payments.tenants

    @app.post(TENANTS, status_code=201)
    async def onboard_tenant(actor: Actor, body: OnboardBody, response: Response) -> OnboardedTenant:
        """Create the tenant's grant and its first tenant admin key, whose token is shown once."""
        onboarded = await desk.onboard(
            actor,
            body.name,
            body.amount,
            operation_ref=body.operation_ref,
            admin_actor_id=body.admin_actor_id,
        )
        return replay(response, onboarded)

    @app.get(TENANTS)
    async def list_tenants(actor: Actor) -> list[TenantGrant]:
        return await desk.list_tenants(actor)

    @app.post(TENANT + "/status")
    async def set_tenant_status(
        actor: Actor, tenant_id: GrantTenantId, body: TenantStatusBody, response: Response
    ) -> TenantState:
        """Pause or resume the tenant by pausing or resuming its grant."""
        state = await desk.set_status(actor, tenant_id, body.status, operation_ref=body.operation_ref)
        return replay(response, state)


def mount_tenant_routes(app: FastAPI, engine: ClearinghouseApi) -> None:
    """Add what a tenant's admin or auditor calls for its own tenant: its grant, its actors and their
    allocations, and its access keys. A clearinghouse admin calls them for any tenant."""
    Actor = Annotated[ActorContext, Depends(actor_dependency(engine))]

    if engine.payments.tenants is not None:  # reading a grant needs the tenant desk
        desk = engine.payments.tenants

        @app.get(TENANT + "/grant")
        async def get_grant(actor: Actor, tenant_id: GrantTenantId) -> TenantGrant:
            """The grant's total, its unallocated balance and its status."""
            return await desk.grant(actor, tenant_id)

    @app.get(TENANT + "/actors")
    async def list_actors(
        actor: Actor,
        tenant_id: PathId,
        status: AllocationStatus | None = None,
        allotted: bool | None = None,
        limit: Annotated[int, Query(ge=1, le=1000)] = 500,
    ) -> list[KnownActor]:
        """The tenant's allotted actors and the people an identity provider signed in (D46, D48)."""
        return await engine.payments.list_actors(
            actor, tenant_id=tenant_id, status=status, allotted=allotted, limit=limit
        )

    @app.get(ALLOCATION)
    async def get_actor(actor: Actor, tenant_id: PathId, actor_id: PathId) -> ActorAllocation:
        return await engine.payments.get_actor(actor, tenant_id, actor_id)

    @app.post(ALLOCATION, status_code=201)
    async def allot(
        actor: Actor,
        tenant_id: Annotated[StoredText, Path()],  # as stored: a 422 here, before money moves
        actor_id: Annotated[StoredText, Path()],
        body: AllotBody,
        response: Response,
    ) -> KeyedAllocation:
        """Allot the actor's allocation from the tenant's grant. The answer carries the actor's payment
        key, whose token is shown once."""
        request = AllotRequest(
            tenant_id=tenant_id,
            actor_id=actor_id,
            amount=body.amount,
            label=body.label,
            operation_ref=body.operation_ref,
        )
        return replay(response, await engine.payments.allot(actor, request))

    @app.post(ALLOCATION + "/top-up")
    async def top_up(
        actor: Actor, tenant_id: PathId, actor_id: PathId, body: TopUpRequest, response: Response
    ) -> ActorAllocation:
        return replay(response, await engine.payments.top_up(actor, tenant_id, actor_id, body))

    @app.post(ALLOCATION + "/status")
    async def set_status(
        actor: Actor, tenant_id: PathId, actor_id: PathId, body: StatusBody
    ) -> ActorAllocation:
        return await engine.payments.set_status(actor, tenant_id, actor_id, body.status)

    @app.post(ALLOCATION + "/revoke")
    async def revoke(actor: Actor, tenant_id: PathId, actor_id: PathId) -> ActorAllocation:
        return await engine.payments.revoke(actor, tenant_id, actor_id)

    @app.post(ALLOCATION + "/rotate-payment-key")
    async def rotate_payment_key(
        actor: Actor, tenant_id: PathId, actor_id: PathId, body: RotateBody, response: Response
    ) -> KeyedAllocation:
        """Replace the actor's payment key, and the Batteries key behind it. The old one stops at once."""
        rotated = await engine.payments.rotate_payment_key(actor, tenant_id, actor_id, body.operation_ref)
        return replay(response, rotated)

    if engine.keys is None:  # API keys off: a tenant has no access keys to manage (decision 2)
        return
    keys = engine.keys

    @app.post(KEYS, status_code=201)
    async def issue_tenant_key(
        actor: Actor, tenant_id: PathId, body: TenantKeyBody, response: Response
    ) -> IssuedKey:
        """Issue an auditor key for the tenant, or, as a clearinghouse admin, another tenant admin key."""
        issued = await keys.issue(
            actor,
            kind=body.kind,
            actor_id=body.actor_id,
            tenant_id=tenant_id,
            label=body.label,
            operation_ref=body.operation_ref,
        )
        return replay(response, issued)

    @app.get(KEYS)
    async def list_tenant_keys(actor: Actor, tenant_id: PathId) -> list[AccessKey]:
        return await keys.list_keys(actor, tenant_id)

    @app.get(KEYS + "/{key_id}")
    async def get_tenant_key(actor: Actor, tenant_id: PathId, key_id: PathId) -> AccessKey:
        return await keys.get(actor, key_id, tenant_id)

    @app.post(KEYS + "/{key_id}/rotate", status_code=201)
    async def rotate_tenant_key(
        actor: Actor, tenant_id: PathId, key_id: PathId, body: RotateKeyBody, response: Response
    ) -> IssuedKey:
        rotated = await keys.rotate(
            actor, key_id, label=body.label, operation_ref=body.operation_ref, tenant_id=tenant_id
        )
        return replay(response, rotated)

    @app.post(KEYS + "/{key_id}/revoke", status_code=204)
    async def revoke_tenant_key(actor: Actor, tenant_id: PathId, key_id: PathId) -> Response:
        await keys.revoke(actor, key_id, tenant_id)
        return Response(status_code=204)
