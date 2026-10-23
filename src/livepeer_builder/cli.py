"""``lpb``: operator commands. Every command reads LPB_* settings (docs/RELIABILITY.md#operations).

lpb migrate            apply packaged migrations (production runs this before start)
lpb revision           show the database revision and the package head
lpb backup FILE        consistent SQLite backup (online)
lpb restore FILE       restore a verified backup (engine stopped)
lpb verify FILE        integrity-check a backup and print its revision
lpb purge              apply the configured retention now
lpb bootstrap-admin    issue the first admin key (one time; printed once)
lpb gen-secret-key     print a new LPB_SECRET_KEY
lpb smoke              import, migrate, start, health-check, stop (CI install check)
lpb serve              run the REST service (needs the [server] extra)
lpb gen-docs [--check] regenerate docs/generated (openapi.json, db-schema.md)
"""

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from livepeer_builder.config import EngineSettings
from livepeer_builder.errors import EngineError
from livepeer_builder.runtime import BuilderEngine, package_version
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
    docs = command("gen-docs", _gen_docs, "regenerate docs/generated")
    docs.add_argument("--out", type=Path, default=Path("docs/generated"))
    docs.add_argument("--check", action="store_true", help="fail if the committed files are stale")
    return parser


def _emit(payload: Any) -> None:
    print(json.dumps(payload, indent=2, default=str))


async def _migrate(_: argparse.Namespace) -> None:
    store = await SqlStore.connect(EngineSettings.from_env().database_url, migrate=False)
    try:
        await upgrade(store.engine)
        _emit({"revision": await current_revision(store.engine)})
    finally:
        await store.close()


async def _revision(_: argparse.Namespace) -> int:
    store = await SqlStore.connect(EngineSettings.from_env().database_url, migrate=False)
    try:
        current, head = await current_revision(store.engine), head_revision()
    finally:
        await store.close()
    _emit({"database": current, "package_head": head, "up_to_date": current == head})
    return 0 if current == head else 2


async def _backup(args: argparse.Namespace) -> None:
    _emit({"backup": str(await backup(EngineSettings.from_env().database_url, args.file))})


async def _restore(args: argparse.Namespace) -> int:
    if not args.yes:
        print("restore overwrites the database; stop the engine and pass --yes", file=sys.stderr)
        return 2
    await restore(EngineSettings.from_env().database_url, args.file)
    _emit({"restored_from": str(args.file)})
    return 0


async def _verify(args: argparse.Namespace) -> None:
    _emit({"file": str(args.file), "revision": await asyncio.to_thread(verify, args.file)})


async def _purge(_: argparse.Namespace) -> None:
    engine = await BuilderEngine.create(EngineSettings.from_env())
    try:
        _emit((await engine.purge()).model_dump())
    finally:
        await engine.stop()


async def _bootstrap(args: argparse.Namespace) -> None:
    engine = await BuilderEngine.create(EngineSettings.from_env())
    try:
        if engine.keys is None:
            raise SystemExit("API keys are disabled in this deployment")
        issued = await engine.keys.bootstrap_admin(args.label)
        _emit({"key_id": issued.key.key_id, "token": issued.token, "note": "shown once; store it now"})
    finally:
        await engine.stop()


async def _gen_secret_key(_: argparse.Namespace) -> None:
    from livepeer_builder.adapters.secrets import EncryptedDbSecretStore

    print(EncryptedDbSecretStore.generate_key())


async def _smoke(_: argparse.Namespace) -> int:
    """The CI install check: proves the installed wheel imports, migrates, starts and stops."""
    engine = await BuilderEngine.create(EngineSettings.from_env())
    try:
        await engine.start()
        health = engine.health()
        revision = await engine.schema_revision()
    finally:
        await engine.stop()
    ok = revision == head_revision()
    _emit({"ok": ok, "version": health.version, "schema_revision": revision, "health": health.model_dump()})
    return 0 if ok else 1


async def _serve(_: argparse.Namespace) -> None:
    try:
        import uvicorn

        from livepeer_builder.http import create_app
    except ImportError:
        print("the REST service needs: pip install 'livepeer-builder[server]'", file=sys.stderr)
        raise SystemExit(2) from None
    from livepeer_builder.runtime.logging import configure_logging

    settings = EngineSettings.from_env()
    configure_logging(json_lines=settings.log_json)
    app = create_app(await BuilderEngine.create(settings))
    server = uvicorn.Server(
        uvicorn.Config(app, host=settings.http_host, port=settings.http_port, log_config=None)
    )
    await server.serve()


async def _gen_docs(args: argparse.Namespace) -> int:
    from livepeer_builder.docs_diagrams import render_contracts, render_interfaces
    from livepeer_builder.docs_gen import render_db_schema, render_openapi

    outputs = {
        args.out / "openapi.json": await render_openapi(),
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
