"""Every third-party import is a declared dependency (docs/exec-plans/active/dependency-injection-refactor.md, step 6).

A module the package imports but does not declare works only while something else happens to install
it. Each top-level import in ``src/livepeer_builder`` must map to a distribution in ``[project]
dependencies``, or in the extra that owns the package it is imported from (``http/`` is ``[server]``).
"""

import ast
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "livepeer_builder"
# Import name -> distribution name. A new third-party import needs a line here.
DISTRIBUTIONS = {
    "aiohttp": "aiohttp",
    "alembic": "alembic",
    "cryptography": "cryptography",
    "fastapi": "fastapi",
    "httpx": "httpx",
    "livepeer_gateway": "livepeer-gateway",
    "pydantic": "pydantic",
    "sqlalchemy": "sqlalchemy",
    "starlette": "starlette",
    "uvicorn": "uvicorn",
}
# Package directory -> the extra its imports may also come from.
EXTRAS = {"http": "server"}


def distribution(requirement: str) -> str:
    match = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", requirement)
    assert match, requirement
    return re.sub(r"[-_.]+", "-", match.group()).lower()


def declared() -> tuple[set[str], dict[str, set[str]]]:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    core = {distribution(r) for r in project["dependencies"]}
    extras = {
        name: {distribution(r) for r in requirements}
        for name, requirements in project.get("optional-dependencies", {}).items()
    }
    return core, extras


def third_party_imports(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
        if isinstance(node, ast.Import):
            names |= {alias.name.partition(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.partition(".")[0])
    return {name for name in names if name not in sys.stdlib_module_names and name != "livepeer_builder"}


def test_every_third_party_import_is_declared() -> None:
    core, extras = declared()
    unmapped: set[str] = set()
    undeclared: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        area = path.relative_to(SRC).parts[0]
        allowed = core | extras.get(EXTRAS.get(area, ""), set())
        for name in sorted(third_party_imports(path)):
            if name not in DISTRIBUTIONS:
                unmapped.add(name)
            elif DISTRIBUTIONS[name] not in allowed:
                where = (
                    f"[project.optional-dependencies] {EXTRAS[area]}"
                    if area in EXTRAS
                    else "[project] dependencies"
                )
                undeclared.append(
                    f"{path.relative_to(ROOT)} imports {name}: declare {DISTRIBUTIONS[name]} in {where}"
                )
    assert not unmapped, (
        f"add these imports to DISTRIBUTIONS with their distribution name: {sorted(unmapped)}"
    )
    assert not undeclared, "\n".join(undeclared)
