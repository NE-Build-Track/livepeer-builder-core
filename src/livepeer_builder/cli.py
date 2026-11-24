"""``lpb``: operator commands (docs/RELIABILITY.md#operations). ``migrate``, ``revision``, ``backup`` and
``restore`` read only ``LPB_DATABASE_URL``; ``purge``, ``bootstrap-admin``, ``smoke`` and ``serve`` read the
LPB_* settings. Only an engine that starts needs ``LPB_DISCOVERY_URL``, so only ``serve`` requires it:
``purge`` and ``bootstrap-admin`` build an engine and never start it, and ``smoke`` starts one at an
address of its own (#112, C23; #154). With a payment provider, ``purge`` and ``bootstrap-admin``, which
never allot, do not require ``LPB_SECRET_KEY`` (#106 review).

lpb migrate            apply packaged migrations (production runs this before start)
lpb revision           show the database revision and the package head
lpb backup FILE        consistent SQLite backup (online)
lpb restore FILE       restore a verified backup (engine stopped)
lpb verify FILE        integrity-check a backup and print its revision
lpb purge              apply the configured retention now
lpb bootstrap-admin    issue the first admin key (one time; printed once)
lpb gen-secret-key     print a new LPB_SECRET_KEY
lpb smoke              import, migrate, start, health-check, stop, on a throwaway database (CI), in the
                       role LPB_ROLE names
lpb serve              run the REST service (needs the [server] extra)
lpb payments check     probe what the engine's role needs for payments, with GET requests only: the
                       signer, and Batteries or the payment clearinghouse of an engine in its work role
                       (exit 0 ready, 1 a probe failed, 2 settings refused)
lpb gen-docs [--check] regenerate docs/generated (openapi.json, openapi-clearinghouse.json for the
                       clearinghouse role, db-schema.md, interfaces.md, contracts.md)
"""

import argparse
import asyncio
import json
import sys
import tempfile
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from livepeer_builder import EngineError, EngineSettings, generate_secret_key
from livepeer_builder.runtime import BuilderEngine, check_payments, package_version
from livepeer_builder.store import SqlStore, backup, current_revision, head_revision, restore, upgrade, verify


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        return int(asyncio.run(args.handler(args)) or 0)
    except EngineError as exc:
        print(json.dumps({"error": str(exc), "type": type(exc).__name__}), file=sys.stderr)
        return 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lpb", description="livepeer-builder operator commands")
    parser.add_argument("--version", action="version", version=package_version())
    commands = parser.add_subparsers(required=True, metavar="COMMAND")

    def command(name: str, handler: Any, help_text: str) -> argparse.ArgumentParser:
        sub = commands.add_parser(name, help=help_text)
        sub.set_defaults(handler=handler)
        return sub

    command("migrate", _migrate, "apply packaged migrations")
    command("revision", _revision, "show database and package schema revisions")
    command("backup", _backup, "back up the SQLite database").add_argument("file", type=Path)
    restore_cmd = command("restore", _restore, "restore a verified backup (engine stopped)")
    restore_cmd.add_argument("file", type=Path)
    restore_cmd.add_argument("--yes", action="store_true", help="confirm overwriting the database")
    command("verify", _verify, "integrity-check a backup").add_argument("file", type=Path)
    command("purge", _purge, "apply configured retention now")
    command("bootstrap-admin", _bootstrap, "issue the first admin key").add_argument(
        "--label", default="bootstrap"
    )
    command("gen-secret-key", _gen_secret_key, "print a new LPB_SECRET_KEY")
    command("smoke", _smoke, "import/start/health/stop check")
    command("serve", _serve, "run the REST service")
    payments = command("payments", _payments_check, "check what payments need").add_subparsers(
        required=True, metavar="CHECK"
    )
    payments.add_parser(
        "check", help="probe the signer, and Batteries or the payment clearinghouse; never writes"
    ).set_defaults(handler=_payments_check)
    docs = command("gen-docs", _gen_docs, "regenerate docs/generated")
    docs.add_argument("--out", type=Path, default=Path("docs/generated"))
    docs.add_argument("--check", action="store_true", help="fail if the committed files are stale")
    return parser


def _emit(payload: Any) -> None:
    print(json.dumps(payload, indent=2, default=str))


@asynccontextmanager
async def _open_store() -> AsyncIterator[SqlStore]:
    """The store at ``LPB_DATABASE_URL``, as it is: not migrated here, and closed on the way out. The
    store-only commands read that setting and nothing else."""
    store = await SqlStore.connect(_database_url(), migrate=False)
    try:
        yield store
    finally:
        await store.close()


@asynccontextmanager
async def _built_engine(settings: EngineSettings) -> AsyncIterator[BuilderEngine]:
    """An engine built from ``settings`` and not started, stopped on the way out: what it opened is
    closed, whether or not the command started it."""
    engine = await BuilderEngine.create(settings)
    try:
        yield engine
    finally:
        await engine.stop()


def _database_url() -> str:
    """The store-only commands read this setting and nothing else."""
    return EngineSettings.database_url_from_env()


def _settings_that_allot_nothing() -> EngineSettings:
    """The LPB_* settings for purge and bootstrap-admin. They never allot, so they keep no payment key:
    without LPB_SECRET_KEY they get a key for this run only, and the engine's check that a payment
    provider's keys can be kept is met (#106 review). They never start the engine either, so they need no
    discovery address and get none (#154). smoke gets no key: it proves the engine starts as serve would."""
    settings = EngineSettings.from_env()
    if settings.secret_key is not None:
        return settings
    # A new key is valid by construction, so the copy needs no second validation.
    return settings.model_copy(update={"secret_key": SecretStr(generate_secret_key())})


