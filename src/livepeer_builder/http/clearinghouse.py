"""The clearinghouse half's routes. Under ``/v1/clearinghouse/tenants``: tenants and their grants, a
tenant's actors and their allocations (allot, top up, status, revoke, rotate the payment key), a tenant's
access keys and its usage. Beside them, ``/v1/clearinghouse/discovery``: the runner list the clearinghouse
publishes (#166).

One unit, mounted by itself or beside the work set (D84). It is typed against ``ClearinghouseApi``, so it
cannot run a job. Routes are thin: each authenticates, then makes one engine call, and authorization
lives in the services' AccessPolicy checks: the path names the tenant, the service passes it to the
policy, and another tenant's admin is refused there (403) before any lookup or provider call (#165).

The set is two groups, each one function, so a deployment can serve them on different listeners (#167):

- ``mount_tenant_routes``: what a tenant's own admin or auditor calls, for its own tenant, and the
  runner list, which any of them reads;
- ``mount_clearinghouse_admin_routes``: what only a clearinghouse admin does (onboarding, the tenant
  list, a grant's top-up and a tenant's status).

A tenant's id is its grant's id (D83): no route or body takes a separate grant id.
"""

from typing import Annotated

from fastapi import Depends, FastAPI, Path, Query, Response
from fastapi.responses import JSONResponse

from livepeer_builder.api import ClearinghouseApi
from livepeer_builder.contracts import (
    AccessKey,
    ActorAllocation,
    ActorContext,
    AllocationStatus,
    AllotRequest,
    IssuedKey,
    KnownActor,
    OnboardedTenant,
    TenantGrant,
    TenantState,
    TopUpRequest,
    UsagePage,
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
from livepeer_builder.http.delivered import DeliveredAllocationBody, delivered_body
from livepeer_builder.http.params import GrantTenantId, PathId, QueryId
from livepeer_builder.http.replies import replay, replayed

TENANTS = "/v1/clearinghouse/tenants"
TENANT = TENANTS + "/{tenant_id}"
ALLOCATION = TENANT + "/actors/{actor_id}/allocation"
KEYS = TENANT + "/keys"
DISCOVERY = "/v1/clearinghouse/discovery"


def mount_clearinghouse_routes(
    app: FastAPI, engine: ClearinghouseApi, *, deliver_allocation_keys: bool = True
) -> None:
    """Mount both groups. Standalone clearinghouses deliver allocation credentials to work engines.

    The combined self-hosted app passes False because its own work engine owns the secret store.
    Listener/role composition must preserve that distinction.
    """
    mount_tenant_routes(app, engine, deliver_allocation_keys=deliver_allocation_keys)
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


def mount_tenant_routes(
    app: FastAPI, engine: ClearinghouseApi, *, deliver_allocation_keys: bool = True
) -> None:
    """Add what a tenant's admin or auditor calls for its own tenant: its grant, its actors and their
    allocations, its usage and its access keys. A clearinghouse admin calls them for any tenant. With
    them, the clearinghouse's runner list, which any credential of a tenant reads."""
    Actor = Annotated[ActorContext, Depends(actor_dependency(engine))]

    if engine.payments.tenants is not None:  # reading a grant needs the tenant desk
        desk = engine.payments.tenants

        @app.get(TENANT + "/grant")
        async def get_grant(actor: Actor, tenant_id: GrantTenantId) -> TenantGrant:
            """The grant's total, its unallocated balance and its status."""
            return await desk.grant(actor, tenant_id)

    if engine.runner_list is not None:  # needs a discovery source that holds the document it read
        runner_list = engine.runner_list

        @app.get(
            DISCOVERY,
            responses={
                200: {"description": "The livepeer/node `/discovery` document the clearinghouse last read."},
                503: {"description": "The clearinghouse has read no runner list yet."},
            },
        )
        async def discovery(actor: Actor) -> Response:
            """The clearinghouse's runner list, in livepeer/node's discovery format: what an enterprise's
            engine reads as its ``LPB_DISCOVERY_URL``. The document is served as the clearinghouse read
            it. Any credential of a tenant may read it."""
            document = runner_list.document(actor)
            if document is None:  # never an empty list for an unknown one: a reader would empty its catalog
                unread = {"error": "the clearinghouse has read no runner list yet"}
                return JSONResponse(unread, status_code=503, headers={"Retry-After": "5"})
            return Response(content=document, media_type="application/json")

    if engine.payments.usage is not None:  # needs a source that reads one tenant's usage
        usage = engine.payments.usage

        @app.get(TENANT + "/usage")
        async def tenant_usage(
            actor: Actor,
            tenant_id: GrantTenantId,
            cursor: QueryId = None,
            limit: Annotated[int, Query(ge=1, le=1000)] = 1000,
        ) -> UsagePage:
            """One page of the tenant's payment records, oldest first: the payment provider's usage rows
            for the tenant's grant, and none of another tenant's. ``cursor`` is the last page's
            ``next_cursor``, which is empty on the last page."""
            return await usage.page(actor, tenant_id, cursor, limit)

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

    @app.post(
        ALLOCATION,
        status_code=201,
        response_model=DeliveredAllocationBody if deliver_allocation_keys else ActorAllocation,
    )
    async def allot(
        actor: Actor,
        tenant_id: Annotated[StoredText, Path()],  # as stored: a 422 here, before money moves
        actor_id: Annotated[StoredText, Path()],
        body: AllotBody,
        response: Response,
    ) -> ActorAllocation:
        response.headers["Cache-Control"] = "no-store"
        request = AllotRequest(
            tenant_id=tenant_id,
            actor_id=actor_id,
            amount=body.amount,
            label=body.label,
            operation_ref=body.operation_ref,
        )
        if deliver_allocation_keys:  # the key is written out here, and only here
            return replay(response, delivered_body(await engine.payments.allot_for_delivery(actor, request)))
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

    @app.post(
        ALLOCATION + "/rotate-payment-key",
        response_model=DeliveredAllocationBody if deliver_allocation_keys else ActorAllocation,
    )
    async def rotate_payment_key(
        actor: Actor, tenant_id: PathId, actor_id: PathId, body: RotateBody, response: Response
    ) -> ActorAllocation:
        response.headers["Cache-Control"] = "no-store"
        ref = body.operation_ref
        if deliver_allocation_keys:  # the key is written out here, and only here
            rotated = await engine.payments.rotate_for_delivery(actor, tenant_id, actor_id, ref)
            return replay(response, delivered_body(rotated))
        return replay(response, await engine.payments.rotate_payment_key(actor, tenant_id, actor_id, ref))

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
