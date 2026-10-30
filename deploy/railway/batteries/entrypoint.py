#!/usr/bin/env python3
"""Write Batteries config for Railway private networking and start clearinghouse.

Compose can set KAFKA_BIND (the default compose file uses 0.0.0.0:9092). Railway
leaves it unset, so Kafka binds this container's unique-local address. HTTP
listens on dual-stack [::].

The builder-engine credential may grants.fund. It has no grants.*. Fund grant
uses LPB_BATTERIES_TOKEN, which is ENGINE_TOKEN. A credential without
grants.fund makes Batteries answer 403 Forbidden.
"""

import ipaddress
import os
import re
import subprocess
import sys
from pathlib import Path

HEX_TOKEN = re.compile(r"^[0-9a-f]{64}$")


def require_token(name: str) -> str:
    value = os.environ.get(name, "")
    if HEX_TOKEN.fullmatch(value) is None:
        sys.exit(f"{name} must be 64 lowercase hex characters")
    return value


# The console's Batteries credential. Read permissions only, the same list as the
# local stack's console-reader. Absent unless CONSOLE_READER_TOKEN is set, so a
# production deploy that does not set it keeps today's credential file.
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


def console_reader_token() -> str | None:
    value = os.environ.get("CONSOLE_READER_TOKEN", "").strip()
    if not value:
        return None
    if HEX_TOKEN.fullmatch(value) is None:
        sys.exit("CONSOLE_READER_TOKEN must be 64 lowercase hex characters")
    return value


def credentials_document(
    operator: str,
    engine: str,
    webhook: str,
    reader: str,
    producer: str,
    console: str | None,
) -> str:
    creds = f"""[[credentials]]
id = "operator"
secret = "{operator}"
[credentials.management]
allow = ["grants.*", "allocations.*", "api_keys.*", "sessions.*",
         "settlements.read", "usage.read", "ledger.read", "escrow.read"]

[[credentials]]
id = "builder-engine"
secret = "{engine}"
[credentials.management]
allow = ["grants.fund", "allocations.create", "allocations.fund", "allocations.read",
         "allocations.status", "allocations.revoke", "api_keys.create", "api_keys.read",
         "api_keys.revoke", "usage.read"]

[[credentials]]
id = "signer"
secret = "{webhook}"
[credentials.webhook]
authorize = true

[[credentials]]
id = "accounting"
secret = "{reader}"
[credentials.kafka]
username = "accounting"
allow = ["read"]
accounting = true

[[credentials]]
id = "producer"
secret = "{producer}"
[credentials.kafka]
username = "producer"
allow = ["write"]
"""
    if console is None:
        return creds
    allow = ", ".join(f'"{name}"' for name in READER_ALLOW)
    return creds + f"""
[[credentials]]
id = "console-reader"
secret = "{console}"
[credentials.management]
allow = [{allow}]
"""


def local_ula() -> str:
    """Return this container's unique-local IPv6 address."""
    text = Path("/proc/net/if_inet6").read_text(encoding="ascii")
    found: list[ipaddress.IPv6Address] = []
    for line in text.splitlines():
        raw = line.split()[0]
        if len(raw) != 32:
            continue
        groups = [raw[index : index + 4] for index in range(0, 32, 4)]
        address = ipaddress.IPv6Address(":".join(groups))
        if address.is_private and not address.is_loopback and not address.is_link_local:
            found.append(address)
    if len(found) != 1:
        sys.exit(f"expected one unique-local address, found {len(found)}")
    return str(found[0])


def kafka_bind() -> str:
    configured = os.environ.get("KAFKA_BIND", "").strip()
    if not configured:
        return f"[{local_ula()}]:9092"
    if "\n" in configured or '"' in configured:
        sys.exit("KAFKA_BIND cannot contain quotes or newlines")
    return configured


def main() -> None:
    os.umask(0o077)
    operator = require_token("OPERATOR_TOKEN")
    engine = require_token("ENGINE_TOKEN")
    webhook = require_token("WEBHOOK_TOKEN")
    reader = require_token("KAFKA_READER_TOKEN")
    producer = require_token("KAFKA_PRODUCER_TOKEN")
    console = console_reader_token()
    bind = kafka_bind()

    secrets = Path("/run/secrets")
    secrets.mkdir(mode=0o700, exist_ok=True)
    Path("/data").mkdir(mode=0o700, exist_ok=True)
    creds = credentials_document(operator, engine, webhook, reader, producer, console)
    (secrets / "clearinghouse.toml").write_text(creds, encoding="utf-8")
    os.chmod(secrets / "clearinghouse.toml", 0o600)

    config_path = Path("/etc/clearinghouse.toml")
    config_path.write_text(
        "\n".join(
            [
                'DBPath = "/data/clearinghouse.db"',
                'CredsFile = "/run/secrets/clearinghouse.toml"',
                'EnableAuthWebhook = "[::]:8080"',
                'EnableManagementAPI = "[::]:8081"',
                "UnsafeHTTPBind = true",
                "EnableKafka = true",
                "EnableAccounting = true",
                f'KafkaBind = "{bind}"',
                'KafkaTopic = "livepeer-signing"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    print(f"batteries kafka bind {bind}", flush=True)  # noqa: T201
    # Pre-deploy cannot see the volume, so the schema is applied by the process
    # that mounts /data, before Kafka or HTTP start. `serve` migrates again;
    # that second pass is a no-op once this one has reached head.
    # The binary path is the image COPY destination, not PATH lookup.
    migrated = subprocess.run(
        [
            "/usr/local/bin/clearinghouse",
            "migrate",
            "up",
            "--db-path",
            "/data/clearinghouse.db",
        ],
        check=False,
    )
    if migrated.returncode != 0:
        sys.exit(migrated.returncode)
    os.execv(  # noqa: S606
        "/usr/local/bin/clearinghouse",
        ["/usr/local/bin/clearinghouse", "serve", "--config-file", str(config_path)],
    )


if __name__ == "__main__":
    main()
