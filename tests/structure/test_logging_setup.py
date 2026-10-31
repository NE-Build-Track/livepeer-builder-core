"""Logging setup belongs to the application (python-dependency-injection guide, Rule 11).

A library that installs handlers or calls ``basicConfig`` takes a decision away from every application
that embeds it. Only ``runtime/logging.py`` touches the root logger, and only the shipped applications
(the CLI, the stock server and the Railway entrypoint) call ``configure_logging``.
"""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "livepeer_builder"
FILES = [*sorted(SRC.rglob("*.py")), ROOT / "deploy" / "railway" / "engine" / "entrypoint.py"]
SETS_UP_LOGGING = {SRC / "runtime" / "logging.py"}
APPLICATIONS = {
    SRC / "cli.py",
    SRC / "http" / "server.py",
    ROOT / "deploy" / "railway" / "engine" / "entrypoint.py",
}


def root_logger_changes(tree: ast.AST) -> list[int]:
    """Lines that reach the root logger: ``basicConfig``, ``logging.root`` or ``logging.getLogger()``."""
    lines: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "logging":
            if any(alias.name in ("basicConfig", "root") for alias in node.names):
                lines.append(node.lineno)
        elif (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "logging"
        ):
            if node.attr in ("basicConfig", "root"):
                lines.append(node.lineno)
        elif isinstance(node, ast.Call) and not node.args and not node.keywords:
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "getLogger":
                lines.append(node.lineno)
    return lines


def configure_logging_calls(tree: ast.AST) -> list[int]:
    lines: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = (
                func.id
                if isinstance(func, ast.Name)
                else func.attr
                if isinstance(func, ast.Attribute)
                else ""
            )
            if name == "configure_logging":
                lines.append(node.lineno)
    return lines


def test_only_runtime_logging_touches_the_root_logger() -> None:
    found = [
        f"{path.relative_to(ROOT)}:{line}"
        for path in FILES
        if path not in SETS_UP_LOGGING
        for line in root_logger_changes(ast.parse(path.read_text(), filename=str(path)))
    ]
    assert not found, (
        f"these reach the root logger: {found}. Log through logging.getLogger(__name__); handlers and "
        "levels are the application's choice, made by configure_logging in runtime/logging.py."
    )


def test_only_the_applications_call_configure_logging() -> None:
    found = [
        f"{path.relative_to(ROOT)}:{line}"
        for path in FILES
        if path not in APPLICATIONS
        for line in configure_logging_calls(ast.parse(path.read_text(), filename=str(path)))
    ]
    assert not found, (
        f"configure_logging is called from library code: {found}. Only the CLI, http/server.py's serve() "
        "and the Railway entrypoint configure logging; an embedding application configures its own."
    )
