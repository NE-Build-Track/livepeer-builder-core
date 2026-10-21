"""TLS verification is never turned off in the engine (D42).

Discovery and the paid call verify orchestrator certificates with the process trust store. An
orchestrator the store does not trust is left out of discovery; the fix is a trusted certificate on the
orchestrator, never an exception in the engine.
"""

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "livepeer_builder"

UNVERIFIED_KEYWORDS = {"ssl": False, "verify_ssl": False, "verify": False}
UNVERIFIED_NAMES = {"CERT_NONE", "_create_unverified_context", "_SSL_CONTEXT_UNVERIFIED"}


def violations(tree: ast.AST) -> list[str]:
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg in UNVERIFIED_KEYWORDS:
            if isinstance(node.value, ast.Constant) and node.value.value is UNVERIFIED_KEYWORDS[node.arg]:
                found.append(f"line {node.value.lineno}: {node.arg}={node.value.value!r}")
        elif isinstance(node, ast.Attribute) and node.attr in UNVERIFIED_NAMES:
            found.append(f"line {node.lineno}: {node.attr}")
        elif isinstance(node, ast.Name) and node.id in UNVERIFIED_NAMES:
            found.append(f"line {node.lineno}: {node.id}")
        elif (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Constant)
            and node.value.value is False
        ):
            for target in node.targets:
                if isinstance(target, ast.Attribute) and target.attr == "check_hostname":
                    found.append(f"line {node.lineno}: check_hostname = False")
    return found


def test_tls_verification_is_never_disabled() -> None:
    for path in sorted(SRC.rglob("*.py")):
        found = violations(ast.parse(path.read_text(), filename=str(path)))
        assert not found, (
            f"{path.relative_to(SRC.parent)} turns TLS verification off ({found}). Leave verification on: "
            "an orchestrator whose certificate the trust store rejects is excluded at discovery (D42)."
        )


def test_the_check_finds_each_form() -> None:
    source = """
session(ssl=False)
client(verify=False)
connector(verify_ssl=False)
mode = ssl.CERT_NONE
context = ssl._create_unverified_context()
context.check_hostname = False
"""
    assert len(violations(ast.parse(source))) == 6
