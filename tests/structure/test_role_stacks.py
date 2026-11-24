"""The clearinghouse project and the enterprise project (#169, D87): two Compose projects that run a
payment clearinghouse and an enterprise that uses it, side by side on one host.

They are not part of CI and cannot be started here, so this file holds what a start would show too late:
each engine's settings load in its role; the clearinghouse publishes its engine's public listener and
its signer, and nothing else; the signer keeps its Batteries authorization webhook, the only check at
signing; the enterprise's engine is told the published signer's address; Batteries runs its on-chain
listener and the grants credential has the permissions the tenant desk and the wallet report need; the
services listen where the engine calls them; the wallet's key folder is mounted read-only from a
variable; and nothing real is committed.

The files are read as text, as tests/structure/test_dev_stack.py reads the dev stack's.
"""

import ipaddress
import re
import subprocess
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
    assert settings.batteries_url
    assert settings.batteries_token is not None and settings.batteries_grants_token is not None
    # where its wallet report reads the signer's readiness: the signer, inside the project
    assert settings.signer_url == "http://batteries:8937"
    assert "LPB_SECRET_KEY" not in environment and settings.secret_key is None, (
        "a clearinghouse hands each allocation's key to the caller and stores none: it has no secret key"
    )
    assert not [name for name in environment if name.startswith("LPB_CLEARINGHOUSE_")]


def test_the_enterprise_projects_engine_loads_in_its_work_role_and_knows_nothing_of_batteries() -> None:
    environment = engine_environment(ENTERPRISE)
    settings = EngineSettings.from_env(environment)
    assert settings.role == "work" and settings.uses_clearinghouse
    assert settings.clearinghouse_url == "http://clearinghouse:8080"
    assert settings.signer_url == "http://signer:8937", (
        "the enterprise's engine signs at the clearinghouse's published signer itself: LPB_SIGNER_URL"
    )
    assert settings.secret_key is not None, "it stores the keys the clearinghouse hands over"
    held = [name for name in environment if name.startswith("LPB_BATTERIES_")]
    assert not held, f"the enterprise's engine holds nothing of Batteries: {held}"
    assert "LPB_DISCOVERY_URL" not in environment  # the clearinghouse's runner list is the default (D87)
    assert settings.discovery_url == "http://clearinghouse:8080/v1/clearinghouse/discovery"
    assert settings.http_private_port is None
    services = re.findall(r"^  ([\w-]+):$", compose(ENTERPRISE).split("\nservices:\n", 1)[1], flags=re.M)
    assert services[0] == "engine" and "batteries" not in services and "signer" not in services


# -- what is published ----------------------------------------------------------------------------------------


def test_the_clearinghouse_publishes_its_public_listener_and_its_signer_and_nothing_else() -> None:
    published = re.findall(r'^\s+- "?([^"\n]*:\d+)"?.*$', compose(CLEARINGHOUSE), flags=re.M)
    mappings = [entry for entry in published if re.search(r":\d+$", entry) and "//" not in entry]
    assert mappings == [
        "127.0.0.1:${CLEARINGHOUSE_SIGNER_PORT:-8137}:8937",
        "127.0.0.1:${CLEARINGHOUSE_PUBLIC_PORT:-8180}:8080",
    ], mappings
    for name in ("batteries", "signer", "discovery", "private"):
        assert "ports:" not in service(CLEARINGHOUSE, name), (
            f"{name} publishes no port: the signer is published by signer-relay, and nothing of "
            "Batteries is published at all"
        )
    assert service(CLEARINGHOUSE, "engine").count("ports:") == 1
    assert service(CLEARINGHOUSE, "signer-relay").count("ports:") == 1
    # the private listener is reached in the engine's own network namespace, and from nowhere else
    assert "network_mode: service:engine" in service(CLEARINGHOUSE, "private")
    assert "profiles: [admin]" in service(CLEARINGHOUSE, "private")


