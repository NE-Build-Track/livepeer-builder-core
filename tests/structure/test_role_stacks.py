"""The clearinghouse project and the enterprise project (#169, D87): two Compose projects that run a
payment clearinghouse and an enterprise that uses it, side by side on one host.

They are not part of CI and cannot be started here, so this file holds what a start would show too late:
each engine's settings load in its role; the clearinghouse publishes its public listener and nothing
else; Batteries runs its on-chain listener and the grants credential has the permissions the tenant desk
and the wallet report need; the services listen where the engine calls them; the wallet's key folder is
mounted read-only from a variable; and nothing real is committed.

The files are read as text, as tests/structure/test_dev_stack.py reads the dev stack's.
"""

import ipaddress
import re
import tomllib
from pathlib import Path

from livepeer_builder import EngineSettings

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy"
CLEARINGHOUSE = DEPLOY / "clearinghouse"
ENTERPRISE = DEPLOY / "enterprise"
BASELINE = ROOT / "docs" / "references" / "upstream-baseline.md"
VARIABLE = re.compile(r"\$\{(\w+)(?::[-?+]([^}]*))?\}")
GRANTS_CREDENTIAL = {
    "grants.read",
    "grants.create",
    "grants.fund",
    "grants.status",
    "allocations.read",
    "escrow.read",
}


def compose(project: Path) -> str:
    return (project / "compose.yaml").read_text()


def service(project: Path, name: str) -> str:
    """The text of one service of a project's compose.yaml: from its key to the next key at its depth."""
    services = compose(project).split("\nservices:\n", 1)[1]
    services = re.split(r"\n(?=\S)", services, maxsplit=1)[0]  # up to the next top-level key
    blocks = re.split(r"\n(?=  [\w-]+:)", "\n" + services)
    found = [block for block in blocks if block.lstrip("\n").startswith(f"  {name}:")]
    assert len(found) == 1, f"{project.name}/compose.yaml has no service {name}"
    return found[0]


def example_env(project: Path) -> dict[str, str]:
    lines = (project / ".env.example").read_text().splitlines()
    pairs = (line.split("=", 1) for line in lines if "=" in line and not line.startswith("#"))
    return {name: value for name, value in pairs}


def engine_environment(project: Path) -> dict[str, str]:
    """The engine's ``LPB_*`` settings as Compose would pass them with the example environment."""
    given = example_env(project)

    def resolve(match: re.Match[str]) -> str:
        return given.get(match.group(1)) or (match.group(2) or "")

    settings: dict[str, str] = {}
    for name, value in re.findall(r"^      (LPB_\w+): (.*)$", service(project, "engine"), flags=re.M):
        value = VARIABLE.sub(resolve, value.split("  #")[0].strip()).strip('"')
        settings[name] = value
    return settings


# -- each engine is in its role -----------------------------------------------------------------------------


def test_the_clearinghouse_projects_engine_loads_in_its_clearinghouse_role() -> None:
    environment = engine_environment(CLEARINGHOUSE)
    settings = EngineSettings.from_env(environment)
    assert settings.role == "clearinghouse"
    assert settings.http_private_port == 8081
    assert "LPB_HTTP_PRIVATE_HOST" not in environment and settings.http_private_host == "127.0.0.1", (
        "the private listener stays on the engine container's own loopback: no network reaches it"
    )
    assert settings.signer_url and settings.batteries_url
    assert settings.batteries_token is not None and settings.batteries_grants_token is not None
    assert settings.secret_key is not None
    assert not [name for name in environment if name.startswith("LPB_CLEARINGHOUSE_")]


