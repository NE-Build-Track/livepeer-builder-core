"""Create the dev stack's service secrets once (adapted from the Batteries compose sample).

Writes deploy/dev-stack/secrets/: Batteries credentials (operator, a least-privilege engine credential,
the signer webhook, Kafka users) and the signer's webhook and Kafka files. You still add rpc-url,
keystore.json and keystore-password yourself (mode 0600).
"""

import os
import secrets
from pathlib import Path

os.umask(0o077)
target = Path(__file__).resolve().parents[1] / "secrets"
target.mkdir(mode=0o700, exist_ok=True)
if any(target.iterdir()):
    raise SystemExit("secrets/ must be empty; refusing to replace credentials")
operator, engine, webhook, reader, producer = (secrets.token_hex(32) for _ in range(5))
creds = f'''[[credentials]]
id = "operator"
secret = "{operator}"
[credentials.management]
allow = ["grants.*", "allocations.*", "api_keys.*", "sessions.*",
         "settlements.read", "usage.read", "ledger.read", "escrow.read"]

[[credentials]]
id = "builder-engine"
secret = "{engine}"
[credentials.management]
allow = ["allocations.create", "allocations.fund", "allocations.read", "allocations.status",
         "allocations.revoke", "api_keys.create", "api_keys.read", "api_keys.revoke", "usage.read"]

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
    "clearinghouse.toml": creds,
    "operator-token": operator,
    "engine-token": engine,
    "webhook-headers": f"Livepeer-Clearinghouse-Token:{webhook}",
    "webhook-url": "http://127.0.0.1:8080/v1/signer/authorize",
    "kafka-username": "producer",
    "kafka-password": producer,
}
for name, value in files.items():
    (target / name).write_text(value)
print(  # noqa: T201
    "Created secrets/. Add rpc-url, keystore.json and keystore-password "
    "(mode 0600; password without a trailing newline)."
)