def test_the_signer_is_published_by_a_relay_of_its_one_port() -> None:
    """The signer shares Batteries' network namespace (Kafka and the webhook are on its loopback), so
    putting that namespace on the edge network would publish Batteries' management API with it. A relay
    forwards the signer's main port and nothing else: no port of Batteries, and not the signer's metrics
    and readiness port."""
    relay = service(CLEARINGHOUSE, "signer-relay")
    signer = tomllib.loads((CLEARINGHOUSE / "config" / "signer.toml").read_text())
    batteries = tomllib.loads((CLEARINGHOUSE / "config" / "batteries.toml").read_text())
    _, signs_on = _port(signer["Listen"])
    targets = re.findall(r"TCP:([\w.-]+):(\d+)", relay)
    assert targets == [("batteries", str(signs_on))], f"the relay forwards to {targets}"
    assert re.findall(r"TCP-LISTEN:(\d+)", relay) == [str(signs_on)]
    private_ports = {
        _port(signer["MetricsListen"])[1],
        _port(batteries["EnableManagementAPI"])[1],
        _port(batteries["EnableAuthWebhook"])[1],
        int(batteries["KafkaBind"].rpartition(":")[2]),
    }
    assert signs_on not in private_ports
    assert not {int(port) for port in re.findall(r":(\d{4,5})\b", relay)} & private_ports, (
        "the relay names a port that stays private: the signer's metrics, or a port of Batteries"
    )
    assert _port(signer["MetricsListen"])[0], "the signer's metrics and readiness stay on loopback"
    # where enterprise engines find it: the edge network, under the name the enterprise project calls
    assert re.search(r"edge:\s*\n\s+aliases: \[signer\]", relay), "the relay is `signer` on the edge network"
    assert re.search(r"^      inside: \{\}$", relay, flags=re.M), "the relay reaches the signer on inside"
    for name in ("batteries", "discovery"):
        assert "edge" not in service(CLEARINGHOUSE, name), f"{name} is on the edge network"
    assert "network_mode: service:batteries" in service(CLEARINGHOUSE, "signer")
    # a third-party image in the signing path is named by its digest, and gets no privilege
    assert re.search(r"image: alpine/socat:[\d.]+@sha256:[0-9a-f]{64}$", relay, flags=re.M)
    assert "read_only: true" in relay and "cap_drop: [ALL]" in relay and 'user: "65534:65534"' in relay
    called = re.fullmatch(r"http://signer:(\d+)", engine_environment(ENTERPRISE)["LPB_SIGNER_URL"])
    assert called and int(called.group(1)) == signs_on


def test_the_published_signer_keeps_its_batteries_authorization_webhook() -> None:
    """Enterprise engines call the signer themselves, so the clearinghouse's engine is not in the path
    and the webhook is the only check at signing (D85 point 1): a signer published without it would sign
    for anybody."""
    signer = tomllib.loads((CLEARINGHOUSE / "config" / "signer.toml").read_text())
    assert signer["AuthWebhookFile"] == "/run/secrets/webhook-url"
    assert signer["AuthWebhookHeadersFile"] == "/run/secrets/webhook-headers"
    script = (CLEARINGHOUSE / "config" / "init-secrets.py").read_text()
    assert re.search(r'"webhook-url": "http://127\.0\.0\.1:\d+/v1/signer/authorize"', script)
    assert '"webhook-headers": f"Livepeer-Clearinghouse-Token:{webhook}"' in script
    credential = script.split('id = "signer"', 1)[1].split("[[credentials]]", 1)[0]
    assert "[credentials.webhook]" in credential and "authorize = true" in credential
    batteries = tomllib.loads((CLEARINGHOUSE / "config" / "batteries.toml").read_text())
    assert batteries["EnableAuthWebhook"] and batteries["EnableAccounting"] is True
    assert {"webhook-url", "webhook-headers"} <= secret_files("signer")  # read-only, each by itself


