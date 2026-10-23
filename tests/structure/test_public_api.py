"""The public API is complete: everything a consumer receives or catches is importable from the root.

A type reachable from the facade Protocols (``livepeer_builder.api``), directly or through the fields
of a returned model, must be exported from ``livepeer_builder``; so must every ``EngineError``. Ports a
consumer implements (``Authenticator``, ``EventFeed``, …) live in the public ``livepeer_builder.protocols``
module instead. When a consumer has to import from any other submodule, the public API is incomplete.
"""

import inspect
import typing
from types import UnionType

from pydantic import BaseModel

import livepeer_builder
from livepeer_builder import api, errors

ROLES = (api.JourneyApi, api.AdminApi, api.EngineApi)


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
