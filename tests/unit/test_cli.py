"""The `lpb` operator CLI, run in-process: migrations, revision, backup/restore, bootstrap, smoke, docs."""

import asyncio
import json
from pathlib import Path

import pytest

from livepeer_builder import cli
from livepeer_builder.store import create_engine, upgrade


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "engine.db"
    monkeypatch.setenv("LPB_DATABASE_URL", f"sqlite+aiosqlite:///{path}")
    monkeypatch.setenv("LPB_LOG_JSON", "false")
    monkeypatch.chdir(tmp_path)
    return path


def run(*argv: str) -> int:
    return cli.main(list(argv))


def output(capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    return dict(json.loads(capsys.readouterr().out))


def test_migrate_and_revision(db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run("migrate") == 0
    assert output(capsys) == {"revision": "0007"}
    assert run("revision") == 0
    assert output(capsys)["up_to_date"] is True


def test_revision_reports_a_database_that_is_behind(db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    async def at_first_revision() -> None:
        engine = create_engine(f"sqlite+aiosqlite:///{db}")
        await upgrade(engine, "0001")
        await engine.dispose()

    asyncio.run(at_first_revision())
    assert run("revision") == 2
    assert output(capsys) == {"database": "0001", "package_head": "0007", "up_to_date": False}


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
    assert smoke["ok"] is True and smoke["schema_revision"] == "0007"
    assert run("purge") == 0
    assert output(capsys) == {"jobs_deleted": 0, "usage_rows_deleted": 0}


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


def test_discovery_runs_the_discovery_service_from_lpd_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    import uvicorn

    served: list[object] = []

    async def fake_serve(self: uvicorn.Server) -> None:
        served.append((self.config.app, self.config.port))

    monkeypatch.setattr(uvicorn.Server, "serve", fake_serve)
    monkeypatch.setenv("LPD_ORCHESTRATORS", "https://a.example")
    monkeypatch.setenv("LPD_HTTP_PORT", "18939")
    assert run("discovery") == 0
    assert served and type(served[0][0]).__name__ == "Starlette" and served[0][1] == 18939


def test_discovery_refuses_settings_it_cannot_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("LPD_ORCHESTRATORS", raising=False)
    monkeypatch.setenv("LPD_DIRECTORY", "static")
    assert run("discovery") == 2
    assert "LPD_ORCHESTRATORS" in capsys.readouterr().err


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exited:
        run("--version")
    assert exited.value.code == 0
    assert capsys.readouterr().out.strip()