def test_the_two_projects_and_the_dev_stack_run_side_by_side() -> None:
    names = [
        re.search(r"^name: (\S+)$", compose(project), flags=re.M) for project in (CLEARINGHOUSE, ENTERPRISE)
    ]
    assert [name.group(1) for name in names if name] == ["lpb-clearinghouse", "lpb-enterprise"]
    dev = (DEPLOY / "dev-stack" / "compose.yaml").read_text()
    assert "name: livepeer-builder-dev" in dev
    host_ports = {
        "clearinghouse": example_env(CLEARINGHOUSE)["CLEARINGHOUSE_PUBLIC_PORT"],
        "signer": example_env(CLEARINGHOUSE)["CLEARINGHOUSE_SIGNER_PORT"],
        "enterprise": example_env(ENTERPRISE)["ENTERPRISE_PORT"],
    }
    taken = set(re.findall(r"127\.0\.0\.1:(\d+):", dev))
    assert len(set(host_ports.values())) == 3 and not set(host_ports.values()) & taken
    # The one network both are on carries the public listener and the signer's relay, and nothing else.
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


def test_the_signer_reads_the_password_without_its_trailing_newline() -> None:
    """The signer reads its password file as exact bytes, and a password file usually ends in a newline
    (the funded wallet's does). The signer's own start strips it into the container's memory: nothing is
    written to the key folder, which is read-only, or to the project."""
    signer = service(CLEARINGHOUSE, "signer")
    assert "--keystore-password-file /dev/shm/wallet-password" in signer
    assert "--keystore-password-file /run/wallet" not in signer, "the signer is given the file as it is"
    assert "umask 077" in signer and "> /dev/shm/wallet-password" in signer
    assert "exec /usr/local/bin/livepeer-signer" in signer, "the signer is the container's first process"


def test_a_service_that_reads_an_owner_only_file_runs_as_root() -> None:
    """The runbook works under ``umask 077``, so the key folder, ``secrets/`` and ``config/discovery.toml``
    are owner-only on the host. The images' own user could not read them."""
    for name in ("signer", "discovery"):
        assert 'user: "0:0"' in service(CLEARINGHOUSE, name), f"{name} cannot read an owner-only file"


def test_the_committed_files_hold_placeholders_and_no_real_address_or_path() -> None:
    env = example_env(CLEARINGHOUSE) | example_env(ENTERPRISE)
    secrets = sorted(name for name in env if name.endswith(("_TOKEN", "_KEY")))
    assert secrets == [
        "LPB_BATTERIES_GRANTS_TOKEN",
        "LPB_BATTERIES_TOKEN",
        "LPB_CLEARINGHOUSE_TOKEN",
        "LPB_SECRET_KEY",
    ]
    assert "LPB_SECRET_KEY" not in example_env(CLEARINGHOUSE)  # it stores no allocation's key
    assert example_env(ENTERPRISE)["LPB_SECRET_KEY"].startswith("replace-with-")
    assert example_env(ENTERPRISE)["LPB_SIGNER_URL"] == "http://signer:8937"
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
        for withdrawn in ("lpp_", "payment_key", "signer's two paths", "signer paths"):
            assert withdrawn not in text, (
                f"{path.relative_to(ROOT)} still says {withdrawn!r}: no engine serves a signer path, and "
                "an enterprise's engine signs at the published signer with the key it was handed"
            )


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


# -- who can read which credential (the reviewer's comments on #183) ----------------------------------------

SECRET_FILE = re.compile(
    r"- \{type: bind, source: \./secrets/([\w.-]+), target: /run/secrets/([\w.-]+), read_only: true, "
    r"bind: \{create_host_path: false\}\}"
)


def secret_files(name: str) -> set[str]:
    """The files of ``secrets/`` a service of the clearinghouse project is given, each mounted by itself,
    read-only, under its own name, and refused when it is not there."""
    mounts = SECRET_FILE.findall(service(CLEARINGHOUSE, name))
    assert all(source == target for source, target in mounts), f"{name} mounts a secret under another name"
    return {source for source, _ in mounts}


