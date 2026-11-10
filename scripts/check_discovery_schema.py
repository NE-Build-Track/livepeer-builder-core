"""Compare the vendored discovery schema with livepeer/node. Weekly, not on pull requests.

``main`` is always compared. The latest release tag is compared once it contains the pinned commit:
until node tags a release after the pin, that tag predates the schema and is skipped (D63).

Prints a match line and exits 0 when every compared ref matches the pin. Exits 2 and prints a report
when the schema differs on a compared ref, or is absent there (HTTP 404), so the workflow can open an
issue. Any other HTTP error, or a network failure, is left uncaught and exits 1: the job fails and no
issue is opened.
"""

import base64
import hashlib
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_PIN = _ROOT / "src/livepeer_builder/contracts/discovery.schema.pin.json"
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


class SchemaUnavailable(Exception):
    """The compared ref answered, and it does not serve the schema file."""

    def __init__(self, ref: str, status: int) -> None:
        self.ref = ref
        self.status = status


def _sha(ref: str) -> str:
    quoted = urllib.parse.quote(ref, safe="")
    try:
        payload = json.loads(_get(f"{_API}/contents/docs/discovery.schema.json?ref={quoted}"))
    except urllib.error.HTTPError as error:
        if error.code != 404:  # a rate limit or an outage says nothing about the schema
            raise
        raise SchemaUnavailable(ref, error.code) from error
    raw = base64.b64decode(payload["content"])
    return hashlib.sha256(raw).hexdigest()


def _contains(tag: str, commit: str) -> bool:
    """Whether ``tag`` includes ``commit`` in its history (GitHub's compare of ``commit...tag``)."""
    quoted = urllib.parse.quote(tag, safe="")
    status = json.loads(_get(f"{_API}/compare/{commit}...{quoted}"))["status"]
    return status in ("ahead", "identical")


def main() -> int:
    pin = json.loads(_PIN.read_text())
    latest = json.loads(_get(f"{_API}/releases/latest"))["tag_name"]
    main_sha = json.loads(_get(f"{_API}/commits/main"))["sha"]
    refs = [("main " + main_sha, main_sha)]
    notes: list[str] = []
    if _contains(latest, pin["commit"]):
        refs.insert(0, ("latest tag " + latest, latest))
    else:
        notes.append(f"latest tag {latest} predates the pin; compared main only")
    found: dict[str, str] = {}
    missing: list[str] = []
    for name, ref in refs:
        try:
            found[name] = _sha(ref)
        except SchemaUnavailable as unavailable:
            missing.append(f"{name}: HTTP {unavailable.status}, schema not on this ref")
    drifted = {name: digest for name, digest in found.items() if digest != pin["sha256"]}
    for note in notes:
        print(note)
    if not drifted and not missing:
        print(f"discovery schema matches {pin['commit']}")
        return 0
    print(f"vendored {pin['commit']} sha256 {pin['sha256']}")
    for name, digest in drifted.items():
        print(f"differs at {name}: {digest}")
    for line in missing:
        print(line)
    return 2


if __name__ == "__main__":
    sys.exit(main())
