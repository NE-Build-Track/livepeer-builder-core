"""#158: a 422 that repeats a JSON ``NaN`` or an infinity is still a 422.

Python's JSON reader takes ``NaN``, ``Infinity`` and ``-Infinity`` as numbers, and JSON has no number for
them. The 422 repeats the input it refused, could not write one, and became a 500. It now writes each as a
string of its token, so the body is JSON that every client reads and says what was read.
"""

import json
from collections.abc import AsyncIterator

import httpx
import pytest

from livepeer_builder import BuilderEngine
from livepeer_builder.contracts import ActorContext
from livepeer_builder.http import create_app
from tests.conftest import EngineFactory

Api = tuple[httpx.AsyncClient, dict[str, str], BuilderEngine]
# (the body sent, the field refused, that field's input as the 422 repeats it)
BODIES = [
    (b'{"app":"example/echo","timeout_s":NaN}', "timeout_s", "NaN"),
    (b'{"app":"example/echo","timeout_s":Infinity}', "timeout_s", "Infinity"),
    (b'{"app":"example/echo","timeout_s":-Infinity}', "timeout_s", "-Infinity"),
    # no app: the whole body is the input repeated, and the numbers JSON can write stay numbers
    (b'{"payload":{"x":[NaN,-Infinity,1.5,2]}}', "app", {"payload": {"x": ["NaN", "-Infinity", 1.5, 2]}}),
]


def strict(token: str) -> None:
    raise ValueError(f"{token} is not JSON")


@pytest.fixture
async def api(make_engine: EngineFactory, admin: ActorContext) -> AsyncIterator[Api]:
    engine = await make_engine()
    key = await engine.keys.issue(admin, kind="actor", actor_id="alice", operation_ref="issue-alice")
    headers = {"Authorization": f"Bearer {key.token}", "Content-Type": "application/json"}
    app = create_app(engine, manage_lifecycle=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://engine") as client:
        yield client, headers, engine


@pytest.mark.parametrize(
    ("body", "field", "repeated"), BODIES, ids=["NaN", "Infinity", "-Infinity", "nested"]
)
async def test_a_422_repeats_a_number_json_cannot_write_as_a_string_of_its_token(
    api: Api, admin: ActorContext, body: bytes, field: str, repeated: object
) -> None:
    client, headers, engine = api
    answer = await client.post("/v1/jobs", content=body, headers=headers)
    assert answer.status_code == 422, answer.text
    assert answer.headers["content-type"] == "application/json"
    detail = json.loads(answer.content, parse_constant=strict)["detail"]  # JSON a strict reader takes
    assert (detail[0]["loc"], detail[0]["input"]) == (["body", field], repeated)
    assert await engine.jobs.list_jobs(admin) == []


async def test_a_422_with_only_numbers_json_can_write_is_as_fastapi_writes_it(api: Api) -> None:
    client, headers, _ = api
    answer = await client.post(
        "/v1/jobs", content=b'{"app":"example/echo","timeout_s":-1.5}', headers=headers
    )
    assert answer.status_code == 422, answer.text
    assert b'"input":-1.5,' in answer.content and b'"ctx":{"gt":0.0}' in answer.content