def test_the_enterprise_projects_engine_loads_in_its_work_role_and_knows_only_the_clearinghouse() -> None:
    environment = engine_environment(ENTERPRISE)
    settings = EngineSettings.from_env(environment)
    assert settings.role == "work" and settings.uses_clearinghouse
    assert settings.clearinghouse_url == "http://clearinghouse:8080"
    assert settings.secret_key is not None
    held = [name for name in environment if name.startswith(("LPB_BATTERIES_", "LPB_SIGNER_"))]
    assert not held, f"the enterprise's engine holds nothing of Batteries or a signer: {held}"
    assert "LPB_DISCOVERY_URL" not in environment  # the clearinghouse's runner list is the default (D87)
    assert settings.discovery_url == "http://clearinghouse:8080/v1/clearinghouse/discovery"
    assert settings.http_private_port is None
    services = re.findall(r"^  ([\w-]+):$", compose(ENTERPRISE).split("\nservices:\n", 1)[1], flags=re.M)
    assert services[0] == "engine" and "batteries" not in services and "signer" not in services


# -- what is published ----------------------------------------------------------------------------------------


def test_the_clearinghouse_publishes_its_public_listener_and_nothing_else() -> None:
    published = re.findall(r'^\s+- "?([^"\n]*:\d+)"?.*$', compose(CLEARINGHOUSE), flags=re.M)
    mappings = [entry for entry in published if re.search(r":\d+$", entry) and "//" not in entry]
    assert mappings == ["127.0.0.1:${CLEARINGHOUSE_PUBLIC_PORT:-8180}:8080"], mappings
    for name in ("batteries", "signer", "discovery", "private"):
        assert "ports:" not in service(CLEARINGHOUSE, name), (
            f"{name} is published nowhere: only the engine's public listener is"
        )
    assert service(CLEARINGHOUSE, "engine").count("ports:") == 1
    # the private listener is reached in the engine's own network namespace, and from nowhere else
    assert "network_mode: service:engine" in service(CLEARINGHOUSE, "private")
    assert "profiles: [admin]" in service(CLEARINGHOUSE, "private")


def test_the_two_projects_and_the_dev_stack_run_side_by_side() -> None:
    names = [
        re.search(r"^name: (\S+)$", compose(project), flags=re.M) for project in (CLEARINGHOUSE, ENTERPRISE)
    ]
    assert [name.group(1) for name in names if name] == ["lpb-clearinghouse", "lpb-enterprise"]
    dev = (DEPLOY / "dev-stack" / "compose.yaml").read_text()
    assert "name: livepeer-builder-dev" in dev
    host_ports = {
        "clearinghouse": example_env(CLEARINGHOUSE)["CLEARINGHOUSE_PUBLIC_PORT"],
        "enterprise": example_env(ENTERPRISE)["ENTERPRISE_PORT"],
    }
    taken = set(re.findall(r"127\.0\.0\.1:(\d+):", dev))
    assert len(set(host_ports.values())) == 2 and not set(host_ports.values()) & taken
    # The one network both are on carries the public listener alone, under a name of its own.
    assert "name: lpb-clearinghouse-edge" in compose(CLEARINGHOUSE)
    edge = compose(ENTERPRISE).split("\nnetworks:\n", 1)[1]
    assert "external: true" in edge and "name: lpb-clearinghouse-edge" in edge
    for name in ("batteries", "discovery"):
        assert "networks: [inside]" in service(CLEARINGHOUSE, name), f"{name} is on the inside network only"
    assert "network_mode: service:batteries" in service(CLEARINGHOUSE, "signer")


# -- Batteries and the signer ---------------------------------------------------------------------------------


def test_batteries_runs_its_onchain_listener_with_an_rpc_address_and_the_wallets_address_from_outside_git() -> (
    None
):
    batteries = service(CLEARINGHOUSE, "batteries")
    for needed in (
        "--enable-onchain-listener",
        "--rpc-url-file",
        "/run/secrets/rpc-url",
        "--signer-addresses",
    ):
        assert needed in batteries, f"the batteries service lacks {needed}: the wallet report would be empty"
    assert "${WALLET_ADDRESS:?" in batteries


