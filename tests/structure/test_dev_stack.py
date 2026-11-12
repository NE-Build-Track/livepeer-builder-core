"""The dev stack starts and the engine can reach it.

The README states the Batteries permissions the engine needs, and they match what
``deploy/dev-stack/config/init-secrets.py`` writes; any deployment copies that list from the README.
The signer, discovery and Batteries listen where their callers call them, and every upstream source
the stack builds is a commit docs/references/upstream-baseline.md records.
"""

import ipaddress
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STACK = ROOT / "deploy" / "dev-stack"
INIT_SECRETS = STACK / "config" / "init-secrets.py"
README = STACK / "README.md"
BASELINE = ROOT / "docs" / "references" / "upstream-baseline.md"


def _written(credential: str) -> set[str]:
    """The ``allow`` list init-secrets.py writes for one credential id."""
    block = INIT_SECRETS.read_text().split(f'id = "{credential}"', 1)[1].split("[[credentials]]", 1)[0]
    allow = block.split("allow = [", 1)[1].split("]", 1)[0]
    return set(re.findall(r'"([^"]+)"', allow))


def _documented(credential: str) -> set[str]:
    """The permissions on the README bullet that starts with this credential id."""
    text = README.read_text()
    item = text.split(f"  - `{credential}` (", 1)[1].split("\n  - ", 1)[0].split("\n- ", 1)[0]
    return set(re.findall(r"`([a-z_]+\.[a-z_*]+)`", item))


def test_the_readme_lists_the_engine_permissions_init_secrets_writes() -> None:
    for credential in ("builder-engine", "grant-desk"):
        written = _written(credential)
        assert written, f"init-secrets.py writes no allow list for {credential}"
        assert _documented(credential) == written, (
            f"deploy/dev-stack/README.md lists other {credential} permissions"
        )


def _host_port(bind: str) -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, int]:
    host, _, port = bind.removeprefix("unsafe@").rpartition(":")
    return ipaddress.ip_address(host.strip("[]")), int(port)


def _engine_url_port(text: str, setting: str, host: str) -> int:
    match = re.search(rf"{setting}[=:]\s*http://{host}:(\d+)", text)
    assert match, f"{setting} does not point at {host}"
    return int(match.group(1))


def test_the_signer_and_discovery_listen_where_the_engine_calls_them() -> None:
    """livepeer/node f45d83f (boa v1.0.4) refuses a non-loopback bind without ``unsafe@`` and exits. The
    engine calls both over the compose network, so neither may listen on loopback. Their metrics stay on
    loopback: those ports are not published."""
    engine_calls = {
        "signer": _engine_url_port((STACK / ".env.example").read_text(), "LPB_SIGNER_URL", "clearinghouse"),
        "discovery": _engine_url_port((STACK / "compose.yaml").read_text(), "LPB_DISCOVERY_URL", "discovery"),
    }
    for name, port in engine_calls.items():
        config = tomllib.loads((STACK / "config" / f"{name}.toml").read_text())
        listen, metrics = config["Listen"], config["MetricsListen"]
        assert listen.startswith("unsafe@"), f"{name}.toml Listen {listen!r} needs the unsafe@ prefix"
        host, listen_port = _host_port(listen)
        assert not host.is_loopback, f"{name}.toml Listen {listen!r} is loopback; the engine cannot reach it"
        assert listen_port == port, f"{name}.toml listens on {listen_port}; the engine calls {port}"
        assert _host_port(metrics)[0].is_loopback, (
            f"{name}.toml MetricsListen {metrics!r} must stay on loopback"
        )


def _batteries_bind(bind: str) -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, int]:
    """Batteries (boa.HTTPBind) reads ``:port`` as loopback."""
    return _host_port(f"127.0.0.1{bind}" if bind.startswith(":") else bind)


def test_batteries_listens_where_the_engine_and_signer_call_it() -> None:
    """Clearinghouse v0.0.3 (boa.HTTPBind, clearinghouse-batteries#44) removed ``UnsafeHTTPBind``; it
    ignores the key, and exits on a non-loopback bind without ``unsafe@`` on that address. The
    engine calls the management API over the compose network; the signer shares Batteries' network
    namespace and calls the webhook on loopback, so the webhook stays there."""
    config = tomllib.loads((STACK / "config" / "clearinghouse.toml").read_text())
    assert "UnsafeHTTPBind" not in config, (
        "clearinghouse.toml sets UnsafeHTTPBind, which Batteries v0.0.3 removed; "
        "prefix the non-loopback bind with unsafe@ instead"
    )
    management = config["EnableManagementAPI"]
    assert management.startswith("unsafe@"), f"EnableManagementAPI {management!r} needs the unsafe@ prefix"
    host, port = _batteries_bind(management)
    assert not host.is_loopback, f"EnableManagementAPI {management!r} is loopback; the engine cannot reach it"
    engine_port = _engine_url_port((STACK / ".env.example").read_text(), "LPB_BATTERIES_URL", "clearinghouse")
    assert port == engine_port, f"the management API listens on {port}; the engine calls {engine_port}"

    webhook = config["EnableAuthWebhook"]
    host, port = _batteries_bind(webhook)
    assert host.is_loopback, f"EnableAuthWebhook {webhook!r} must stay on loopback (only the signer calls it)"
    signer_calls = re.search(r'"webhook-url": "http://127\.0\.0\.1:(\d+)/', INIT_SECRETS.read_text())
    assert signer_calls, "init-secrets.py writes no loopback webhook-url for the signer"
    assert port == int(signer_calls.group(1)), (
        f"the webhook listens on {port}; the signer calls {signer_calls.group(1)}"
    )


def test_every_upstream_source_is_a_baseline_commit() -> None:
    """Each git build context in compose.yaml is pinned to a full commit listed in upstream-baseline.md,
    and its image tag names that commit, so a pin bump moves the source, the tag and the record together."""
    compose = (STACK / "compose.yaml").read_text()
    baseline = BASELINE.read_text()
    pins = re.findall(r"image: \S+:(\S+)\n\s+build:\n\s+context: https://github\.com/\S+\.git#(\S+)", compose)
    assert pins, "compose.yaml builds no upstream source from a pinned git context"
    for tag, commit in pins:
        assert re.fullmatch(r"[0-9a-f]{40}", commit), f"compose.yaml pins {commit!r}; use the full commit"
        assert commit.startswith(tag), f"image tag {tag!r} does not name the commit it builds ({commit})"
        assert commit in baseline, f"compose.yaml builds {commit}, which upstream-baseline.md does not record"
