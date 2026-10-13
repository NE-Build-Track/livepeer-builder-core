"""Backup and restore for the SQLite store (docs/RELIABILITY.md#backup-and-restore).

SQLite uses the online backup API, so a backup is consistent even while the engine runs. Restore
writes a verified backup over the live database and must run with the engine stopped. PostgreSQL
deployments use pg_dump/pg_restore; these commands refuse non-SQLite URLs with that guidance.
"""

import asyncio
import sqlite3
from pathlib import Path

from sqlalchemy.engine import make_url

from livepeer_builder.errors import InvalidRequest, UnsupportedForBackend
from livepeer_builder.store.schema import VERSION_TABLE


def sqlite_path(database_url: str) -> Path:
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite":
        raise UnsupportedForBackend(
            "backup/restore here is SQLite-only; use pg_dump and pg_restore for PostgreSQL"
        )
    if url.database in (None, "", ":memory:"):
        raise UnsupportedForBackend("an in-memory database cannot be backed up")
    return Path(url.database)


async def backup(database_url: str, destination: Path) -> Path:
    source = sqlite_path(database_url)
    if not source.exists():
        raise InvalidRequest(f"database {source} does not exist")
    await asyncio.to_thread(_copy, source, destination)
    await asyncio.to_thread(verify, destination)
    return destination


async def restore(database_url: str, backup_file: Path) -> None:
    """Replace the database with a verified backup. Stop the engine first."""
    target = sqlite_path(database_url)
    await asyncio.to_thread(verify, backup_file)
    await asyncio.to_thread(_copy, backup_file, target)


def verify(path: Path) -> str:
    """Integrity-check a database file and return its schema revision."""
    if not path.exists():
        raise InvalidRequest(f"{path} does not exist")
    try:
        return _verify(path)
    except sqlite3.DatabaseError as exc:  # not a SQLite database at all (e.g. a truncated copy)
        raise InvalidRequest(f"{path} is not a readable SQLite database: {exc}") from exc


def _verify(path: Path) -> str:
    with sqlite3.connect(path) as conn:
        status = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if status != "ok":
            raise InvalidRequest(f"{path} failed integrity_check: {status}")
        try:
            row = conn.execute(f"SELECT version_num FROM {VERSION_TABLE}").fetchone()  # noqa: S608 - constant
        except sqlite3.OperationalError as exc:
            raise InvalidRequest(f"{path} is not a livepeer-builder database") from exc
    if row is None:
        raise InvalidRequest(f"{path} has no schema revision")
    return str(row[0])


def _copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source) as src, sqlite3.connect(destination) as dst:
        src.backup(dst)
