"""The stock server's composition root: ``lpb serve``, the Railway entrypoint and your own entrypoint.

Customize the service the way you customize the embedded engine, with the same two stages
(docs/design-docs/extending-the-engine.md): ``supply`` parts, ``decorate`` defaults, and ``extend``
the app with routes that receive the engine explicitly::

    asyncio.run(serve(EngineSettings.from_env(), supply=..., decorate=..., extend=...))
"""

from collections.abc import Callable

from fastapi import FastAPI

from livepeer_builder import BuilderEngine, Decorate, EngineApi, EngineSettings, EngineSupply
from livepeer_builder.http.app import actor_dependency, create_app
from livepeer_builder.runtime import configure_logging

Extend = Callable[[FastAPI, EngineApi], None]
"""Adds routes or middleware to the app; receives the engine, so nothing is looked up."""


async def build_app(
    settings: EngineSettings,
    *,
    supply: EngineSupply | None = None,
    decorate: Decorate | None = None,
    extend: Extend | None = None,
) -> FastAPI:
    """An app for your own ASGI stack; logging is yours.

    The engine lives as long as the app's lifespan, which starts and stops it, so run that lifespan (any
    ASGI server does). To hold the engine yourself, use ``BuilderEngine.create`` and ``create_app``.
    """
    engine = await BuilderEngine.create(settings, supply=supply, decorate=decorate)
    try:
        return _app(engine, extend)
    except BaseException:
        await engine.stop()
        raise


async def serve(
    settings: EngineSettings,
    *,
    supply: EngineSupply | None = None,
    decorate: Decorate | None = None,
    extend: Extend | None = None,
) -> None:
    """Run the service. The engine is stopped even when the server never starts (a taken port)."""
    import uvicorn

    configure_logging(json_lines=settings.log_json)
    engine = await BuilderEngine.create(settings, supply=supply, decorate=decorate)
    try:
        app = _app(engine, extend)
        config = uvicorn.Config(app, host=settings.http_host, port=settings.http_port, log_config=None)
        await uvicorn.Server(config).serve()
    finally:
        await engine.stop()  # idempotent: the lifespan may already have stopped it


def _app(engine: EngineApi, extend: Extend | None) -> FastAPI:
    app = create_app(engine)
    if extend is not None:
        extend(app, engine)
    return app


__all__ = ["Extend", "actor_dependency", "build_app", "serve"]
