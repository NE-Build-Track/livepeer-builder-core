"""The dev stack README states the Batteries permissions the engine needs, and they match what
``deploy/dev-stack/config/init-secrets.py`` writes. Any deployment copies that list from the README."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INIT_SECRETS = ROOT / "deploy" / "dev-stack" / "config" / "init-secrets.py"
README = ROOT / "deploy" / "dev-stack" / "README.md"


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
