"""The Railway Batteries credential must match the dev stack and be able to fund a grant."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENTRYPOINT = ROOT / "deploy" / "railway" / "batteries" / "entrypoint.py"
INIT_SECRETS = ROOT / "deploy" / "dev-stack" / "config" / "init-secrets.py"


def builder_engine_allow(text: str) -> list[str]:
    start = text.index('id = "builder-engine"')
    block = text[start : text.index("[[credentials]]", start)]
    allow_at = block.index("allow = [")
    body = block[allow_at : block.index("]", allow_at)]
    return re.findall(r'"([^"]+)"', body)


def test_railway_builder_engine_can_fund_grants() -> None:
    railway = builder_engine_allow(ENTRYPOINT.read_text())
    local = builder_engine_allow(INIT_SECRETS.read_text())
    assert "grants.fund" in railway, (
        "deploy/railway/batteries/entrypoint.py builder-engine allow list must include "
        "grants.fund. Without it, Fund grant returns provider HTTP 403."
    )
    assert "grants.*" not in railway, (
        "builder-engine must not receive grants.*. Keep grants.fund, and leave grants.* "
        "on the operator credential."
    )
    assert railway == local, (
        "The Railway builder-engine allow list drifted from "
        "deploy/dev-stack/config/init-secrets.py. Update both together."
    )


def test_console_reader_is_optional_and_read_only() -> None:
    text = ENTRYPOINT.read_text()
    assert "CONSOLE_READER_TOKEN" in text, (
        "The console reader is optional: omit the credential when CONSOLE_READER_TOKEN is unset "
        "so production keeps today's file."
    )
    start = text.index("READER_ALLOW = [")
    body = text[start : text.index("]", start)]
    names = re.findall(r'"([^"]+)"', body)
    assert names and all(name.endswith(".read") for name in names), (
        "The Railway console reader may only hold *.read permissions. "
        "Do not give it grants.*, create, fund, or revoke."
    )
