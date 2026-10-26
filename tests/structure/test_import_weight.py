"""Importing the package is cheap (docs/exec-plans/active/dependency-injection-refactor.md, step 6).

``import livepeer_builder`` and ``import livepeer_builder.api`` load no database, HTTP, crypto, SDK or
server library. An application that types against the facade, or builds its own parts, pays only for
what it uses; the default adapters and the SQL store load when ``create`` builds them. The
``testing``, ``store`` and ``http`` packages may load heavy dependencies: they are imported on purpose.
"""

import json
import subprocess
import sys

import pytest

HEAVY = (
    "sqlalchemy",
    "alembic",
    "aiosqlite",
    "asyncpg",
    "httpx",
    "cryptography",
    "jwt",
    "livepeer_gateway",
    "aiohttp",
    "fastapi",
    "starlette",
    "uvicorn",
)
# A heavy module the light imports cannot avoid, with the reason. Empty today.
UNAVOIDABLE: dict[str, str] = {}


def loaded_by(module: str) -> set[str]:
    """The heavy top-level modules in ``sys.modules`` after importing ``module`` in a fresh interpreter."""
    code = (
        f"import json, sys, {module}\n"
        f"print(json.dumps(sorted({{name.partition('.')[0] for name in sys.modules}} & set({HEAVY!r}))))"
    )
    command = [sys.executable, "-c", code]  # this interpreter and a string built from constants
    done = subprocess.run(command, capture_output=True, text=True, check=True)  # noqa: S603
    return set(json.loads(done.stdout))


@pytest.mark.parametrize("module", ["livepeer_builder", "livepeer_builder.api"])
def test_importing_loads_no_heavy_dependency(module: str) -> None:
    heavy = sorted(loaded_by(module) - UNAVOIDABLE.keys())
    assert not heavy, (
        f"import {module} loads {heavy}. Import them where they are used (inside default_parts for a "
        "default adapter), or name the module in UNAVOIDABLE with the reason."
    )
