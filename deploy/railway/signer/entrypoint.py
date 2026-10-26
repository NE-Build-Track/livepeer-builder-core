#!/usr/bin/env python3
"""Write the livepeer/node signer config and start livepeer-signer.

The signer only signs payments: discovery is the discovery service (D43), so
the config has no Orchestrators list.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

HEX_TOKEN = re.compile(r"^[0-9a-f]{64}$")
ACCOUNT = re.compile(r"^0x[0-9a-fA-F]{40}$")
DECIMAL = re.compile(r"^[0-9]+$")

DEFAULT_ACCOUNT = "0x0074780FefF1FD0277FAD6ccdb5a29908df6051F"
DEFAULT_RPC_URL = "http://nyc-router.eliteencoder.net:3517"
DEFAULT_MAX_HOURLY_PRICE = "1000000"
DEFAULT_MAX_FIXED_PRICE = "1000000"
DEFAULT_MAX_BATCH_EV = "100000000000000"
DEFAULT_MAX_TICKET_EV = "3000000000000"


def env_or(name: str, default: str) -> str:
    value = os.environ.get(name, "").strip()
    return value or default


def require_token(name: str) -> str:
    value = os.environ.get(name, "")
    if HEX_TOKEN.fullmatch(value) is None:
        sys.exit(f"{name} must be 64 lowercase hex characters")
    return value


def http_url(name: str, value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
        sys.exit(f"{name} must be an absolute http or https URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        sys.exit(f"{name} must not include credentials, a query, or a fragment")
    if '"' in value or "\n" in value:
        sys.exit(f"{name} contains characters that cannot be embedded in the config")
    return value


def write_secret(path: Path, contents: str) -> None:
    if contents.endswith("\n") or contents.endswith("\r"):
        sys.exit(f"{path.name} must not end with a newline")
    path.write_text(contents, encoding="utf-8")
    os.chmod(path, 0o600)


def main() -> None:
    os.umask(0o077)
    account = env_or("SIGNER_ACCOUNT", DEFAULT_ACCOUNT)
    if ACCOUNT.fullmatch(account) is None:
        sys.exit("SIGNER_ACCOUNT must be an 0x Ethereum address")
    rpc_url = http_url("SIGNER_RPC_URL", env_or("SIGNER_RPC_URL", DEFAULT_RPC_URL))
    hourly = env_or("SIGNER_MAX_HOURLY_PRICE", DEFAULT_MAX_HOURLY_PRICE)
    fixed = env_or("SIGNER_MAX_FIXED_PRICE", DEFAULT_MAX_FIXED_PRICE)
    ticket_ev = env_or("SIGNER_MAX_TICKET_EV", DEFAULT_MAX_TICKET_EV)
    batch_ev = env_or("SIGNER_MAX_BATCH_EV", DEFAULT_MAX_BATCH_EV)
    for name, value in (
        ("SIGNER_MAX_HOURLY_PRICE", hourly),
        ("SIGNER_MAX_FIXED_PRICE", fixed),
        ("SIGNER_MAX_TICKET_EV", ticket_ev),
        ("SIGNER_MAX_BATCH_EV", batch_ev),
    ):
        if DECIMAL.fullmatch(value) is None or value == "0":
            sys.exit(f"{name} must be a positive integer")

    batteries_host = os.environ.get("BATTERIES_PRIVATE_HOST", "").strip()
    if not batteries_host or "/" in batteries_host or ":" in batteries_host:
        sys.exit("BATTERIES_PRIVATE_HOST must be a hostname without a port")
    webhook = require_token("WEBHOOK_TOKEN")
    producer = require_token("KAFKA_PRODUCER_TOKEN")

    keystore_text = os.environ.get("SIGNER_KEYSTORE_JSON", "").strip()
    try:
        keystore = json.loads(keystore_text)
    except json.JSONDecodeError:
        sys.exit("SIGNER_KEYSTORE_JSON is not valid JSON")
    address = str(keystore.get("address", "")).lower()
    if address != account.lower().removeprefix("0x"):
        sys.exit("keystore address does not match SIGNER_ACCOUNT")
    password = os.environ.get("SIGNER_KEYSTORE_PASSWORD", "")
    if not password or "\n" in password or "\r" in password:
        sys.exit("SIGNER_KEYSTORE_PASSWORD must be a single line")

    secrets = Path("/run/secrets")
    secrets.mkdir(mode=0o700, exist_ok=True)
    Path("/data/node").mkdir(parents=True, mode=0o700, exist_ok=True)
    write_secret(secrets / "keystore.json", keystore_text)
    write_secret(secrets / "keystore-password", password)
    write_secret(secrets / "rpc-url", rpc_url)
    write_secret(secrets / "webhook-url", f"http://{batteries_host}:8080/v1/signer/authorize")
    write_secret(secrets / "webhook-headers", f"Livepeer-Clearinghouse-Token:{webhook}")
    write_secret(secrets / "kafka-username", "producer")
    write_secret(secrets / "kafka-password", producer)

    config_path = Path("/etc/signer.toml")
    config_path.write_text(
        "\n".join(
            [
                'Listen = "[::]:8937"',
                'MetricsListen = "[::1]:8938"',
                'Network = "arbitrum-one-mainnet"',
                f'Account = "{account}"',
                'RPCURLFile = "/run/secrets/rpc-url"',
                'KeystoreFile = "/run/secrets/keystore.json"',
                'KeystorePasswordFile = "/run/secrets/keystore-password"',
                f'MaxHourlyPrice = "{hourly}"',
                f'MaxFixedPrice = "{fixed}"',
                f'MaxTicketEV = "{ticket_ev}"',
                f'MaxBatchEV = "{batch_ev}"',
                'AuthWebhookFile = "/run/secrets/webhook-url"',
                'AuthWebhookHeadersFile = "/run/secrets/webhook-headers"',
                "",
                "[Kafka]",
                f'Broker = "kafka://{batteries_host}:9092"',
                'Topic = "livepeer-signing"',
                'AuthMethod = "scram-sha-512"',
                'UsernameFile = "/run/secrets/kafka-username"',
                'PasswordFile = "/run/secrets/kafka-password"',
                'OutboxDB = "/data/signer-events.sqlite"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    os.chmod(config_path, 0o600)
    print(f"signer account {account}", flush=True)  # noqa: T201
    # The outbox path is absolute. Migrate that file, not the default
    # events.sqlite under the data dir. Pre-deploy has no volume, so this runs
    # in the start container before livepeer-signer opens the outbox.
    migrate_config = secrets / "signer-migrate.toml"
    migrate_config.write_text(
        '[Kafka]\nOutboxDB = "/data/signer-events.sqlite"\n',
        encoding="utf-8",
    )
    os.chmod(migrate_config, 0o600)
    # The binary path is the image COPY destination. migrate_config is written above.
    migrated = subprocess.run(  # noqa: S603
        [
            "/usr/local/bin/livepeer-signer",
            "migrate",
            "up",
            "--config",
            str(migrate_config),
        ],
        check=False,
    )
    if migrated.returncode != 0:
        sys.exit(migrated.returncode)
    os.execv(  # noqa: S606
        "/usr/local/bin/livepeer-signer",
        [
            "/usr/local/bin/livepeer-signer",
            "--data-dir",
            "/data/node",
            "--config",
            str(config_path),
        ],
    )


if __name__ == "__main__":
    main()
