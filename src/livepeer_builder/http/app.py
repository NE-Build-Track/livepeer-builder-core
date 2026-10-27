"""REST adapter (the ``[server]`` extra). Each route authenticates, then makes one engine call.
Health and the catalog (``GET /v1/offerings``) are public.

Routes are thin: authorization lives in the services' AccessPolicy checks, not here. Reserved
contracts (async jobs, sessions, events) answer 501. The generated schema is committed at
docs/generated/openapi.json and checked for drift in CI.
"""

from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, FastAPI, Query, Request, Response, Security
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from livepeer_builder.api import EngineApi
from livepeer_builder.contracts import (
    AccessKey,
    ActorContext,
    AllocationStatus,
    Allowance,
    AuthRequest,
    CostSummary,
    CredentialKind,
    EngineHealth,
    EventPage,
    FundRequest,
    GrantRecord,
    IssuedKey,
    Job,
    JobCost,
    JobRequest,
    Money,
    Offering,
    ProvisionedActor,
    ProvisionRequest,
    Reconciliation,
    Runner,
    Session,
    SessionRequest,
    SyncReport,
)
from livepeer_builder.errors import (
    AccessDenied,
    EngineError,
    InvalidRequest,
    JobFailed,
    NoCapacity,
    NotAuthenticated,
    NotFound,
    NotImplementedInMilestone,
    NotProvisioned,
    OperationConflict,
    OperationExists,
    ProviderError,
    ProviderUnavailable,
)
from livepeer_builder.http.streaming import stream_response

JOB_ID_HEADER = "Livepeer-Job-Id"
_FAILURE_STATUS = {
    "no_offering": 404,
    "no_capacity": 503,
    "not_provisioned": 402,
    "refused": 503,
    "unreachable": 503,
    "payment": 503,
    "timeout": 504,
    "runner_rejected": 422,
    "runner_error": 502,
    "interrupted": 502,
}
_RETRY_AFTER = {"no_capacity": "5", "refused": "5", "unreachable": "5", "payment": "10"}


class IssueKeyBody(BaseModel):
    kind: CredentialKind = "actor"
    actor_id: str
    application_id: str = "default"
    permissions: list[str] | None = None
    label: str = ""


class ProvisionBody(BaseModel):
    amount: Money
    label: str = ""
    grant_id: str = ""


class GrantBody(BaseModel):
    name: str
    amount: Money


class StatusBody(BaseModel):
    status: AllocationStatus


class RotateKeyBody(BaseModel):
    label: str | None = None  # keep the key's label unless given


class RotateBody(BaseModel):
    operation_ref: str = Field(min_length=1, max_length=200)


class UsageBody(BaseModel):
    quantities: dict[str, int]


def actor_dependency(engine: EngineApi) -> Callable[..., Coroutine[Any, Any, ActorContext]]:
    """The FastAPI dependency every authenticated route uses: the engine's authenticator chain.

    Extra endpoints added through ``livepeer_builder.http.server`` (``extend=``) use it too, so they
    authenticate like the built-in routes: ``Annotated[ActorContext, Depends(actor_dependency(engine))]``.

    When the engine accepts API keys, it also declares the ``lpk_`` bearer scheme in OpenAPI, so Swagger
    UI shows Authorize. With keys off it declares a JWT bearer scheme when the authenticator chain announces
    a ``Bearer`` challenge (``LPB_AUTH=jwt``, D45), and otherwise none: the engine cannot know what another
    authenticator expects, so a service that wants Swagger to send it declares it in ``extend``.
    """
    # Declares the scheme only; authentication itself stays with engine.authenticator (raw headers).
    if engine.keys is not None:
        bearer = HTTPBearer(auto_error=False, bearerFormat="lpk", description="An engine API key: `lpk_...`")
    elif "bearer" in (engine.authenticator.challenge() or "").lower():  # JWT bearer (D45), keys off
        bearer = HTTPBearer(
            auto_error=False,
            bearerFormat="JWT",
            description="An access token from the engine's identity provider",
        )
    else:

        async def actor_from(request: Request) -> ActorContext:
            return await _authenticated(engine, request)

        return actor_from

    async def actor_from_key(
        request: Request,
        _declared: Annotated[HTTPAuthorizationCredentials | None, Security(bearer)],
    ) -> ActorContext:
        return await _authenticated(engine, request)

    return actor_from_key


