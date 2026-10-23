"""Architecture invariants, enforced mechanically (ARCHITECTURE.md#layers).

Failures explain how to fix the violation, so an agent can repair it without asking.
"""

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "livepeer_builder"

# layer -> layers it may import (itself is always allowed)
ALLOWED: dict[str, set[str]] = {
    "utils": set(),
    "contracts": set(),
    "errors": {"contracts"},
    "protocols": {"contracts", "errors"},
    "api": {"contracts", "errors", "protocols"},
    "config": {"contracts", "errors"},
    "services": {"contracts", "errors", "protocols", "config", "utils"},
    "store": {"contracts", "errors", "protocols", "config", "utils"},
    "adapters": {"contracts", "errors", "protocols", "config", "utils"},
    # make_engine builds a real engine, so testing may import what runtime does
    "testing": {
        "contracts",
        "errors",
        "protocols",
        "api",
        "utils",
        "config",
        "runtime",
        "store",
        "adapters",
        "services",
    },
    "runtime": {
        "contracts",
        "errors",
        "protocols",
        "api",
        "config",
        "utils",
        "services",
        "store",
        "adapters",
    },
    "http": {"contracts", "errors", "api", "runtime", "config"},
    "entry": {"*"},
}

# third-party package -> the only modules (path prefixes under src/livepeer_builder) that may import it
CONFINED: dict[str, tuple[str, ...]] = {
    "livepeer_gateway": ("adapters/sdk.py",),
    "aiohttp": ("adapters/sdk.py",),
    "httpx": ("adapters/batteries.py",),
    "cryptography": ("adapters/secrets.py",),
    "sqlalchemy": ("store/",),
    "alembic": ("store/",),
    "asyncpg": ("store/",),
    "aiosqlite": ("store/",),
    "fastapi": ("http/",),
    "starlette": ("http/",),
    "uvicorn": ("http/server.py",),
}

REMEDIATION = {
    "services": "Services reach persistence and the network only through protocols in livepeer_builder.protocols.",
    "store": "The store implements protocols; it must not call services or adapters.",
    "adapters": "Adapters implement protocols; business rules belong in services.",
    "protocols": "Protocols describe interfaces with contract types only.",
    "contracts": "Contracts are the bottom layer and import nothing else in the package.",
    "http": "The HTTP layer calls BuilderEngine (runtime); it never reaches services or stores directly.",
    "testing": "Fakes implement protocols with contract types; they must not depend on implementations.",
}


def layer_of(path: Path) -> str:
    relative = path.relative_to(SRC)
    head = relative.parts[0]
    if head in ("cli.py", "docs_gen.py", "docs_diagrams.py", "__init__.py"):
        return "entry"
    return head.removesuffix(".py")


def module_files() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def imports_of(path: Path) -> list[tuple[str, int]]:
    tree = ast.parse(path.read_text())
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [(alias.name, node.lineno) for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.append((node.module, node.lineno))
    return found


@pytest.mark.parametrize("path", module_files(), ids=lambda p: str(p.relative_to(SRC)))
def test_layer_edges(path: Path) -> None:
    layer = layer_of(path)
    allowed = ALLOWED[layer]
    if "*" in allowed:
        return
    for module, line in imports_of(path):
        if not module.startswith("livepeer_builder."):
            continue
        target = module.split(".")[1]
        target_layer = "entry" if target in ("cli", "docs_gen", "docs_diagrams") else target
        if target_layer == layer or target_layer in allowed:
            continue
        pytest.fail(
            f"{path.relative_to(SRC)}:{line} imports {module} ({layer} -> {target_layer} is not an allowed edge). "
            f"{REMEDIATION.get(layer, '')} See ARCHITECTURE.md#layers."
        )


@pytest.mark.parametrize("path", module_files(), ids=lambda p: str(p.relative_to(SRC)))
def test_third_party_packages_are_confined(path: Path) -> None:
    relative = str(path.relative_to(SRC))
    for module, line in imports_of(path):
        package = module.split(".")[0]
        homes = CONFINED.get(package)
        if homes is None or any(relative.startswith(home) for home in homes):
            continue
        if package == "fastapi" and relative == "cli.py":
            continue
        pytest.fail(
            f"{relative}:{line} imports {package}, which is confined to {homes}. Wrap it behind a protocol "
            f"and implement that protocol in the owning module (ARCHITECTURE.md#third-party-boundaries)."
        )
