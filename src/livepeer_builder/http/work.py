"""The work half's routes: the catalog, runners, jobs and their costs, the allowance read, the reserved
events and sessions, and what an admin of the engine does there (its own access keys, reconciliation and
cost sync).

One unit, mounted by itself or beside the clearinghouse set (D84). It is typed against ``WorkApi``, so it
cannot call a clearinghouse command. Routes are thin: each authenticates, then makes one engine call, and
authorization lives in the services' AccessPolicy checks. ``GET /v1/offerings`` is public. Reserved
contracts (sessions, events) answer 501. The result route and the cancel route of an async job are in
``http/job_handles.py``, mounted here with the rest.
"""

from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, Query, Request, Response

from livepeer_builder.api import WorkApi
from livepeer_builder.contracts import (
    AccessKey,
    ActorContext,
    Allowance,
    CostSummary,
    EventPage,
    IssuedKey,
    Job,
    JobCost,
    Offering,
    Reconciliation,
    Runner,
    Session,
    SessionRequest,
    SyncReport,
)
from livepeer_builder.contracts.common import StoredText
from livepeer_builder.errors import InvalidRequest
from livepeer_builder.http.auth import actor_dependency
from livepeer_builder.http.bodies import IssueKeyBody, JobRequest, RotateKeyBody, UsageBody
from livepeer_builder.http.job_handles import POLL_AFTER_S, add_job_handle_routes, submitted
from livepeer_builder.http.params import PathId, QueryId, QueryInstant, QueryTime
from livepeer_builder.http.replies import JOB_ID_HEADER, replay
from livepeer_builder.http.streaming import stream_response


