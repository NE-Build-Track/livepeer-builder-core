"""#113 C26: every id a route reads from its path or query string is stored text.

A NUL in a path or query id reached the store, where PostgreSQL refuses it: a 500. An id longer than any
record holds was looked up anyway, and answered 404. Both are now a 422 before any query runs.
``tests/structure/test_route_params.py`` keeps every route that way. So is a query time that UTC cannot
hold, which overflowed in the store.
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


# A time whose offset puts it outside the years a datetime holds, once in UTC: the store's conversion met
# it as an ``OverflowError``, a 500 (review of #139).
BEFORE_YEAR_1, AFTER_YEAR_9999 = "0001-01-01T00:00:00+14:00", "9999-12-31T23:59:59-14:00"
TIMES = [
    ("/v1/jobs", "before", BEFORE_YEAR_1),
    ("/v1/jobs", "before", AFTER_YEAR_9999),
    ("/v1/costs/summary", "start", BEFORE_YEAR_1),
    ("/v1/costs/summary", "start", AFTER_YEAR_9999),
    ("/v1/costs/summary", "end", BEFORE_YEAR_1),
    ("/v1/costs/summary", "end", AFTER_YEAR_9999),
]


@pytest.mark.parametrize(("path", "query", "bad"), TIMES, ids=[f"{t[0]} {t[1]} {t[2][:4]}" for t in TIMES])
async def test_a_query_time_that_utc_cannot_hold_is_422_before_any_query(
    api: tuple[httpx.AsyncClient, dict[str, str]], path: str, query: str, bad: str
) -> None:
    client, admin = api
    answer = await client.get(path, params={query: bad}, headers=admin)
    assert answer.status_code == 422, answer.text
    (error,) = answer.json()["detail"]
    assert error["loc"] == ["query", query]


async def test_the_first_and_the_last_time_utc_holds_are_still_read(
    api: tuple[httpx.AsyncClient, dict[str, str]],
) -> None:
    client, admin = api
    window = {"start": "0001-01-01T00:00:00Z", "end": "9999-12-31T23:59:59.999999Z"}
    summary = await client.get("/v1/costs/summary", params=window, headers=admin)
    assert summary.status_code == 200, summary.text
    assert (summary.json()["start"], summary.json()["end"]) == (window["start"], window["end"])
    for before in window.values():
        page = await client.get("/v1/jobs", params={"before": before}, headers=admin)
        assert (page.status_code, page.json()) == (200, [])


@pytest.mark.parametrize(
    "written",
    ["2026-01-01T02:00:00+02:00", "2026-01-01T00:00:00", "2026-01-01T00:00:00Z"],
    ids=["+02:00", "no offset", "Z"],
)
async def test_a_summary_s_window_is_answered_in_utc_however_it_was_written(
    api: tuple[httpx.AsyncClient, dict[str, str]], written: str
) -> None:
    """A time with no offset is read as UTC, as before; the answer now says so."""
    client, admin = api
    window = {"start": written, "end": "2026-02-01T00:00:00+00:00"}
    summary = await client.get("/v1/costs/summary", params=window, headers=admin)
    assert summary.status_code == 200, summary.text
    assert (summary.json()["start"], summary.json()["end"]) == (
        "2026-01-01T00:00:00Z",
        "2026-02-01T00:00:00Z",
    )
