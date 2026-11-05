"""Railway discovery must run livepeer-onchain-discovery, not ``lpb discovery``."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "deploy" / "railway" / "discovery" / "Dockerfile"
ENTRYPOINT = ROOT / "deploy" / "railway" / "discovery" / "entrypoint.sh"
PIN = "737e5201e37b9ae4106183c6ccbaf32d698b5cfa"


def test_railway_discovery_image_runs_the_pinned_onchain_service() -> None:
    dockerfile = DOCKERFILE.read_text()
    assert "lpb discovery" not in dockerfile, (
        "deploy/railway/discovery/Dockerfile must not run lpb discovery. "
        "Railway discovery is livepeer-onchain-discovery."
    )
    assert PIN in dockerfile, (
        f"Pin github.com/NE-Build-Track/livepeer-discovery at {PIN} in deploy/railway/discovery/Dockerfile."
    )
    assert "LIVEPEER_ONCHAIN_DISCOVERY_LISTEN=[::]:8939" in dockerfile, (
        "Railway's private network is IPv6. Set "
        "LIVEPEER_ONCHAIN_DISCOVERY_LISTEN=[::]:8939 in the discovery image."
    )
    assert "LIVEPEER_ONCHAIN_DISCOVERY_NODES=/etc/lpd/orchestrators.txt" in dockerfile, (
        "Keep probing deploy/railway/discovery/orchestrators.txt via LIVEPEER_ONCHAIN_DISCOVERY_NODES."
    )
    assert "LIVEPEER_ONCHAIN_DISCOVERY_REFRESH_INTERVAL=30s" in dockerfile, (
        "Set LIVEPEER_ONCHAIN_DISCOVERY_REFRESH_INTERVAL=30s on the discovery image."
    )
    entrypoint = ENTRYPOINT.read_text()
    assert '[ -z "${LIVEPEER_ONCHAIN_DISCOVERY_RPC_URL:-}" ]' in entrypoint, (
        "deploy/railway/discovery/entrypoint.sh must exit when LIVEPEER_ONCHAIN_DISCOVERY_RPC_URL is empty."
    )
    assert 'echo "$LIVEPEER_ONCHAIN_DISCOVERY_RPC_URL"' not in entrypoint, (
        "The entrypoint must not print LIVEPEER_ONCHAIN_DISCOVERY_RPC_URL."
    )
    assert "exec /usr/local/bin/livepeer-onchain-discovery" in entrypoint, (
        "The entrypoint must exec livepeer-onchain-discovery."
    )