async def _migrate(_: argparse.Namespace) -> None:
    async with _open_store() as store:
        await upgrade(store.engine)
        _emit({"revision": await current_revision(store.engine)})


async def _revision(_: argparse.Namespace) -> int:
    async with _open_store() as store:
        current, head = await current_revision(store.engine), head_revision()
    _emit({"database": current, "package_head": head, "up_to_date": current == head})
    return 0 if current == head else 2


async def _backup(args: argparse.Namespace) -> None:
    _emit({"backup": str(await backup(_database_url(), args.file))})


async def _restore(args: argparse.Namespace) -> int:
    if not args.yes:
        print("restore overwrites the database; stop the engine and pass --yes", file=sys.stderr)
        return 2
    await restore(_database_url(), args.file)
    _emit({"restored_from": str(args.file)})
    return 0


async def _verify(args: argparse.Namespace) -> None:
    _emit({"file": str(args.file), "revision": await asyncio.to_thread(verify, args.file)})


async def _purge(_: argparse.Namespace) -> None:
    async with _built_engine(_settings_that_allot_nothing()) as engine:
        _emit((await engine.purge()).model_dump())


async def _bootstrap(args: argparse.Namespace) -> None:
    async with _built_engine(_settings_that_allot_nothing()) as engine:
        if engine.keys is None:
            raise SystemExit("API keys are disabled in this deployment")
        issued = await engine.keys.bootstrap_admin(args.label)
        _emit({"key_id": issued.key.key_id, "token": issued.token, "note": "shown once; store it now"})


async def _gen_secret_key(_: argparse.Namespace) -> None:
    print(generate_secret_key())


async def _smoke(_: argparse.Namespace) -> int:
    """The CI install check: proves the installed wheel imports, migrates, starts and stops.

    It never opens ``LPB_DATABASE_URL``: starting an engine there would resolve that deployment's running
    jobs as interrupted. It migrates a throwaway SQLite file instead, removed afterwards (#112, C23).

    The engine is built in the role the environment names (``LPB_ROLE``, #169), from that deployment's
    settings, so it also proves the settings load and the role has the parts it needs. Starting it polls
    discovery once and, outside the ``clearinghouse`` role, may read one page of usage; it writes
    nothing anywhere but the throwaway database.

    An engine does not start without a discovery address (D63). Without ``LPB_DISCOVERY_URL`` smoke
    starts its engine at an address nothing listens on: its one poll is refused, and the engine starts
    as it does when discovery is down (#154)."""
    with tempfile.TemporaryDirectory(prefix="lpb-smoke-") as directory:
        database = f"sqlite+aiosqlite:///{Path(directory) / 'smoke.db'}"
        configured = EngineSettings.from_env()
        # Each value is valid by construction, so the copy needs no second validation.
        settings = configured.model_copy(
            update={
                "database_url": database,
                "auto_migrate": True,
                "discovery_url": configured.discovery_url or "http://127.0.0.1:9/discovery",
            }
        )
        async with _built_engine(settings) as engine:
            await engine.start()
            health = engine.health()
            revision = await engine.schema_revision()
    ok = revision == head_revision()
    report = {"ok": ok, "role": health.role, "version": health.version, "schema_revision": revision}
    _emit(report | {"health": health.model_dump()})
    return 0 if ok else 1


async def _serve(_: argparse.Namespace) -> None:
    try:
        from livepeer_builder.http import serve
    except ImportError:
        print("the REST service needs: pip install 'livepeer-builder[server]'", file=sys.stderr)
        raise SystemExit(2) from None
    await serve(EngineSettings.from_env())


async def _payments_check(_: argparse.Namespace) -> int:
    """Read-only (D87): every request is a GET. No engine is built and no database is opened. Exit 0 when
    every probe passed, 1 when one was refused or failed, 2 when the settings themselves are refused. The
    report names settings and never holds a credential, an address or the other side's own words."""
    try:
        settings = EngineSettings.from_env()
    except ValueError as refused:  # pydantic's, which names the settings and echoes no value
        print(json.dumps({"error": str(refused), "type": "InvalidSettings"}), file=sys.stderr)
        return 2
    report = await check_payments(settings)
    _emit(report.model_dump(mode="json"))
    return 0 if report.ok else 1


async def _gen_docs(args: argparse.Namespace) -> int:
    from livepeer_builder.docs_diagrams import render_contracts, render_interfaces
    from livepeer_builder.docs_gen import render_db_schema, render_openapi

    outputs = {
        args.out / "openapi.json": await render_openapi(),
        args.out / "openapi-clearinghouse.json": await render_openapi("clearinghouse"),
        args.out / "db-schema.md": render_db_schema(),
        args.out / "interfaces.md": render_interfaces(),
        args.out / "contracts.md": render_contracts(),
    }
    stale = [str(path) for path, text in outputs.items() if not path.exists() or path.read_text() != text]
    if args.check:
        if stale:
            print(f"stale generated docs: {stale}; run `uv run lpb gen-docs`", file=sys.stderr)
            return 1
        return 0
    for path, text in outputs.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    _emit({"written": [str(path) for path in outputs]})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