def _written(credential: str) -> set[str]:
    script = (CLEARINGHOUSE / "config" / "init-secrets.py").read_text()
    block = script.split(f'id = "{credential}"', 1)[1].split("[[credentials]]", 1)[0]
    return set(re.findall(r'"([^"]+)"', block.split("allow = [", 1)[1].split("]", 1)[0]))


def test_the_grants_credential_has_what_the_tenant_desk_and_the_wallet_report_need() -> None:
    assert _written("grant-desk") == GRANTS_CREDENTIAL
    assert "grants.fund" in _written("builder-engine") and not [
        permission for permission in _written("builder-engine") if permission.endswith(".*")
    ]
    readme = (CLEARINGHOUSE / "README.md").read_text()
    for credential in ("builder-engine", "grant-desk"):
        item = readme.split(f"- `{credential}` (", 1)[1].split("\n- ", 1)[0].split("\n\n", 1)[0]
        assert set(re.findall(r"`([a-z_]+\.[a-z_*]+)`", item)) == _written(credential), credential


def _port(bind: str) -> tuple[bool, int]:
    """Whether a bind address is loopback, and its port. Batteries reads ``:port`` as loopback."""
    host, _, port = bind.removeprefix("unsafe@").rpartition(":")
    return ipaddress.ip_address((host or "127.0.0.1").strip("[]")).is_loopback, int(port)


def test_the_services_listen_where_the_engine_calls_them() -> None:
    environment = engine_environment(CLEARINGHOUSE)
    signer = tomllib.loads((CLEARINGHOUSE / "config" / "signer.toml").read_text())
    batteries = tomllib.loads((CLEARINGHOUSE / "config" / "batteries.toml").read_text())
    discovery = tomllib.loads((CLEARINGHOUSE / "config" / "discovery.example.toml").read_text())
    calls = {
        "the signer": (environment["LPB_SIGNER_URL"], "batteries", signer["Listen"]),
        "Batteries": (environment["LPB_BATTERIES_URL"], "batteries", batteries["EnableManagementAPI"]),
        "discovery": (environment["LPB_DISCOVERY_URL"], "discovery", discovery["Listen"]),
    }
    for what, (url, host, bind) in calls.items():
        called = re.fullmatch(rf"http://{host}:(\d+)(/discovery)?", url)
        assert called, f"the engine calls {what} at another service than {host}"
        loopback, port = _port(bind)
        assert bind.startswith("unsafe@") and not loopback, f"{what} listens on loopback: {bind!r}"
        assert port == int(called.group(1)), f"{what} listens on {port}; the engine calls {called.group(1)}"
    assert _port(signer["MetricsListen"])[0] and _port(discovery["MetricsListen"])[0]
    # The webhook stays on loopback: only the signer calls it, from the same network namespace.
    loopback, port = _port(batteries["EnableAuthWebhook"])
    script = (CLEARINGHOUSE / "config" / "init-secrets.py").read_text()
    webhook = re.search(r'"webhook-url": "http://127\.0\.0\.1:(\d+)/', script)
    assert loopback and webhook and port == int(webhook.group(1))
    assert batteries["CredsFile"] == "/run/secrets/batteries.toml" and '"batteries.toml": creds' in script
    assert "KeystoreFile" not in signer  # named in compose.yaml, in the wallet's key folder


def test_every_upstream_source_is_a_baseline_commit() -> None:
    text = compose(CLEARINGHOUSE)
    baseline = BASELINE.read_text()
    images = dict(re.findall(r"^x-(\w+)-image: &\S+ \S+:(\S+)$", text, flags=re.M))
    commits = re.findall(r"context: https://github\.com/livepeer/(\S+)\.git#(\S+)", text)
    assert {repository for repository, _ in commits} == {"clearinghouse-batteries", "node"}
    for repository, commit in commits:
        tag = images["batteries" if repository == "clearinghouse-batteries" else "node"]
        assert re.fullmatch(r"[0-9a-f]{40}", commit), f"compose.yaml pins {commit!r}; use the full commit"
        assert commit.startswith(tag), f"image tag {tag!r} does not name the commit it builds ({commit})"
        assert commit in baseline, f"compose.yaml builds {commit}, which upstream-baseline.md does not record"


