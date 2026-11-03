"""The discovery service's composition root and HTTP app: ``lpb discovery`` runs ``serve``.

``GET /discovery`` is livepeer-node's inventory format, so an engine (``LPB_DISCOVERY_URL``), or later
a livepeer-node signer, reads it unchanged. Bring your own directory with ``serve(settings,
directory=...)``; logging is the caller's (the CLI configures it).
"""

import asyncio
import logging

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from livepeer_builder.discovery.catalog import Catalog
from livepeer_builder.discovery.directory import OrchestratorDirectory
from livepeer_builder.discovery.document import discovery_document
from livepeer_builder.discovery.onchain import OnChainDirectory
from livepeer_builder.discovery.probe import Prober
from livepeer_builder.discovery.rpc import JsonRpcChain
from livepeer_builder.discovery.settings import DiscoverySettings
from livepeer_builder.discovery.static import StaticDirectory

_LOG = logging.getLogger(__name__)


def create_app(catalog: Catalog, *, refresh: "asyncio.Task[None] | None" = None) -> Starlette:
    """The HTTP surface. With ``refresh``, readiness also needs that task alive: a stopped refresh would
    serve a frozen inventory, so it must not look ready (#46 review)."""

    async def discovery(_: Request) -> Response:
        # Runner objects are the orchestrator's JSON text. json.dumps would quote them.
        return Response(discovery_document(catalog.inventory()), media_type="application/json")

    async def healthz(_: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    async def readyz(_: Request) -> JSONResponse:
        status = catalog.status()
        refreshing = refresh is None or not refresh.done()
        ready = status.ready and refreshing
        body = status.model_dump(mode="json") | {"ready": ready, "refreshing": refreshing}
        return JSONResponse(body, status_code=200 if ready else 503)

    return Starlette(
        routes=[
            Route("/discovery", discovery, methods=["GET"]),
            Route("/healthz", healthz, methods=["GET"]),
            Route("/readyz", readyz, methods=["GET"]),
        ]
    )


def directory_from(settings: DiscoverySettings) -> OrchestratorDirectory:
    if settings.directory == "onchain":
        assert settings.arbitrum_rpc_url is not None  # checked by the settings
        return OnChainDirectory(
            JsonRpcChain(settings.arbitrum_rpc_url.get_secret_value()),
            controller=settings.controller,
            service_registry=None if settings.service_registry == "controller" else settings.service_registry,
        )
    assert settings.orchestrators is not None  # checked by the settings
    return StaticDirectory.parse(settings.orchestrators)


async def serve(
    settings: DiscoverySettings,
    *,
    directory: OrchestratorDirectory | None = None,
) -> None:
    """Run the refresh loop and the listener until cancelled."""
    import uvicorn

    catalog = Catalog(
        directory or directory_from(settings),
        Prober(timeout_s=settings.probe_timeout_s),
        max_failed_refreshes=settings.max_failed_refreshes,
        directory_timeout_s=settings.directory_timeout_s,
    )
    stop = asyncio.Event()
    refresh = asyncio.create_task(
        catalog.run(
            stop,
            directory_interval_s=settings.directory_interval_s,
            probe_interval_s=settings.probe_interval_s,
        ),
        name="lpd-refresh",
    )
    refresh.add_done_callback(_stopped)
    config = uvicorn.Config(
        create_app(catalog, refresh=refresh),
        host=settings.http_host,
        port=settings.http_port,
        log_config=None,
    )
    try:
        await uvicorn.Server(config).serve()
    finally:
        stop.set()
        refresh.cancel()
        await asyncio.gather(refresh, return_exceptions=True)


def _stopped(task: "asyncio.Task[None]") -> None:
    if not task.cancelled() and task.exception() is not None:
        _LOG.error(
            "the discovery refresh stopped; /readyz now fails",
            extra={"event": "discovery.refresh_stopped", "error": type(task.exception()).__name__},
        )