def test_no_service_is_given_the_secrets_folder_and_each_is_given_only_the_files_it_reads() -> None:
    """``secrets/`` holds every Batteries credential, and the runbook keeps the clearinghouse admin's key
    and each tenant's admin key there. The signer, which answers the internet through the relay and runs
    as root, was given the whole folder, and so was Batteries. Each is given the files its own
    configuration names, and nothing is given the folder."""
    text = compose(CLEARINGHOUSE)
    assert not re.search(r"\./secrets/?[:,\s]", text), "a service is given the whole secrets folder"
    named = set(
        re.findall(r'"/run/secrets/([\w.-]+)"', (CLEARINGHOUSE / "config" / "signer.toml").read_text())
    )
    assert named == {"rpc-url", "webhook-url", "webhook-headers", "kafka-username", "kafka-password"}
    assert secret_files("signer") == named
    batteries = tomllib.loads((CLEARINGHOUSE / "config" / "batteries.toml").read_text())
    registry = Path(batteries["CredsFile"])
    assert registry.parent == Path("/run/secrets")
    assert "- /run/secrets/rpc-url" in service(CLEARINGHOUSE, "batteries")  # its on-chain listener's
    assert secret_files("batteries") == {registry.name, "rpc-url"}
    for name in ("signer-relay", "discovery", "engine", "private"):
        given = service(CLEARINGHOUSE, name)
        assert "./secrets" not in given and "/run/secrets" not in given, f"{name} is given a file of secrets/"
    # so the keys the runbook keeps there reach no container
    for kept in ("batteries.toml", "engine-token", "grant-desk-token", "admin-key", "tenant-one-key"):
        assert kept not in secret_files("signer")


def runbook() -> str:
    return (DEPLOY / "README.md").read_text()


def test_the_runbook_never_empties_a_saved_key() -> None:
    """``command | jq -r .token > file`` empties the file before the command runs. A repeat of
    ``lpb bootstrap-admin`` is refused, a repeat of an onboarding or of a key answers without the token,
    and the one saved copy of the key was gone. Every key the runbook saves goes through ``keep``."""
    text = runbook()
    assert not re.search(r"token[^\n|]*>\s*\$[CE]S/", text), "a key is redirected straight into its file"
    assert not re.search(r">\s*\$[CE]S/[^\n]*\.json", text), "an answer that holds a key is written to a file"
    for saved in ("$CS/admin-key", "$CS/tenant-$1-key", "$ES/admin-key", "$ES/$1-key"):
        assert f"| keep {saved}" in text, f"{saved} is not saved through keep"


def keep(folder: Path, given: str, into: str = "a-key") -> tuple[int, str]:
    """Run the runbook's own ``keep`` on ``given``, and say how it ended and what the file holds."""
    definition = re.search(r"^keep\(\) \{.*?^\}$", runbook(), flags=re.S | re.M)
    assert definition is not None, "the runbook defines keep"
    script = f'umask 077\n{definition.group(0)}\nprintf \'%s\' "$1" | keep "$2"'
    ended = subprocess.run(  # noqa: S603 - the runbook's own function, on a file of this test
        ["/bin/bash", "-c", script, "keep", given, str(folder / into)],
        capture_output=True,
        text=True,
        check=False,
    )
    file = folder / into
    return ended.returncode, file.read_text() if file.exists() else "(no file)"


def test_keep_saves_a_token_and_leaves_a_saved_key_when_none_comes_back(tmp_path: Path) -> None:
    assert keep(tmp_path, "") == (1, "(no file)")  # nothing came back and nothing is saved: say so
    assert keep(tmp_path, "null") == (1, "(no file)")  # jq's word for a field that is not there
    assert keep(tmp_path, "lpk_first\n") == (0, "lpk_first\n")
    assert (tmp_path / "a-key").stat().st_mode & 0o777 == 0o600
    assert keep(tmp_path, "") == (0, "lpk_first\n")  # a refused repeat: the saved key stays
    assert keep(tmp_path, "null") == (0, "lpk_first\n")
    assert keep(tmp_path, "lpk_second") == (0, "lpk_second\n")  # a key that was issued replaces the old one
