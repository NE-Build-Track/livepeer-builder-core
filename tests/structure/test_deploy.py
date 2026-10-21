"""Deployment invariants.

The Railway Batteries credential must match the dev stack and be able to fund a grant. The engine image
carries its own orchestrator list, and no deployment installs extra certificates (D42).
"""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy"
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


def test_railway_engine_image_carries_its_orchestrator_list() -> None:
    dockerfile = (DEPLOY / "railway" / "engine" / "Dockerfile").read_text()
    assert (
        "COPY deploy/railway/engine/orchestrators.json /etc/livepeer-builder/orchestrators.json" in dockerfile
    ), "deploy/railway/engine/Dockerfile must copy engine/orchestrators.json into the image."
    assert "LPB_ORCHESTRATOR_LIST=/etc/livepeer-builder/orchestrators.json" in dockerfile, (
        "deploy/railway/engine/Dockerfile must set LPB_ORCHESTRATOR_LIST to the list baked into the image."
    )


def test_orchestrator_lists_are_orch_webhook_json() -> None:
    for path in (
        DEPLOY / "railway" / "engine" / "orchestrators.json",
        DEPLOY / "dev-stack" / "config" / "orchestrators.json",
    ):
        payload = json.loads(path.read_text())
        assert isinstance(payload, list) and payload, (
            f"{path.relative_to(ROOT)} must be a non-empty JSON list."
        )
        for item in payload:
            assert isinstance(item, dict) and str(item.get("address", "")).startswith("https://"), (
                f"{path.relative_to(ROOT)}: every entry needs an https address, as "
                '{"address": "https://host:port"}.'
            )


def test_no_deployment_installs_extra_certificates() -> None:
    forbidden = (
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "REQUESTS_CA_BUNDLE",
        "pinned-certs",
        "pin-orchestrator-certs",
    )
    for path in sorted(DEPLOY.rglob("*")):
        if not path.is_file() or "secrets" in path.parts or path.name == ".env":
            continue  # git-ignored local material, never part of a deployment
        text = path.read_text(errors="replace")
        found = [name for name in forbidden if name in text]
        assert not found, (
            f"{path.relative_to(ROOT)} mentions {found}. Discovery and paid calls verify TLS with the system "
            "trust store; an orchestrator with an untrusted certificate is left out, not pinned (D42)."
        )
