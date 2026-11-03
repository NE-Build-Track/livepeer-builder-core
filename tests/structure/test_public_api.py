"""The public API is complete, and the shipped applications use nothing else.

Everything a consumer receives or catches is importable from the root.

A type reachable from the facade Protocols (``livepeer_builder.api``), directly or through the fields
of a returned model, must be exported from ``livepeer_builder``; so must every ``EngineError``. Ports a
consumer implements (``Authenticator``, ``EventFeed``, …) live in the public ``livepeer_builder.protocols``
module instead. When a consumer has to import from any other submodule, the public API is incomplete.

The CLI, the stock server and the Railway entrypoint are applications built on the package, so they
import only the public API: the root, and the ``__all__`` of ``api``, ``protocols``, ``runtime``,
``http``, ``store`` and ``testing``. What they need, another application needs too.
"""

import ast
import importlib
import inspect
import typing
from pathlib import Path
from types import UnionType

import pytest
from pydantic import BaseModel

import livepeer_builder
from livepeer_builder import api, errors

ROLES = (api.ActorApi, api.AdminApi, api.EngineApi)
ROOT = Path(__file__).resolve().parents[2]
PUBLIC_MODULES = (
    "livepeer_builder",
    "livepeer_builder.api",
    "livepeer_builder.protocols",
    "livepeer_builder.runtime",
    "livepeer_builder.http",
    "livepeer_builder.discovery",
    "livepeer_builder.store",
    "livepeer_builder.testing",
)
APPLICATIONS = (
    ROOT / "src" / "livepeer_builder" / "cli.py",
    ROOT / "src" / "livepeer_builder" / "http" / "server.py",
    ROOT / "deploy" / "railway" / "engine" / "entrypoint.py",
)
# (application, private module) -> why the application may import it
PRIVATE_ALLOWED = {
    (
        "cli.py",
        "livepeer_builder.docs_diagrams",
    ): "lpb gen-docs is maintainers' doc tooling, not operator API",
    ("cli.py", "livepeer_builder.docs_gen"): "lpb gen-docs is maintainers' doc tooling, not operator API",
}


def _types_in(hint: object, seen: set[type]) -> None:
    origin = typing.get_origin(hint)
    if origin is not None or isinstance(hint, UnionType):
        for arg in typing.get_args(hint):
            _types_in(arg, seen)
        return
    if not isinstance(hint, type) or hint in seen or not hint.__module__.startswith("livepeer_builder"):
        return
    seen.add(hint)
    if issubclass(hint, BaseModel):
        for field in hint.model_fields.values():
            _types_in(field.annotation, seen)
    elif getattr(hint, "_is_protocol", False):
        for name, member in inspect.getmembers(hint):
            if name.startswith("_") and name != "__aiter__":
                continue
            target = member.fget if isinstance(member, property) else member
            if callable(target):
                for value in typing.get_type_hints(target).values():
                    _types_in(value, seen)


def reachable() -> set[type]:
    seen: set[type] = set()
    for role in ROLES:
        _types_in(role, seen)
    public_modules = ("livepeer_builder.api", "livepeer_builder.protocols")
    return {t for t in seen if not t.__module__.startswith(public_modules)}


def test_every_type_a_consumer_receives_is_exported_from_the_root() -> None:
    exported = set(livepeer_builder.__all__)
    missing = sorted(t.__name__ for t in reachable() if t.__name__ not in exported)
    assert not missing, (
        f"export these from livepeer_builder (src/livepeer_builder/__init__.py): {missing}. A type a "
        "consumer receives must be importable from the package root."
    )


def test_every_engine_error_is_exported_from_the_root() -> None:
    exported = set(livepeer_builder.__all__)
    engine_errors = {
        name
        for name, value in vars(errors).items()
        if isinstance(value, type) and issubclass(value, errors.EngineError)
    }
    missing = sorted(engine_errors - exported)
    assert not missing, f"export these errors from livepeer_builder so consumers can catch them: {missing}"


def public_names(module: str) -> set[str]:
    loaded = importlib.import_module(module)
    names = getattr(loaded, "__all__", None)
    return set(names) if names is not None else {name for name in vars(loaded) if not name.startswith("_")}


def private_imports(path: Path) -> list[str]:
    """Each ``livepeer_builder`` import in ``path`` that is not public, as ``line: what``."""
    own_package = "livepeer_builder.http" if path.parent.name == "http" else None
    found: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules = [(node.module, [alias.name for alias in node.names])]
        elif isinstance(node, ast.Import):
            modules = [(alias.name, []) for alias in node.names]
        else:
            continue
        for module, names in modules:
            if not module.startswith("livepeer_builder"):
                continue
            if own_package and module.startswith(own_package + "."):
                continue  # a module may use its own package's internals
            if (path.name, module) in PRIVATE_ALLOWED:
                continue
            if module not in PUBLIC_MODULES:
                found.append(f"{node.lineno}: {module}")
                continue
            hidden = sorted(set(names) - public_names(module))
            if hidden:
                found.append(f"{node.lineno}: {', '.join(hidden)} from {module}")
    return found


@pytest.mark.parametrize("path", APPLICATIONS, ids=lambda path: path.name)
def test_applications_import_only_the_public_api(path: Path) -> None:
    found = private_imports(path)
    assert not found, (
        f"{path.relative_to(ROOT)} imports outside the public API: {found}. Import it from the package "
        f"root or a public module ({', '.join(PUBLIC_MODULES)}), exporting it there if it is missing; a "
        "deliberate exception goes in PRIVATE_ALLOWED with its reason."
    )
