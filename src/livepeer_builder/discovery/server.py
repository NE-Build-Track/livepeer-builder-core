"""The discovery service's composition root and HTTP app: ``lpb discovery`` runs ``serve``.

``GET /discovery`` is livepeer-node's inventory format, so an engine (``LPB_DISCOVERY_URL``), or later
a livepeer-node signer, reads it unchanged. Bring your own directory with ``serve(settings,
directory=...)``; logging is the caller's (the CLI configures it).
"""

import asyncio

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from livepeer_builder.discovery.catalog import Catalog, Probes
from livepeer_builder.discovery.directory import OrchestratorDirectory
from livepeer_builder.discovery.onchain import OnChainDirectory
from livepeer_builder.discovery.probe import Prober
from livepeer_builder.discovery.rpc import JsonRpcChain
from livepeer_builder.discovery.settings import DiscoverySettings
from livepeer_builder.discovery.static import StaticDirectory


def create_app(catalog: Catalog) -> Starlette:
    async def discovery(_: Request) -> JSONResponse:
        return JSONResponse(catalog.inventory())

    async def healthz(_: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    async def readyz(_: Request) -> JSONResponse:
        status = catalog.status()
        return JSONResponse(status.model_dump(mode="json"), status_code=200 if status.ready else 503)

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
            bonding_manager=settings.bonding_manager,
            ai_service_registry=settings.ai_service_registry,
        )
    assert settings.orchestrators is not None  # checked by the settings
    return StaticDirectory.parse(settings.orchestrators)


async def serve(
    settings: DiscoverySettings,
    *,
    directory: OrchestratorDirectory | None = None,
    prober: Probes | None = None,
) -> None:
    """Run the refresh loop and the listener until cancelled."""
    import uvicorn

    catalog = Catalog(
        directory or directory_from(settings),
        prober or Prober(timeout_s=settings.probe_timeout_s),
        max_failed_refreshes=settings.max_failed_refreshes,
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
    config = uvicorn.Config(
        create_app(catalog), host=settings.http_host, port=settings.http_port, log_config=None
    )
    try:
        await uvicorn.Server(config).serve()
    finally:
        stop.set()
        refresh.cancel()
        await asyncio.gather(refresh, return_exceptions=True)
