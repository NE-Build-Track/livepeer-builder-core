"""No REST route declares a plain string, or an unbounded time, as a path or query parameter (#113, C26).

Every id a route reads from its path or query string is stored text: at most 200 characters and no NUL,
as every record keeps it, so a bad one is a 422 before any query runs. A plain ``str`` lets a NUL reach
PostgreSQL, which refuses it with a 500. Each text parameter is checked by validating a 201-character
value and a NUL with the route's own parameter field.
"""

import types
import typing
from collections.abc import Iterator
from datetime import datetime
from typing import Any

from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute
from pydantic import AwareDatetime, FutureDatetime, NaiveDatetime, PastDatetime

from livepeer_builder.http import create_app
from tests.conftest import EngineFactory


def parameters(dependant: Dependant) -> Iterator[tuple[str, Any]]:
    yield from (("path", field) for field in dependant.path_params)
    yield from (("query", field) for field in dependant.query_params)
    for dependency in dependant.dependencies:
        yield from parameters(dependency)


def holds(annotation: Any, kinds: tuple[Any, ...]) -> bool:
    if typing.get_origin(annotation) is typing.Annotated:
        return holds(typing.get_args(annotation)[0], kinds)
    if typing.get_origin(annotation) in (typing.Union, types.UnionType):
        return any(holds(arg, kinds) for arg in typing.get_args(annotation))
    return annotation in kinds


def is_text(annotation: Any) -> bool:
    return holds(annotation, (str,))


def is_time(annotation: Any) -> bool:
    """``datetime``, or one of pydantic's own datetime types, which are classes of their own."""
    return holds(annotation, (datetime, AwareDatetime, NaiveDatetime, PastDatetime, FutureDatetime))


async def test_no_route_declares_a_plain_string_path_or_query_parameter(make_engine: EngineFactory) -> None:
    engine = await make_engine()  # API keys and the grant desk on, so every route exists
    app = create_app(engine, manage_lifecycle=False)
    plain: list[str] = []
    checked = 0
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        for kind, field in parameters(route.dependant):
            if not is_text(field.field_info.annotation):
                continue
            checked += 1
            for bad in ("x" * 201, "a\x00b"):
                _, errors = field.validate(bad, {}, loc=(kind, field.alias))
                if not errors:
                    plain.append(f"{sorted(route.methods)} {route.path}: {kind} parameter {field.alias!r}")
                    break
    assert checked >= 20  # the route ids this covers today; fewer means the walk missed some
    assert not plain, (
        "These parameters take any string, so a NUL or an over-long id reaches the store (a 500 on "
        "PostgreSQL) instead of a 422. Declare each as stored text: PathId or QueryId from "
        "livepeer_builder/http/params.py, or Annotated[StoredText, Query(...)] (#113, C26):\n"
        + "\n".join(plain)
    )


async def test_no_route_declares_an_unbounded_time_as_a_path_or_query_parameter(
    make_engine: EngineFactory,
) -> None:
    """A time with an offset can lie outside the years a datetime holds once it is in UTC, and the store
    converts every time it is handed: an ``OverflowError``, a 500."""
    engine = await make_engine()
    app = create_app(engine, manage_lifecycle=False)
    unbounded: list[str] = []
    checked = 0
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        for kind, field in parameters(route.dependant):
            if not is_time(field.field_info.annotation):
                continue
            checked += 1
            for bad in ("0001-01-01T00:00:00+14:00", "9999-12-31T23:59:59-14:00"):
                _, errors = field.validate(bad, {}, loc=(kind, field.alias))
                if not errors:
                    unbounded.append(
                        f"{sorted(route.methods)} {route.path}: {kind} parameter {field.alias!r}"
                    )
                    break
    assert checked >= 3  # before, start and end today; fewer means the walk missed some
    assert not unbounded, (
        "These parameters take a time that UTC cannot hold, so it reaches the store and overflows there "
        "(a 500) instead of a 422. Declare each as QueryTime or QueryInstant from "
        "livepeer_builder/http/params.py:\n" + "\n".join(unbounded)
    )
