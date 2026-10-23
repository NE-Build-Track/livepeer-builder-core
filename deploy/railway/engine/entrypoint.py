#!/usr/bin/env python3
"""Start the engine on Railway Postgres.

Railway's DATABASE_URL uses the postgresql:// scheme. The engine's async
SQLAlchemy engine requires postgresql+asyncpg://. The rest of the URL,
including the password, is left unchanged and is not logged. The rewritten
URL is passed as a settings value to the server and as an explicit
environment to the `lpb` subprocesses; the process environment is not changed.
"""

import asyncio
import os
import subprocess
import sys

from livepeer_builder import EngineSettings
from livepeer_builder.http import serve

ASYNCPG_SCHEME = "postgresql+asyncpg://"


def asyncpg_url(url: str) -> str:
    if url.startswith(ASYNCPG_SCHEME):
        return url
    if url.startswith("postgresql://"):
        return ASYNCPG_SCHEME + url.removeprefix("postgresql://")
    if url.startswith("postgres://"):
        return ASYNCPG_SCHEME + url.removeprefix("postgres://")
    sys.exit("LPB_DATABASE_URL must be a postgresql URL")


def database_url() -> str:
    raw = os.environ.get("LPB_DATABASE_URL", "")
    if not raw:
        sys.exit("LPB_DATABASE_URL is required")
    return asyncpg_url(raw)


def lpb(command: str) -> int:
    # `command` is only "migrate" or "revision", chosen by this file.
    env = {**os.environ, "LPB_DATABASE_URL": database_url()}
    completed = subprocess.run(["/usr/local/bin/lpb", command], check=False, env=env)  # noqa: S603
    return completed.returncode


def migrate() -> int:
    """Apply packaged migrations, then fail if the database is not at package head.

    Railway runs this as the pre-deploy command. A non-zero exit stops the
    deployment, so the previous engine keeps serving.

    Preview shares one Postgres across every branch, so a preview deploy only
    checks the revision. Production applies the packaged migrations first.
    """
    if os.environ.get("RAILWAY_ENVIRONMENT_NAME") != "preview":
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
    settings = EngineSettings.from_env().model_copy(update={"database_url": database_url()})
    # LPB_AUTO_MIGRATE=false: refuse a database that is not at package head
    # rather than migrating from the process that is about to take traffic.
    if not settings.auto_migrate:
        code = lpb("revision")
        if code != 0:
            sys.exit(code)
    asyncio.run(serve(settings))


if __name__ == "__main__":
    main()