async def _authenticated(engine: EngineApi, request: Request) -> ActorContext:
    auth = AuthRequest(
        method=request.method,
        path=request.url.path,
        headers={key.lower(): value for key, value in request.headers.items()},
        client_address=request.client.host if request.client else None,
    )
    actor = await engine.authenticator.authenticate(auth)
    if actor is None:
        raise NotAuthenticated("a credential is required")
    return actor


def create_app(engine: EngineApi, *, manage_lifecycle: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            if manage_lifecycle:
                await engine.start()
            yield
        finally:
            if manage_lifecycle:
                await engine.stop()

    app = FastAPI(title="livepeer-builder", version=engine.health().version, lifespan=lifespan)
    _install_error_handlers(app, engine)

    Actor = Annotated[ActorContext, Depends(actor_dependency(engine))]

    @app.get("/livez", include_in_schema=False)
    async def livez() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", response_model=EngineHealth)
    async def readyz(response: Response) -> EngineHealth:
        health = engine.health()
        response.status_code = 200 if health.ready else 503
        return health

    @app.get("/v1/offerings", response_model=list[Offering])
    async def offerings(app_name: Annotated[str | None, Query(alias="app")] = None) -> list[Offering]:
        # Public (#5): the catalog names no runner URL, starts no work and spends nothing.
        return engine.discovery.offerings(app=app_name)

    @app.get("/v1/runners", response_model=list[Runner])
    async def runners(actor: Actor, app_name: Annotated[str, Query(alias="app")]) -> list[Runner]:
        # App names contain "/" (livepeer-example/hello), so they travel as a query parameter.
        return engine.discovery.runners(actor, app_name)

    @app.post(
        "/v1/jobs",
        responses={
            200: {
                "description": "The runner's own status, content type and body. With "
                "`Accept: text/event-stream` or `?stream=true` the body is streamed as it arrives "
                "(docs/contracts/engine.md#streaming)."
            }
        },
    )
    async def run_job(
        actor: Actor,
        body: JobRequest,
        request: Request,
        stream: Annotated[bool, Query(description="stream the runner's response as it arrives")] = False,
    ) -> Response:
        if stream or "text/event-stream" in request.headers.get("accept", ""):
            streaming = engine.jobs.streaming
            if streaming is None:
                raise InvalidRequest("the configured runner transport does not support streaming")
            opened = await streaming.stream(actor, body)
            return await stream_response(
                opened, {JOB_ID_HEADER: str(opened.job.id), "Cache-Control": "no-cache"}
            )
        result = await engine.jobs.run(actor, body)
        return Response(
            content=result.content,
            status_code=result.status_code,
            media_type=result.content_type,
            headers={JOB_ID_HEADER: str(result.job.id)},
        )

    @app.get("/v1/jobs", response_model=list[Job])
    async def list_jobs(actor: Actor, limit: Annotated[int, Query(ge=1, le=500)] = 50) -> list[Job]:
        return list(await engine.jobs.list_jobs(actor, limit=limit))

    @app.get("/v1/jobs/{job_id}", response_model=Job)
    async def get_job(actor: Actor, job_id: UUID) -> Job:
        return await engine.jobs.get(actor, job_id)

    @app.get("/v1/jobs/{job_id}/result", status_code=200)
    async def get_result(actor: Actor, job_id: UUID) -> Response:
        result = await engine.jobs.result(actor, job_id)
        return Response(content=result.content, media_type=result.content_type)

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
        start: datetime | None = None,
        end: datetime | None = None,
        actor_id: str | None = None,
    ) -> CostSummary:
        end = end or datetime.now(UTC)
        return await engine.costs.summary(actor, start or end - timedelta(days=30), end, actor_id)

    @app.get("/v1/allowance", response_model=Allowance)
    async def allowance(actor: Actor, actor_id: str | None = None) -> Allowance:
        return await engine.payments.allowance(actor, actor_id)

    @app.get("/v1/events", response_model=EventPage)
    async def events(
        actor: Actor, after: int = 0, limit: Annotated[int, Query(ge=1, le=1000)] = 500
    ) -> EventPage:
        return await engine.events.after(actor, after, limit)

    @app.post("/v1/sessions", response_model=Session)
    async def open_session(actor: Actor, body: SessionRequest) -> Session:
        return await engine.sessions.open(actor, body)

    # -- admin (admin credential) -------------------------------------------------------------------

    if engine.keys is not None:  # API keys off: the key routes do not exist (decision 2)
        keys = engine.keys

        @app.post("/v1/admin/keys", response_model=IssuedKey, status_code=201)
        async def issue_key(actor: Actor, body: IssueKeyBody) -> IssuedKey:
            return await keys.issue(
                actor,
                kind=body.kind,
                actor_id=body.actor_id,
                application_id=body.application_id,
                permissions=body.permissions,
                label=body.label,
            )

        @app.get("/v1/admin/keys", response_model=list[AccessKey])
        async def list_keys(actor: Actor, application_id: str | None = None) -> list[AccessKey]:
            return await keys.list_keys(actor, application_id)

        @app.get("/v1/admin/keys/{key_id}", response_model=AccessKey)
        async def get_key(actor: Actor, key_id: str) -> AccessKey:
            return await keys.get(actor, key_id)

        @app.post("/v1/admin/keys/{key_id}/rotate", response_model=IssuedKey, status_code=201)
        async def rotate_key(actor: Actor, key_id: str, body: RotateKeyBody | None = None) -> IssuedKey:
            return await keys.rotate(actor, key_id, label=body.label if body else None)

        @app.post("/v1/admin/keys/{key_id}/revoke", status_code=204)
        async def revoke_key(actor: Actor, key_id: str) -> Response:
            await keys.revoke(actor, key_id)
            return Response(status_code=204)

    base = "/v1/admin/applications/{application_id}/actors/{actor_id}/allocation"

    @app.post(base, response_model=ProvisionedActor, status_code=201)
    async def provision(
        actor: Actor, application_id: str, actor_id: str, body: ProvisionBody
    ) -> ProvisionedActor:
        request = ProvisionRequest(
            application_id=application_id,
            actor_id=actor_id,
            amount=body.amount,
            label=body.label,
            grant_id=body.grant_id,
        )
        return await engine.payments.provision(actor, request)

    @app.post(base + "/fund", response_model=ProvisionedActor)
    async def fund(actor: Actor, application_id: str, actor_id: str, body: FundRequest) -> ProvisionedActor:
        return await engine.payments.fund(actor, application_id, actor_id, body)

    if engine.payments.grants is not None:  # the grant desk is off: no list or create routes (decision 7)
        desk = engine.payments.grants

        @app.get("/v1/admin/grants", response_model=list[GrantRecord])
        async def list_grants(actor: Actor) -> list[GrantRecord]:
            return await desk.list_grants(actor)

        @app.post("/v1/admin/grants", response_model=GrantRecord, status_code=201)
        async def create_grant(actor: Actor, body: GrantBody) -> GrantRecord:
            return await desk.create_grant(actor, body.name, body.amount)

    @app.post("/v1/admin/grant/fund", status_code=204)
    async def fund_grant(actor: Actor, body: FundRequest) -> Response:
        await engine.payments.fund_grant(actor, body)
        return Response(status_code=204)

    @app.post(base + "/status", response_model=ProvisionedActor)
    async def set_status(
        actor: Actor, application_id: str, actor_id: str, body: StatusBody
    ) -> ProvisionedActor:
        return await engine.payments.set_status(actor, application_id, actor_id, body.status)

    @app.post(base + "/revoke", response_model=ProvisionedActor)
    async def revoke(actor: Actor, application_id: str, actor_id: str) -> ProvisionedActor:
        return await engine.payments.revoke(actor, application_id, actor_id)

    @app.post(base + "/rotate-key", response_model=ProvisionedActor)
    async def rotate(actor: Actor, application_id: str, actor_id: str, body: RotateBody) -> ProvisionedActor:
        return await engine.payments.rotate_key(actor, application_id, actor_id, body.operation_ref)

    @app.get("/v1/admin/reconciliation", response_model=Reconciliation)
    async def reconcile(
        actor: Actor, application_id: str = "default", actor_id: str | None = None
    ) -> Reconciliation:
        return await engine.costs.reconcile(actor, application_id, actor_id)

    @app.post("/v1/admin/costs/sync", response_model=SyncReport)
    async def sync_costs(actor: Actor) -> SyncReport:
        return await engine.costs.sync_now(actor)

    return app


