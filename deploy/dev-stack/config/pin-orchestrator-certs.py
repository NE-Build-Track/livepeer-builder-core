"""Pin the TLS certificates of the orchestrators listed in config/signer.toml (trust on first use).

Livepeer orchestrators commonly serve self-signed certificates. The livepeer/node signer verifies TLS
and has no skip-verify option, so it drops those discovery sources. This script fetches each
orchestrator's current leaf certificate into config/pinned-certs/. The signer trusts exactly those
certificates, plus the system CAs, through SSL_CERT_DIR. Re-run it when an orchestrator rotates its
certificate. Only pin hosts you intend to pay.
"""

import ssl
import tomllib
from pathlib import Path
from urllib.parse import urlparse

here = Path(__file__).resolve().parent
target = here / "pinned-certs"
target.mkdir(exist_ok=True)
orchestrators = tomllib.loads((here / "signer.toml").read_text())["Orchestrators"]
for url in orchestrators:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        continue
    port = parsed.port or 443
    try:
        pem = ssl.get_server_certificate((parsed.hostname, port), timeout=10)
    except OSError as exc:
        print(f"skip {parsed.hostname}:{port}: {exc}")  # noqa: T201
        continue
    (target / f"{parsed.hostname}.pem").write_text(pem)
    print(f"pinned {parsed.hostname}:{port}")  # noqa: T201
