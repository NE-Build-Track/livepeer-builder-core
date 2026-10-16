#!/usr/bin/env python3
"""Start the engine on Railway Postgres, with bearer auth in Swagger.

Railway's DATABASE_URL uses the postgresql:// scheme. The engine's async
SQLAlchemy engine requires postgresql+asyncpg://. The rest of the URL,
including the password, is left unchanged and is not logged.

Swagger's Authorize button is added here so this deployment can send
Authorization: Bearer without changing the upstream API.
"""

import asyncio
import os
import subprocess
import sys
from typing import Any

import uvicorn
from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi

from livepeer_builder.config import EngineSettings
from livepeer_builder.http import create_app
from livepeer_builder.runtime import BuilderEngine
from livepeer_builder.runtime.logging import configure_logging

ASYNCPG_SCHEME = "postgresql+asyncpg://"


def asyncpg_url(url: str) -> str:
    if url.startswith(ASYNCPG_SCHEME):
        return url
    if url.startswith("postgresql://"):
        return ASYNCPG_SCHEME + url.removeprefix("postgresql://")
    if url.startswith("postgres://"):
        return ASYNCPG_SCHEME + url.removeprefix("postgres://")
    sys.exit("LPB_DATABASE_URL must be a postgresql URL")


def install_swagger_bearer(app: FastAPI) -> None:
    """Expose an optional bearer scheme so Swagger can set Authorization."""

    def openapi() -> dict[str, Any]:
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
        components = schema.setdefault("components", {})
        schemes = components.setdefault("securitySchemes", {})
        schemes["bearerAuth"] = {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "lpk",
            "description": "Engine access key. Swagger sends Authorization: Bearer <key>.",
        }
        # The empty alternative keeps routes callable when no token is entered.
        schema["security"] = [{"bearerAuth": []}, {}]
        app.openapi_schema = schema
        return schema

    app.openapi = openapi


async def serve() -> None:
    settings = EngineSettings.from_env()
    configure_logging(json_lines=settings.log_json)
    app = create_app(await BuilderEngine.from_settings(settings))
    install_swagger_bearer(app)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=settings.http_host,
            port=settings.http_port,
            log_config=None,
        )
    )
    await server.serve()


def prepare_database_url() -> None:
    raw = os.environ.get("LPB_DATABASE_URL", "")
    if not raw:
        sys.exit("LPB_DATABASE_URL is required")
    os.environ["LPB_DATABASE_URL"] = asyncpg_url(raw)


def lpb(command: str) -> int:
    # `command` is only "migrate" or "revision", chosen by this file.
    completed = subprocess.run(["/usr/local/bin/lpb", command], check=False)  # noqa: S603
    return completed.returncode


def migrate() -> int:
    """Apply packaged migrations, then fail if the database is not at package head.

    Railway runs this as the pre-deploy command. A non-zero exit stops the
    deployment, so the previous engine keeps serving.
    """
    prepare_database_url()
    code = lpb("migrate")
    if code != 0:
        return code
    return lpb("revision")


def main() -> None:
    # Railway may pass the pre-deploy command as the sole argument, or as a
    # full `python3 -u entrypoint.py migrate` invocation appended to ENTRYPOINT.
    if "migrate" in sys.argv[1:]:
        sys.exit(migrate())
    if sys.argv[1:] not in ([], ["serve"]):
        sys.exit("usage: entrypoint.py [serve|migrate]")
    prepare_database_url()
    # Preview sets LPB_AUTO_MIGRATE=false and migrates in pre-deploy.
    # Refuse to serve a database that is behind the package rather than
    # migrating from the process that is about to take traffic.
    if not EngineSettings.from_env().auto_migrate:
        code = lpb("revision")
        if code != 0:
            sys.exit(code)
    asyncio.run(serve())


if __name__ == "__main__":
    main()
