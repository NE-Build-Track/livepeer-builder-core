"""#113 C26: every id a route reads from its path or query string is stored text.

A NUL in a path or query id reached the store, where PostgreSQL refuses it: a 500. An id longer than any
record holds was looked up anyway, and answered 404. Both are now a 422 before any query runs.
``tests/structure/test_route_params.py`` keeps every route that way.
"""

from collections.abc import AsyncIterator
from urllib.parse import quote

import httpx
import pytest

from livepeer_builder import BuilderEngine
from livepeer_builder.http import create_app
from tests.conftest import EngineFactory

ALLOCATION = "/v1/admin/applications/{application}/actors/{actor}/allocation"
REF = {"operation_ref": "r1"}
# (method, path template, query parameter or None, body): {id} is where the bad id goes.
ROUTES = [
    ("GET", "/v1/offerings", "app", None),
    ("GET", "/v1/runners", "app", None),
    ("GET", "/v1/costs/summary", "actor_id", None),
    ("GET", "/v1/allowance", "actor_id", None),
    ("GET", "/v1/admin/keys", "application_id", None),
    ("GET", "/v1/admin/keys/{id}", None, None),
    ("POST", "/v1/admin/keys/{id}/rotate", None, REF),
    ("POST", "/v1/admin/keys/{id}/revoke", None, None),
    ("GET", "/v1/admin/actors", "application_id", None),
    ("GET", ALLOCATION.format(application="{id}", actor="alice"), None, None),
    ("GET", ALLOCATION.format(application="default", actor="{id}"), None, None),
    ("POST", ALLOCATION.format(application="default", actor="{id}") + "/fund",
     None, REF | {"amount": {"amount": "1", "currency": "USD"}}),
    ("POST", ALLOCATION.format(application="{id}", actor="alice") + "/status", None, {"status": "paused"}),
    ("POST", ALLOCATION.format(application="default", actor="{id}") + "/revoke", None, None),
    ("POST", ALLOCATION.format(application="{id}", actor="alice") + "/rotate-key", None, REF),
    ("GET", "/v1/admin/reconciliation", "application_id", None),
    ("GET", "/v1/admin/reconciliation", "actor_id", None),
]  # fmt: skip


@pytest.fixture
async def api(make_engine: EngineFactory) -> AsyncIterator[tuple[httpx.AsyncClient, dict[str, str]]]:
    engine: BuilderEngine = await make_engine()
    admin = await engine.keys.bootstrap_admin()
    app = create_app(engine, manage_lifecycle=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://engine") as client:
        yield client, {"Authorization": f"Bearer {admin.token}"}


@pytest.mark.parametrize(
    ("method", "template", "query", "body"), ROUTES, ids=[f"{r[0]} {r[1]} {r[2]}" for r in ROUTES]
)
@pytest.mark.parametrize("bad", ["a\x00b", "x" * 201], ids=["NUL", "201 characters"])
async def test_a_path_or_query_id_the_store_cannot_hold_is_422_before_any_query(
    api: tuple[httpx.AsyncClient, dict[str, str]],
    method: str,
    template: str,
    query: str | None,
    body: dict[str, object] | None,
    bad: str,
) -> None:
    client, admin = api
    if query is None:
        url, params = template.format(id=quote(bad, safe="")), None
    else:
        url, params = template, {query: bad}
    answer = await client.request(method, url, params=params, json=body, headers=admin)
    assert answer.status_code == 422, answer.text
    (error,) = answer.json()["detail"]
    assert error["loc"][0] == ("query" if query else "path")
