"""A request is read by the names it sends, or refused (#163 review).

After the rename, ``POST /v1/clearinghouse/keys`` ignored a field it did not know and put a key in the
tenant ``default`` when the body named none: a client still sending ``application_id: "acme"`` got a key
for another tenant, with a 201. So every request body refuses a field it does not know, and no body or
query parameter puts a request in a tenant the request did not name. A ``tenant_id`` filter that is
absent narrows nothing; it never stands for a tenant.
"""

from collections.abc import Iterator
from typing import Any

from fastapi.routing import APIRoute
from pydantic import BaseModel
from pydantic_core import PydanticUndefined

from livepeer_builder.http import create_app
from tests.conftest import EngineFactory
from tests.structure.test_route_params import parameters

TENANT = "tenant_id"


def routes(app: Any) -> Iterator[APIRoute]:
    yield from (route for route in app.routes if isinstance(route, APIRoute))


def body_model(route: APIRoute) -> type[BaseModel] | None:
    model = route.body_field.field_info.annotation if route.body_field is not None else None
    return model if isinstance(model, type) and issubclass(model, BaseModel) else None


def defaults_a_tenant(default: object) -> bool:
    return default is not PydanticUndefined and default is not None


async def test_every_request_body_refuses_a_field_it_does_not_know(make_engine: EngineFactory) -> None:
    engine = await make_engine()  # API keys and the grant desk on, so every route exists
    lenient: list[str] = []
    checked = 0
    for route in routes(create_app(engine, manage_lifecycle=False)):
        model = body_model(route)
        if model is None:
            continue
        checked += 1
        if model.model_config.get("extra") != "forbid":
            lenient.append(f"{sorted(route.methods)} {route.path}: {model.__name__}")
    assert checked >= 10  # the bodies today; fewer means the walk missed some
    assert not lenient, (
        "These bodies ignore a field they do not know, so a client that still sends an old name, or "
        "misspells one, is answered as if it had not sent it. Give each model_config "
        'ConfigDict(extra="forbid") (livepeer_builder/http/bodies.py):\n' + "\n".join(lenient)
    )


async def test_no_request_is_put_in_a_tenant_it_did_not_name(make_engine: EngineFactory) -> None:
    engine = await make_engine()
    defaulted: list[str] = []
    for route in routes(create_app(engine, manage_lifecycle=False)):
        where = f"{sorted(route.methods)} {route.path}"
        for kind, field in parameters(route.dependant):
            if field.alias == TENANT and defaults_a_tenant(field.field_info.default):
                defaulted.append(f"{where}: {kind} parameter {TENANT} = {field.field_info.default!r}")
        model = body_model(route)
        held = model.model_fields.get(TENANT) if model is not None else None
        if held is not None and defaults_a_tenant(held.default):
            defaulted.append(f"{where}: body field {TENANT} = {held.default!r}")
    assert not defaulted, (
        "These requests are put in a tenant when they name none, so a client that sends the tenant under "
        "an old name lands in another one. Make tenant_id required, or None for a filter that narrows "
        "nothing:\n" + "\n".join(defaulted)
    )