def _install_error_handlers(app: FastAPI, engine: EngineApi) -> None:
    def problem(
        status: int, message: str, headers: dict[str, str] | None = None, **extra: Any
    ) -> JSONResponse:
        return JSONResponse({"error": message, **extra}, status_code=status, headers=headers)

    @app.exception_handler(NotAuthenticated)
    async def _unauthenticated(_: Request, exc: NotAuthenticated) -> Response:
        challenge = engine.authenticator.challenge()
        return problem(401, str(exc), {"WWW-Authenticate": challenge} if challenge else None)

    @app.exception_handler(JobFailed)
    async def _failed(_: Request, exc: JobFailed) -> Response:
        failure = exc.job.failure
        kind = failure.kind if failure else "runner_error"
        if failure and kind == "runner_rejected" and failure.status_code:
            status = failure.status_code
        else:
            status = _FAILURE_STATUS.get(kind, 502)
        headers = {JOB_ID_HEADER: str(exc.job.id)}
        if kind in _RETRY_AFTER:
            headers["Retry-After"] = _RETRY_AFTER[kind]
        return problem(status, str(exc), headers, job=exc.job.model_dump(mode="json"))

    @app.exception_handler(OperationExists)
    async def _exists(_: Request, exc: OperationExists) -> Response:
        return problem(409, str(exc), {JOB_ID_HEADER: str(exc.job.id)}, job=exc.job.model_dump(mode="json"))

    @app.exception_handler(OperationConflict)
    async def _conflict(_: Request, exc: OperationConflict) -> Response:
        return problem(409, str(exc), {JOB_ID_HEADER: str(exc.job.id)})

    @app.exception_handler(NoCapacity)
    async def _capacity(_: Request, exc: NoCapacity) -> Response:
        return problem(503, str(exc), {"Retry-After": str(exc.retry_after_s)})

    @app.exception_handler(NotImplementedInMilestone)
    async def _reserved(_: Request, exc: NotImplementedInMilestone) -> Response:
        return problem(501, str(exc), milestone=exc.milestone, ticket=exc.ticket)

    simple: dict[type[EngineError], int] = {
        AccessDenied: 403,
        NotFound: 404,
        InvalidRequest: 400,
        NotProvisioned: 402,
        ProviderUnavailable: 503,
    }
    for error_type, status in simple.items():
        app.add_exception_handler(error_type, _status_handler(status))

    @app.exception_handler(ProviderError)
    async def _provider(_: Request, exc: ProviderError) -> Response:
        status = exc.status_code if exc.status_code in (400, 404, 409, 422) else 502
        return problem(status, str(exc))


def _status_handler(status: int) -> Any:
    async def handler(_: Request, exc: Exception) -> Response:
        return JSONResponse({"error": str(exc)}, status_code=status)

    return handler
