"""#113 C27: a runner's content type reaches the caller unchanged, buffered or streamed.

The transport kept only aiohttp's parsed media type, so a runner's parameters (``charset``, ``profile``,
``boundary``) were dropped, and Starlette then appended ``charset=utf-8`` to any ``text/`` type. The
engine now passes the runner's ``Content-Type`` header through as it came.
"""

from collections.abc import AsyncIterator

import httpx
import pytest

from livepeer_builder.adapters.sdk import SdkRunnerTransport
from livepeer_builder.contracts import ActorContext, JobRequest
from livepeer_builder.http import create_app
from livepeer_builder.testing import ScriptedStreamTransport, StreamScript
from tests.conftest import EngineFactory
from tests.unit.livepeer_net import Net, Reply, start
from tests.unit.test_streaming import APP, provisioned

TYPES = [
    "text/plain",
    "text/plain; charset=iso-8859-1",
    "application/x-ndjson; profile=chat",
    "text/event-stream",
]


@pytest.fixture
async def net() -> AsyncIterator[Net]:
    async for value in start():
        value.paid = False
        yield value


@pytest.mark.parametrize("content_type", TYPES)
async def test_the_transport_keeps_the_runner_s_content_type_as_sent(net: Net, content_type: str) -> None:
    transport, runner = SdkRunnerTransport(None), net.runner()
    net.replies = [Reply(content_type=content_type), Reply(content_type=content_type, chunks=(b"a",))]
    reply = await transport.call(runner, JobRequest(app=APP), None)
    stream = await transport.open(runner, JobRequest(app=APP), None)
    try:
        assert (reply.content_type, stream.content_type) == (content_type, content_type)
    finally:
        await stream.aclose()


@pytest.fixture
async def rest(
    make_engine: EngineFactory, admin: ActorContext
) -> AsyncIterator[tuple[httpx.AsyncClient, dict[str, str]]]:
    engine = await provisioned(make_engine, admin)
    issued = await engine.keys.issue(
        admin, kind="actor", actor_id="alice", operation_ref="issue-content-type"
    )
    app = create_app(engine, manage_lifecycle=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://engine") as client:
        yield client, {"Authorization": f"Bearer {issued.token}"}


@pytest.mark.parametrize("content_type", TYPES)
async def test_rest_answers_with_the_runner_s_content_type_unchanged(
    rest: tuple[httpx.AsyncClient, dict[str, str]], transport: ScriptedStreamTransport, content_type: str
) -> None:
    client, alice = rest
    reply = ScriptedStreamTransport.ok(b"hello").model_copy(update={"content_type": content_type})
    transport.steps = [reply]
    buffered = await client.post("/v1/jobs", json={"app": APP}, headers=alice)
    assert (buffered.status_code, buffered.headers["content-type"]) == (200, content_type)

    transport.opens = [StreamScript(events=[b"data: a\n\n"], content_type=content_type)]
    streamed = await client.post("/v1/jobs?stream=true", json={"app": APP}, headers=alice)
    assert (streamed.status_code, streamed.headers["content-type"]) == (200, content_type)

    transport.opens = [StreamScript(events=[], content_type=content_type)]  # an empty streamed body
    empty = await client.post("/v1/jobs?stream=true", json={"app": APP}, headers=alice)
    assert (empty.status_code, empty.headers["content-type"]) == (200, content_type)
