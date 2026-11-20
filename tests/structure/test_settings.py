"""Settings are values (docs/exec-plans/active/dependency-injection-refactor.md, decision 8).

Only ``EngineSettings.from_env`` in config.py reads the environment. Everything else receives an
``EngineSettings``, so embedders can configure the engine in code and tests never set env vars.

``per_actor`` is the only allocation mode (D82), so no setting chooses a mode or holds a shared key.
"""

import ast
from pathlib import Path

from livepeer_builder import EngineSettings

SRC = Path(__file__).resolve().parents[2] / "src" / "livepeer_builder"
ENV_NAMES = {"environ", "environb", "getenv", "getenvb", "putenv", "unsetenv"}
ALLOWED = {SRC / "config.py"}
# The settings of the per_application mode, and the one D74 planned and D82 replaced before it was built.
NOT_SETTINGS = ("allocation_mode", "shared_payment_key", "shared_allocation_id", "payment_operation")


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


def test_no_setting_chooses_an_allocation_mode_or_holds_a_shared_payment_key() -> None:
    present = sorted(set(NOT_SETTINGS) & set(EngineSettings.model_fields))
    assert not present, (
        f"EngineSettings has {present}. per_actor is the only allocation mode (D82 in "
        "docs/design-docs/m4-decisions.md): every actor pays from its own allocation, and an actor "
        "without one fails not_provisioned before any payment. Remove the setting. An application that "
        "wants one shared budget runs every job as a single actor."
    )
