"""The Railway Batteries entrypoint can add a read-only console credential."""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ENTRYPOINT = ROOT / "deploy" / "railway" / "batteries" / "entrypoint.py"

READER_ALLOW = [
    "grants.read",
    "allocations.read",
    "api_keys.read",
    "sessions.read",
    "settlements.read",
    "usage.read",
    "ledger.read",
    "escrow.read",
]


def _load():
    spec = importlib.util.spec_from_file_location("railway_batteries_entrypoint", ENTRYPOINT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


entrypoint = _load()

TOKENS = {
    "operator": "a" * 64,
    "engine": "b" * 64,
    "webhook": "c" * 64,
    "reader": "d" * 64,
    "producer": "e" * 64,
}


def document(console: str | None) -> str:
    return entrypoint.credentials_document(
        TOKENS["operator"],
        TOKENS["engine"],
        TOKENS["webhook"],
        TOKENS["reader"],
        TOKENS["producer"],
        console,
    )


def test_unset_console_reader_leaves_the_credential_file_unchanged() -> None:
    text = document(None)
    assert 'id = "console-reader"' not in text
    assert 'id = "operator"' in text
    assert 'id = "builder-engine"' in text


def test_console_reader_may_only_read() -> None:
    text = document("f" * 64)
    start = text.index('id = "console-reader"')
    block = text[start:]
    allow_at = block.index("allow = [")
    body = block[allow_at : block.index("]", allow_at)]
    allowed = [part.strip().strip('"') for part in body.removeprefix("allow = [").split(",")]
    assert allowed == READER_ALLOW
    assert not any(
        name.endswith(("create", "fund", "revoke", "status")) or name.endswith(".*") for name in allowed
    )


def test_console_reader_rejects_a_token_that_is_not_hex(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONSOLE_READER_TOKEN", "not-a-token")
    with pytest.raises(SystemExit, match="CONSOLE_READER_TOKEN"):
        entrypoint.console_reader_token()
