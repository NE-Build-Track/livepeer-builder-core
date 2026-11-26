"""Create the clearinghouse project's service secrets once, and its .env.

Writes deploy/clearinghouse/secrets/ (git-ignored, mode 0700): Batteries' credential registry (the
engine's two credentials, the signer's webhook credential and the Kafka users) and the signer's webhook
and Kafka files. Then writes deploy/clearinghouse/.env from .env.example, with the engine's two Batteries
credentials filled in. The engine gets no LPB_SECRET_KEY: in its clearinghouse role it hands each
allocation's key to the caller and stores none. Nothing is printed but the names of what is left to do.

You still add secrets/rpc-url (mode 0600, no trailing newline) and config/discovery.toml, and set the
four WALLET_ variables in .env.
"""

import os
import secrets
from pathlib import Path

os.umask(0o077)
project = Path(__file__).resolve().parents[1]
target = project / "secrets"
env_file = project / ".env"
target.mkdir(mode=0o700, exist_ok=True)
if any(target.iterdir()):
    raise SystemExit("secrets/ must be empty; refusing to replace credentials")
if env_file.exists():
    raise SystemExit(".env exists; refusing to replace it")
engine, grant_desk, webhook, reader, producer = (secrets.token_hex(32) for _ in range(5))
creds = f'''[[credentials]]
id = "builder-engine"
secret = "{engine}"
[credentials.management]
allow = ["grants.fund", "allocations.create", "allocations.fund", "allocations.read",
         "allocations.status", "allocations.revoke", "api_keys.create", "api_keys.read",
         "api_keys.revoke", "usage.read"]

[[credentials]]
id = "grant-desk"
secret = "{grant_desk}"
[credentials.management]
allow = ["grants.read", "grants.create", "grants.fund", "grants.status", "allocations.read",
         "escrow.read"]

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
'''
files = {
    "batteries.toml": creds,
    "engine-token": engine,
    "grant-desk-token": grant_desk,
    "webhook-headers": f"Livepeer-Clearinghouse-Token:{webhook}",
    "webhook-url": "http://127.0.0.1:8080/v1/signer/authorize",
    "kafka-username": "producer",
    "kafka-password": producer,
}
for name, value in files.items():
    (target / name).write_text(value)

filled = {"LPB_BATTERIES_TOKEN": engine, "LPB_BATTERIES_GRANTS_TOKEN": grant_desk}
lines = []
for line in (project / ".env.example").read_text().splitlines():
    name = line.split("=", 1)[0]
    lines.append(f"{name}={filled[name]}" if name in filled else line)
env_file.write_text("\n".join(lines) + "\n")
print(  # noqa: T201
    "Created secrets/ and .env. Still to do: add secrets/rpc-url (mode 0600, no trailing newline), copy "
    "config/discovery.example.toml to config/discovery.toml and name the orchestrators there, and set "
    "WALLET_KEY_DIR, WALLET_KEYSTORE_FILE, WALLET_PASSWORD_FILE and WALLET_ADDRESS in .env."
)