def mount_work_routes(app: FastAPI, engine: WorkApi) -> None:
    """Add the work half's routes to ``app``."""
    Actor = Annotated[ActorContext, Depends(actor_dependency(engine))]

    @app.get("/v1/offerings", response_model=list[Offering])
    async def offerings(app_name: Annotated[StoredText | None, Query(alias="app")] = None) -> list[Offering]:
        # Public (#5): the catalog names no runner URL, starts no work and spends nothing.
        return engine.discovery.offerings(app=app_name)

    @app.get("/v1/runners", response_model=list[Runner])
    async def runners(actor: Actor, app_name: Annotated[StoredText, Query(alias="app")]) -> list[Runner]:
        # App names contain "/" (livepeer-example/hello), so they travel as a query parameter.
        return engine.discovery.runners(actor, app_name)

    @app.post(
        "/v1/jobs",
        responses={
            200: {
                "description": "The runner's own status, content type and body. With "
                "`Accept: text/event-stream` or `?stream=true` the body is streamed as it arrives "
                '(docs/contracts/engine.md#streaming). For `mode="async"`, the job a repeated '
                "`operation_ref` already named, with `Idempotent-Replayed: true`."
            },
            202: {
                "model": Job,
                "description": '`mode="async"`: the job, queued. `Location` and `Livepeer-Job-Id` '
                "name it; read it and its result back by id (docs/contracts/engine.md#async-jobs).",
            },
        },
    )
    async def run_job(
        actor: Actor,
        body: JobRequest,
        request: Request,
        stream: Annotated[bool, Query(description="stream the runner's response as it arrives")] = False,
    ) -> Response:
        streamed = stream or "text/event-stream" in request.headers.get("accept", "")
        if body.mode == "async":  # queued: the answer is the job, and its result is read back later
            if streamed:
                raise InvalidRequest("an async job is not streamed: read its result once it has ended")
            return await submitted(engine, actor, body)
        if streamed:
            streaming = engine.jobs.streaming
            if streaming is None:
                raise InvalidRequest("the configured runner transport does not support streaming")
            opened = await streaming.stream(actor, body)
            return await stream_response(
                opened, {JOB_ID_HEADER: str(opened.job.id), "Cache-Control": "no-cache"}
            )
        result = await engine.jobs.run(actor, body)
        return Response(  # the runner's content type as a header: media_type would add a charset (C27)
            content=result.content,
            status_code=result.status_code,
            headers={"Content-Type": result.content_type, JOB_ID_HEADER: str(result.job.id)},
        )

    @app.get("/v1/jobs", response_model=list[Job])
    async def list_jobs(
        actor: Actor, limit: Annotated[int, Query(ge=1, le=500)] = 50, before: QueryInstant = None
    ) -> list[Job]:
        """Newest first; ``before`` (an ISO-8601 time with its offset) is the last page's oldest
        ``created_at``, for the next page (D59)."""
        return list(await engine.jobs.list_jobs(actor, limit=limit, before=before))

    @app.get("/v1/jobs/{job_id}", response_model=Job)
    async def get_job(actor: Actor, job_id: UUID, response: Response) -> Job:
        job = await engine.jobs.get(actor, job_id)
        if job.state in ("queued", "running"):  # not ended: when to read it again (ISS-26)
            response.headers["Retry-After"] = POLL_AFTER_S
        return job

    add_job_handle_routes(app, engine)  # the stored result, and the cancel of a queued job

    @app.post("/v1/jobs/{job_id}/usage", response_model=Job)
    async def report_usage(actor: Actor, job_id: UUID, body: UsageBody) -> Job:
        return await engine.jobs.report_usage(actor, job_id, body.quantities)

    @app.get("/v1/jobs/{job_id}/cost", response_model=JobCost)
    async def job_cost(actor: Actor, job_id: UUID) -> JobCost:
        return await engine.costs.for_job(actor, job_id)

    @app.get("/v1/costs/projections", response_model=list[JobCost])
    async def cost_projections(actor: Actor, limit: Annotated[int, Query(ge=1, le=25)] = 25) -> list[JobCost]:
        return await engine.costs.recent(actor, limit)

    @app.get("/v1/costs/summary", response_model=CostSummary)
    async def cost_summary(
        actor: Actor,
        start: QueryTime = None,
        end: QueryTime = None,
        actor_id: QueryId = None,
    ) -> CostSummary:
        return await engine.costs.summary(actor, start, end, actor_id)  # the engine's clock fills the window

    @app.get("/v1/costs/reconciliation", response_model=Reconciliation)
    async def reconcile(
        actor: Actor,
        tenant_id: Annotated[StoredText, Query()],  # required (#163 review): never a tenant unnamed
        actor_id: QueryId = None,
        # an earlier allocation of the actor; absent: the current one, and empty is a 422, not "absent"
        allocation_id: Annotated[StoredText | None, Query(min_length=1)] = None,
    ) -> Reconciliation:
        return await engine.costs.reconcile(actor, tenant_id, actor_id, allocation_id)

    @app.post("/v1/costs/sync", response_model=SyncReport)
    async def sync_costs(actor: Actor) -> SyncReport:
        return await engine.costs.sync_now(actor)

    @app.get("/v1/allowance", response_model=Allowance)
    async def allowance(actor: Actor, actor_id: QueryId = None) -> Allowance:
        return await engine.payments.allowance(actor, actor_id)

    @app.get("/v1/events", response_model=EventPage)
    async def events(
        actor: Actor, after: int = 0, limit: Annotated[int, Query(ge=1, le=1000)] = 500
    ) -> EventPage:
        return await engine.events.after(actor, after, limit)

    @app.post("/v1/sessions", response_model=Session)
    async def open_session(actor: Actor, body: SessionRequest) -> Session:
        return await engine.sessions.open(actor, body)

    if engine.keys is not None:  # API keys off: the key routes do not exist (decision 2)
        keys = engine.keys

        @app.post("/v1/keys", response_model=IssuedKey, status_code=201)
        async def issue_key(actor: Actor, body: IssueKeyBody, response: Response) -> IssuedKey:
            issued = await keys.issue(
                actor,
                kind=body.kind,
                actor_id=body.actor_id,
                tenant_id=body.tenant_id,
                permissions=body.permissions,
                label=body.label,
                operation_ref=body.operation_ref,
            )
            return replay(response, issued)

        @app.get("/v1/keys", response_model=list[AccessKey])
        async def list_keys(actor: Actor, tenant_id: QueryId = None) -> list[AccessKey]:
            return await keys.list_keys(actor, tenant_id)

        @app.get("/v1/keys/{key_id}", response_model=AccessKey)
        async def get_key(actor: Actor, key_id: PathId) -> AccessKey:
            return await keys.get(actor, key_id)

        @app.post("/v1/keys/{key_id}/rotate", response_model=IssuedKey, status_code=201)
        async def rotate_access_key(
            actor: Actor, key_id: PathId, body: RotateKeyBody, response: Response
        ) -> IssuedKey:
            rotated = await keys.rotate(actor, key_id, label=body.label, operation_ref=body.operation_ref)
            return replay(response, rotated)

        @app.post("/v1/keys/{key_id}/revoke", status_code=204)
        async def revoke_key(actor: Actor, key_id: PathId) -> Response:
            await keys.revoke(actor, key_id)
            return Response(status_code=204)
