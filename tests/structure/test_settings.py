"""Settings are values (docs/exec-plans/active/dependency-injection-refactor.md, decision 8).

Only ``EngineSettings.from_env`` in config.py reads the environment. Everything else receives an
``EngineSettings``, so embedders can configure the engine in code and tests never set env vars.
"""

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "livepeer_builder"
ENV_NAMES = {"environ", "environb", "getenv", "getenvb", "putenv", "unsetenv"}
# the discovery service is its own process (#45); discovery/settings.py is its config layer
ALLOWED = {SRC / "config.py", SRC / "discovery" / "settings.py"}


def env_reads(path: Path) -> list[int]:
    lines: list[int] = []
    for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
        os_attribute = (
            isinstance(node, ast.Attribute)
            and node.attr in ENV_NAMES
            and isinstance(node.value, ast.Name)
            and node.value.id == "os"
        )
        from_os = (
            isinstance(node, ast.ImportFrom)
            and node.module == "os"
            and any(alias.name in ENV_NAMES for alias in node.names)
        )
        if os_attribute or from_os:
            lines.append(node.lineno)
    return lines


def test_only_config_reads_the_environment() -> None:
    offenders = [
        f"{path.relative_to(SRC)}:{line}"
        for path in sorted(SRC.rglob("*.py"))
        if path not in ALLOWED
        for line in env_reads(path)
    ]
    assert not offenders, (
        "Settings are values: read the environment only in EngineSettings.from_env (config.py) "
        f"and pass EngineSettings in. Environment access found at: {offenders}"
    )
