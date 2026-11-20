"""REST adapter (the ``[server]`` extra): the app, and what both halves of the engine share in it.

The routes are two sets (D84): the work half's (``http/work.py``) and the clearinghouse half's, under
``/v1/clearinghouse`` (``http/clearinghouse.py``). Each is one function that adds its routes to an app,
by itself or with the other. ``shared_app`` builds the app both need: the engine's lifecycle, the error
answers, the request limit and the health routes. ``create_app`` mounts both sets on it.

The sets register their routes on the app itself, as ``create_app`` always did, and not through
``include_router``: every route stays an ``APIRoute`` in ``app.routes``, which the structure tests read
(tests/structure/test_route_params.py).

The generated schema is committed at docs/generated/openapi.json and checked for drift in CI.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from livepeer_builder.api import ClearinghouseApi, EngineApi, WorkApi
from livepeer_builder.contracts import EngineHealth
from livepeer_builder.errors import (
    AccessDenied,
    AlreadyAllotted,
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
    OperationInProgress,
    ProviderError,
    ProviderUnavailable,
)
from livepeer_builder.http.clearinghouse import mount_clearinghouse_routes
from livepeer_builder.http.limits import DEFAULT_MAX_REQUEST_BYTES, install_request_limit
from livepeer_builder.http.replies import JOB_ID_HEADER
from livepeer_builder.http.unprocessable import install_unprocessable_handler
from livepeer_builder.http.work import mount_work_routes

_FAILURE_STATUS = {
    "no_offering": 404,
    "no_capacity": 503,
    "not_provisioned": 402,
    "refused": 503,
    "unreachable": 503,
    "payment": 503,
    "payment_unknown": 503,
    "timeout": 504,
    "runner_rejected": 422,
    "runner_error": 502,
    "interrupted": 502,
}
_RETRY_AFTER = {"no_capacity": "5", "refused": "5", "unreachable": "5", "payment": "10"}
# A signer refusal is the payer's to fix, not a reason to retry (D58): no Retry-After.
_REFUSAL_STATUS = {"not_authorized": 403, "allowance_exhausted": 402}
_HELD = {"command", "operation_ref", "state"}  # what a 409 shows of an admin command holding the reference


def create_app(
    engine: EngineApi, *, manage_lifecycle: bool = True, max_request_bytes: int = DEFAULT_MAX_REQUEST_BYTES
) -> FastAPI:
    """The whole REST service: the work routes and the clearinghouse routes on one app."""
    app = shared_app(engine, manage_lifecycle=manage_lifecycle, max_request_bytes=max_request_bytes)
    mount_work_routes(app, engine)
    mount_clearinghouse_routes(app, engine)
    return app


def shared_app(
    engine: WorkApi | ClearinghouseApi,
    *,
    manage_lifecycle: bool = True,
    max_request_bytes: int = DEFAULT_MAX_REQUEST_BYTES,
) -> FastAPI:
    """An app with what both halves share and no set of routes yet: the lifespan that starts and stops the
    engine, the error answers, the request limit and the health routes."""

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if manage_lifecycle:
            # Before the try: a start that fails has already stopped the engine, and one that is refused
            # because the engine is running (#113, C27) must not stop an engine this lifespan did not start.
            await engine.start()
        try:
            yield
        finally:
            if manage_lifecycle:
                await engine.stop()

    app = FastAPI(title="livepeer-builder", version=engine.health().version, lifespan=lifespan)
    _install_error_handlers(app, engine)
    install_request_limit(app, max_request_bytes)  # 413 before a body is held in memory (D59)

    @app.get("/livez", include_in_schema=False)
    async def livez() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", response_model=EngineHealth)
    async def readyz(response: Response) -> EngineHealth:
        health = engine.health()
        response.status_code = 200 if health.ready else 503
        return health

    return app


def _install_error_handlers(app: FastAPI, engine: WorkApi | ClearinghouseApi) -> None:
    def problem(
        status: int, message: str, headers: dict[str, str] | None = None, **extra: Any
    ) -> JSONResponse:
        return JSONResponse({"error": message, **extra}, status_code=status, headers=headers)

    install_unprocessable_handler(app)

    @app.exception_handler(NotAuthenticated)
    async def _unauthenticated(_: Request, exc: NotAuthenticated) -> Response:
        challenge = engine.authenticator.challenge()
        return problem(401, str(exc), {"WWW-Authenticate": challenge} if challenge else None)

    @app.exception_handler(JobFailed)
    async def _failed(_: Request, exc: JobFailed) -> Response:
        failure = exc.job.failure
        kind = failure.kind if failure else "runner_error"
        refusal = _REFUSAL_STATUS.get(failure.reason or "") if failure else None
        if refusal is not None:
            status = refusal
        elif failure and kind == "runner_rejected" and failure.status_code:
            status = failure.status_code
        else:
            status = _FAILURE_STATUS.get(kind, 502)
        headers = {JOB_ID_HEADER: str(exc.job.id)}
        if kind in _RETRY_AFTER and refusal is None:
            headers["Retry-After"] = _RETRY_AFTER[kind]
        return problem(status, str(exc), headers, job=exc.job.model_dump(mode="json"))

    @app.exception_handler(OperationExists)
    async def _exists(_: Request, exc: OperationExists) -> Response:
        """Both carry what holds the reference; one still running asks the caller to come back (D57)."""
        headers, held = _holder(exc)
        code = "operation_exists"
        if isinstance(exc, OperationInProgress):
            headers["Retry-After"] = str(exc.retry_after_s)
            code = "operation_in_progress"
        return problem(409, str(exc), headers, code=code, **held)

    @app.exception_handler(OperationConflict)
    async def _conflict(_: Request, exc: OperationConflict) -> Response:
        headers, held = _holder(exc)
        held.pop("job", None)
        return problem(409, str(exc), headers, code="operation_conflict", **held)

    @app.exception_handler(AlreadyAllotted)
    async def _allotted(_: Request, exc: AlreadyAllotted) -> Response:
        return problem(409, str(exc), code="already_allotted", actor=exc.actor.model_dump(mode="json"))

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


def _holder(exc: OperationExists | OperationConflict) -> tuple[dict[str, str], dict[str, Any]]:
    """The job holding a reference (its id as a header), or the admin command (D57, ISS-11)."""
    if exc.job is not None:
        return {JOB_ID_HEADER: str(exc.job.id)}, {"job": exc.job.model_dump(mode="json")}
    operation = exc.operation
    return {}, {} if operation is None else {"operation": operation.model_dump(include=_HELD)}


def _status_handler(status: int) -> Any:
    async def handler(_: Request, exc: Exception) -> Response:
        return JSONResponse({"error": str(exc)}, status_code=status)

    return handler
