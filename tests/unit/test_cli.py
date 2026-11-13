"""The `lpb` operator CLI, run in-process: migrations, revision, backup/restore, bootstrap, smoke, docs."""

import asyncio
import json
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from livepeer_builder import cli
from livepeer_builder.contracts import Job
from livepeer_builder.store import SqlStore, create_engine, upgrade
from livepeer_builder.testing import FixedClock


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "engine.db"
    monkeypatch.setenv("LPB_DATABASE_URL", f"sqlite+aiosqlite:///{path}")
    monkeypatch.setenv("LPB_DISCOVERY_URL", "http://127.0.0.1:9/discovery")
    monkeypatch.setenv("LPB_LOG_JSON", "false")
    monkeypatch.chdir(tmp_path)
    return path


def run(*argv: str) -> int:
    return cli.main(list(argv))


def output(capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    return dict(json.loads(capsys.readouterr().out))


def test_migrate_and_revision(db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run("migrate") == 0
    assert output(capsys) == {"revision": "0011"}
    assert run("revision") == 0
    assert output(capsys)["up_to_date"] is True


def test_revision_reports_a_database_that_is_behind(db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    async def at_first_revision() -> None:
        engine = create_engine(f"sqlite+aiosqlite:///{db}")
        await upgrade(engine, "0001")
        await engine.dispose()

    asyncio.run(at_first_revision())
    assert run("revision") == 2
    assert output(capsys) == {"database": "0001", "package_head": "0011", "up_to_date": False}


def test_backup_verify_and_restore(db: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run("migrate") == 0
    backup = tmp_path / "backups" / "b.db"
    assert run("backup", str(backup)) == 0
    assert run("verify", str(backup)) == 0
    capsys.readouterr()
    assert run("restore", str(backup)) == 2  # refuses without --yes
    assert "--yes" in capsys.readouterr().err
    assert run("restore", str(backup), "--yes") == 0


def test_engine_errors_exit_non_zero_with_json(
    db: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run("verify", str(tmp_path / "missing.db")) == 1
    assert json.loads(capsys.readouterr().err)["type"] == "InvalidRequest"


def test_bootstrap_admin_is_one_time(db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run("bootstrap-admin", "--label", "ops") == 0
    assert str(output(capsys)["token"]).startswith("lpk_")
    assert run("bootstrap-admin") == 1
    assert "already exists" in capsys.readouterr().err


def test_gen_secret_key_smoke_and_purge(db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run("gen-secret-key") == 0
    assert len(capsys.readouterr().out.strip()) == 44  # a Fernet key
    assert run("smoke") == 0
    smoke = output(capsys)
    assert smoke["ok"] is True and smoke["schema_revision"] == "0011"
    assert run("purge") == 0
    assert output(capsys) == {"jobs_deleted": 0, "usage_rows_deleted": 0}


async def _running_job(db: Path) -> Job:
    store = await SqlStore.connect(f"sqlite+aiosqlite:///{db}")
    try:
        job = Job(
            id=uuid4(), actor_id="alice", application_id="default", operation_ref=None, mode="immediate",
            app="example/echo", capability=None, model=None, state="running", attempts=(), failure=None,
            result=None, usage=None, created_at=FixedClock().now(), completed_at=None,
        )  # fmt: skip
        await store.create_job(job, None)
        return job
    finally:
        await store.close()


async def _stored_job(db: Path, job: Job) -> Job | None:
    store = await SqlStore.connect(f"sqlite+aiosqlite:///{db}", migrate=False)
    try:
        return await store.get_job(job.id)
    finally:
        await store.close()


def test_smoke_never_opens_the_configured_database(
    db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """#112 C23: smoke started an engine on LPB_DATABASE_URL, whose restart recovery marked a live job
    failed. It uses a throwaway SQLite file instead, migrates it, starts, checks health and stops."""
    job = asyncio.run(_running_job(db))
    assert run("smoke") == 0
    assert output(capsys)["ok"] is True
    stored = asyncio.run(_stored_job(db, job))
    assert stored is not None and stored.state == "running" and stored.failure is None
    missing = tmp_path / "never" / "created.db"
    monkeypatch.setenv("LPB_DATABASE_URL", f"sqlite+aiosqlite:///{missing}")
    assert run("smoke") == 0 and not missing.parent.exists()


def test_store_only_commands_read_only_the_database_setting(
    db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """#112 C23: migrate, revision, backup and restore need no discovery address, and another setting
    that would not load does not stop them."""
    monkeypatch.delenv("LPB_DISCOVERY_URL")
    monkeypatch.setenv("LPB_AUTH", "jwt")  # without LPB_JWT_ISSUER: the engine's settings would refuse it
    assert run("migrate") == 0
    assert run("revision") == 0
    backup = tmp_path / "b.db"
    assert run("backup", str(backup)) == 0
    assert run("restore", str(backup), "--yes") == 0
    capsys.readouterr()


def test_engine_commands_that_never_serve_need_no_discovery_address(
    db: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """#112 C23: purge, bootstrap-admin and smoke build an engine without LPB_DISCOVERY_URL; serve, and
    any engine an application starts from its settings, still require it."""
    monkeypatch.delenv("LPB_DISCOVERY_URL")
    assert run("bootstrap-admin") == 0
    assert run("purge") == 0
    assert run("smoke") == 0
    capsys.readouterr()
    with pytest.raises(ValidationError, match="discovery_url"):
        run("serve")


def test_gen_docs_writes_then_checks(db: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "generated"
    assert run("gen-docs", "--check", "--out", str(out)) == 1  # nothing written yet: stale
    assert "stale generated docs" in capsys.readouterr().err
    assert run("gen-docs", "--out", str(out)) == 0
    assert {p.name for p in out.iterdir()} == {
        "openapi.json",
        "db-schema.md",
        "interfaces.md",
        "contracts.md",
    }
    assert run("gen-docs", "--check", "--out", str(out)) == 0
    assert "erDiagram" in (out / "db-schema.md").read_text()
    assert "<<interface>>" in (out / "interfaces.md").read_text()


def test_serve_runs_the_rest_app(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import uvicorn

    served: list[object] = []

    async def fake_serve(self: uvicorn.Server) -> None:
        served.append(self.config.app)

    monkeypatch.setattr(uvicorn.Server, "serve", fake_serve)
    assert run("serve") == 0
    assert served and type(served[0]).__name__ == "FastAPI"


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exited:
        run("--version")
    assert exited.value.code == 0
    assert capsys.readouterr().out.strip()
