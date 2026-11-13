"""No REST route declares a plain string path or query parameter (#113, C26).

Every id a route reads from its path or query string is stored text: at most 200 characters and no NUL,
as every record keeps it, so a bad one is a 422 before any query runs. A plain ``str`` lets a NUL reach
PostgreSQL, which refuses it with a 500. Each text parameter is checked by validating a 201-character
value and a NUL with the route's own parameter field.
"""

import types
import typing
from collections.abc import Iterator
from typing import Any

from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute

from livepeer_builder.http import create_app
from tests.conftest import EngineFactory


def parameters(dependant: Dependant) -> Iterator[tuple[str, Any]]:
    yield from (("path", field) for field in dependant.path_params)
    yield from (("query", field) for field in dependant.query_params)
    for dependency in dependant.dependencies:
        yield from parameters(dependency)


def is_text(annotation: Any) -> bool:
    if typing.get_origin(annotation) is typing.Annotated:
        return is_text(typing.get_args(annotation)[0])
    if typing.get_origin(annotation) in (typing.Union, types.UnionType):
        return any(is_text(arg) for arg in typing.get_args(annotation))
    return annotation is str


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
