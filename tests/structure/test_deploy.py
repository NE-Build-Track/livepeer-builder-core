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