# -- nothing real is committed --------------------------------------------------------------------------------


def test_the_wallets_key_folder_is_mounted_read_only_from_a_variable() -> None:
    signer = service(CLEARINGHOUSE, "signer")
    mount = signer.split("source: ${WALLET_KEY_DIR:?", 1)
    assert len(mount) == 2, "the signer mounts the wallet's key folder from WALLET_KEY_DIR"
    assert "target: /run/wallet" in mount[1] and "read_only: true" in mount[1]
    assert (
        "/run/wallet/${WALLET_KEYSTORE_FILE:?" in signer and "/run/wallet/${WALLET_PASSWORD_FILE:?" in signer
    )
    for name in ("batteries", "discovery", "engine", "private"):
        assert "WALLET_KEY_DIR" not in service(CLEARINGHOUSE, name), f"{name} is given the wallet's key"


def test_the_committed_files_hold_placeholders_and_no_real_address_or_path() -> None:
    env = example_env(CLEARINGHOUSE) | example_env(ENTERPRISE)
    secrets = sorted(name for name in env if name.endswith(("_TOKEN", "_KEY")))
    assert secrets == [
        "LPB_BATTERIES_GRANTS_TOKEN",
        "LPB_BATTERIES_TOKEN",
        "LPB_CLEARINGHOUSE_TOKEN",
        "LPB_SECRET_KEY",
    ]
    assert example_env(CLEARINGHOUSE)["LPB_SECRET_KEY"].startswith("replace-with-")
    for name in secrets:
        assert env[name].startswith("replace-with-"), f"{name} in a .env.example is not a placeholder"
    assert set(env["WALLET_ADDRESS"]) == {"0", "x"}
    assert env["WALLET_KEY_DIR"] == "/path/to/the/wallet/key/folder"
    nodes = tomllib.loads((CLEARINGHOUSE / "config" / "discovery.example.toml").read_text())["Nodes"]
    assert nodes and all(re.fullmatch(r"https://[\w.-]+\.example(:\d+)?", node) for node in nodes), (
        "discovery.example.toml names a real orchestrator: real addresses go in config/discovery.toml, "
        "which is git-ignored"
    )
    committed = [
        path for folder in (CLEARINGHOUSE, ENTERPRISE) for path in sorted(folder.rglob("*")) if path.is_file()
    ]
    committed.append(DEPLOY / "README.md")
    ignored = ("secrets", ".env", "discovery.toml")
    for path in committed:
        if path.name in ignored or "secrets" in path.parts:
            continue  # git-ignored, present only where the projects were set up
        text = path.read_text()
        assert not re.search(r"/home/|~/|\.livepeer/", text), f"{path.relative_to(ROOT)} names a local path"
        assert not re.search(r"@[\w-]+\.[a-z]{2,}", text.replace("unsafe@", "")), f"{path.relative_to(ROOT)}"


def test_what_is_not_committed_is_ignored_by_git_and_kept_out_of_the_image() -> None:
    gitignore = (ROOT / ".gitignore").read_text().splitlines()
    for line in (
        "deploy/clearinghouse/secrets/",
        "deploy/clearinghouse/.env",
        "deploy/clearinghouse/config/discovery.toml",
        "deploy/enterprise/secrets/",
        "deploy/enterprise/.env",
    ):
        assert line in gitignore, f".gitignore lacks {line}"
    dockerignore = (ROOT / ".dockerignore").read_text().splitlines()
    for line in ("deploy/clearinghouse/secrets/", "deploy/enterprise/secrets/", "**/.env"):
        assert line in dockerignore, (
            f".dockerignore lacks {line}: the engine image is built from the repository root"
        )
