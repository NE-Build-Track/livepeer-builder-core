#!/usr/bin/env python3
"""Start the engine on Neon Postgres.

The engine's async SQLAlchemy engine requires postgresql+asyncpg://. Neon
and Railway hand out postgresql:// or postgres://, and Neon adds libpq
parameters asyncpg does not accept. This rewrites the scheme, maps sslmode
to asyncpg's ssl parameter, and drops channel_binding. The password is left
unchanged and is not logged. The rewritten URL is passed as a settings value
to the server and as an explicit environment to the `lpb` subprocesses; the
process environment is not changed.
"""

import asyncio
import os
import subprocess
import sys
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from livepeer_builder import EngineSettings
from livepeer_builder.http import serve

ASYNCPG_SCHEME = "postgresql+asyncpg://"


def asyncpg_url(url: str) -> str:
    if url.startswith("postgres://"):
        url = "postgresql://" + url.removeprefix("postgres://")
    if url.startswith("postgresql://"):
        url = ASYNCPG_SCHEME + url.removeprefix("postgresql://")
    if not url.startswith(ASYNCPG_SCHEME):
        sys.exit("LPB_DATABASE_URL must be a postgresql URL")
    return _asyncpg_query(url)


def _asyncpg_query(url: str) -> str:
    parts = urlsplit(url)
    kept: list[tuple[str, str]] = []
    ssl: str | None = None
    saw_ssl = False
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key == "channel_binding":
            continue
        if key == "sslmode":
            if not saw_ssl:
                ssl = value
            continue
        if key == "ssl":
            saw_ssl = True
            ssl = value
            continue
        kept.append((key, value))
    if ssl is not None:
        kept.append(("ssl", ssl))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept), parts.fragment))


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

    Production migrates the protected Neon main branch. A preview deploy
    migrates that pull request's branch, which is a copy of main.
    """
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
