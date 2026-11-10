"""Compare the vendored discovery schema with livepeer/node. Weekly, not on pull requests.

Prints nothing and exits 0 when node's latest tag and ``main`` still match the pin. Exits 2 and
prints a report when either differs, so the workflow can open an issue.
"""

import base64
import hashlib
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

_PIN = Path("src/livepeer_builder/contracts/discovery.schema.pin.json")
_API = "https://api.github.com/repos/livepeer/node"


def _get(url: str) -> bytes:
    if urllib.parse.urlparse(url).scheme != "https":
        raise ValueError("the schema check only fetches https URLs")
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "livepeer-builder"},
    )
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def _sha(ref: str) -> str:
    quoted = urllib.parse.quote(ref, safe="")
    payload = json.loads(_get(f"{_API}/contents/docs/discovery.schema.json?ref={quoted}"))
    raw = base64.b64decode(payload["content"])
    return hashlib.sha256(raw).hexdigest()


def main() -> int:
    pin = json.loads(_PIN.read_text())
    latest = json.loads(_get(f"{_API}/releases/latest"))["tag_name"]
    main_sha = json.loads(_get(f"{_API}/commits/main"))["sha"]
    found = {"latest tag " + latest: _sha(latest), "main " + main_sha: _sha(main_sha)}
    drifted = {name: digest for name, digest in found.items() if digest != pin["sha256"]}
    if not drifted:
        print(f"discovery schema matches {pin['commit']}")
        return 0
    print(f"vendored {pin['commit']} sha256 {pin['sha256']}")
    for name, digest in drifted.items():
        print(f"differs at {name}: {digest}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
