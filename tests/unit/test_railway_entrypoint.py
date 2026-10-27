"""The Railway entrypoint rewrites Neon URLs for asyncpg and migrates every deploy."""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ENTRYPOINT = ROOT / "deploy" / "railway" / "engine" / "entrypoint.py"


def _load():
    spec = importlib.util.spec_from_file_location("railway_engine_entrypoint", ENTRYPOINT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


entrypoint = _load()


def test_neon_url_becomes_an_asyncpg_url() -> None:
    raw = "postgresql://builder:secret@ep.example.neon.tech/builder?sslmode=require&channel_binding=require"
    assert entrypoint.asyncpg_url(raw) == (
        "postgresql+asyncpg://builder:secret@ep.example.neon.tech/builder?ssl=require"
    )


def test_postgres_scheme_and_an_existing_ssl_parameter_are_kept() -> None:
    raw = "postgres://builder:secret@ep.example/builder?ssl=require&channel_binding=require"
    assert entrypoint.asyncpg_url(raw) == "postgresql+asyncpg://builder:secret@ep.example/builder?ssl=require"


def test_an_asyncpg_url_drops_channel_binding() -> None:
    raw = "postgresql+asyncpg://builder:secret@ep.example/builder?sslmode=require&channel_binding=require"
    assert entrypoint.asyncpg_url(raw) == "postgresql+asyncpg://builder:secret@ep.example/builder?ssl=require"


def test_a_railway_url_only_changes_scheme() -> None:
    raw = "postgresql://postgres:secret@postgres.railway.internal:5432/railway"
    assert (
        entrypoint.asyncpg_url(raw)
        == "postgresql+asyncpg://postgres:secret@postgres.railway.internal:5432/railway"
    )


def test_other_schemes_are_rejected() -> None:
    with pytest.raises(SystemExit, match="postgresql URL"):
        entrypoint.asyncpg_url("sqlite:///tmp/engine.db")


def test_preview_and_production_both_migrate(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def record(command: str) -> int:
        calls.append(command)
        return 0

    monkeypatch.setattr(entrypoint, "lpb", record)
    for name in ("preview", "production"):
        calls.clear()
        monkeypatch.setenv("RAILWAY_ENVIRONMENT_NAME", name)
        assert entrypoint.migrate() == 0
        assert calls == ["migrate", "revision"]


def test_migrate_stops_when_the_upgrade_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def record(command: str) -> int:
        calls.append(command)
        return 7

    monkeypatch.setattr(entrypoint, "lpb", record)
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_NAME", "preview")
    assert entrypoint.migrate() == 7
    assert calls == ["migrate"]


def test_database_url_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LPB_DATABASE_URL", raising=False)
    with pytest.raises(SystemExit, match="required"):
        entrypoint.database_url()
