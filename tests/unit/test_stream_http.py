"""Streams over REST (D37): headers wait for the first chunk, a failure after it aborts the connection,
and a client that disconnects ends the job and frees its slot under a real anyio cancel scope (D34)."""

import asyncio
import contextlib
import json
from typing import Any
from uuid import UUID

import httpx
from starlette.requests import ClientDisconnect

from livepeer_builder.contracts import ActorContext
from livepeer_builder.errors import RunnerCallError
from livepeer_builder.http import create_app
from livepeer_builder.runtime import BuilderEngine
from livepeer_builder.testing import ScriptedStreamTransport, StreamScript
from tests.conftest import EngineFactory
from tests.unit.test_stream_lifecycle import ended
from tests.unit.test_streaming import APP, provisioned

Message = dict[str, Any]


async def bearer(engine: BuilderEngine, admin: ActorContext) -> str:
    issued = await engine.keys.issue(admin, kind="actor", actor_id="alice", application_id="default")
    return issued.token


async def raw_stream(
    engine: BuilderEngine, token: str, *, disconnect_after: int | None = None, spec: str = "2.3"
) -> list[Message]:
    """POST /v1/jobs?stream=true straight through ASGI, recording every message the app sends.

    With ``disconnect_after`` the client goes away once that many body chunks arrived. Spec 2.3 is what
    uvicorn speaks: Starlette then watches for the disconnect and cancels the response in a cancel scope.
    """
    sent: list[Message] = []
    request_read = False
    gone = asyncio.Event()

    async def receive() -> Message:
        nonlocal request_read
        if not request_read:
            request_read = True
            return {"type": "http.request", "body": json.dumps({"app": APP}).encode(), "more_body": False}
        await gone.wait()
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        if gone.is_set():
            raise OSError("client went away")  # how a 2.4 server reports a closed connection
        sent.append(message)
        chunks = [m for m in sent if m["type"] == "http.response.body" and m.get("body")]
        if disconnect_after is not None and len(chunks) >= disconnect_after:
            gone.set()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": spec},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/jobs",
        "raw_path": b"/v1/jobs",
        "query_string": b"stream=true",
        "root_path": "",
        "headers": [(b"content-type", b"application/json"), (b"authorization", f"Bearer {token}".encode())],
        "client": ("127.0.0.1", 50000),
        "server": ("engine", 80),
    }
    app = create_app(engine, manage_lifecycle=False)
    with contextlib.suppress(ClientDisconnect):  # a 2.4 server learns of a disconnect when a send fails
        await app(scope, receive, send)
    return sent


def job_id_of(sent: list[Message]) -> str:
    start = next(m for m in sent if m["type"] == "http.response.start")
    return dict(start["headers"])[b"livepeer-job-id"].decode()


async def test_a_failure_after_the_first_byte_aborts_the_response(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, transport: ScriptedStreamTransport
) -> None:
    engine = await provisioned(make_engine, admin)
    error = RunnerCallError("http", "500", payment_sent=True, status_code=500)
    transport.opens = [StreamScript(events=[b"half", error])]
    sent = await raw_stream(engine, await bearer(engine, admin))
    bodies = [m for m in sent if m["type"] == "http.response.body"]
    assert [m["body"] for m in bodies] == [b"half"]
    assert all(m["more_body"] for m in bodies)  # no closing message: the transfer is visibly cut off
    job = await engine.jobs.get(alice, UUID(job_id_of(sent)))
    assert job.state == "uncertain" and job.failure is not None and job.failure.kind == "runner_error"


async def test_failures_before_the_first_chunk_are_ordinary_error_responses(
    make_engine: EngineFactory, admin: ActorContext, transport: ScriptedStreamTransport
) -> None:
    engine = await provisioned(make_engine, admin)
    headers = {"Authorization": f"Bearer {await bearer(engine, admin)}"}
    app = create_app(engine, manage_lifecycle=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://engine") as client:
        transport.opens = [StreamScript(events=[5.0, b"late"])]  # headers, then nothing
        body = {"app": APP, "first_byte_timeout_s": 0.05}
        response = await client.post("/v1/jobs?stream=true", json=body, headers=headers)
        assert response.status_code == 504 and response.json()["job"]["failure"]["kind"] == "timeout"
        assert response.headers["livepeer-job-id"] == response.json()["job"]["id"]

        error = RunnerCallError("http", "boom", payment_sent=True, status_code=500, body="trace")
        transport.opens = [StreamScript(events=[error])]
        response = await client.post("/v1/jobs?stream=true", json={"app": APP}, headers=headers)
        assert response.status_code == 502 and response.json()["job"]["failure"]["kind"] == "runner_error"

        transport.opens = [StreamScript(events=[], content_type="application/json")]
        response = await client.post("/v1/jobs?stream=true", json={"app": APP}, headers=headers)
        assert response.status_code == 200 and response.content == b""
        job = (await client.get(f"/v1/jobs/{response.headers['livepeer-job-id']}", headers=headers)).json()
        assert job["state"] == "succeeded" and job["result"]["size_bytes"] == 0


async def test_a_client_disconnect_ends_the_job_and_frees_the_slot(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, transport: ScriptedStreamTransport
) -> None:
    engine = await provisioned(make_engine, admin, max_concurrent_jobs=1, stream_settle_s=0.05)
    token = await bearer(engine, admin)
    for spec in ("2.3", "2.4"):  # uvicorn's cancel-scope path, and a server whose send raises
        transport.opens = [StreamScript(events=[b"a", 0.05, b"b", 5.0, b"c"])]
        sent = await raw_stream(engine, token, disconnect_after=1, spec=spec)
        await ended(engine)
        job = await engine.jobs.get(alice, UUID(job_id_of(sent)))
        assert job.state == "uncertain" and job.attempts[0].outcome == "abandoned", spec
        assert engine.admission.running == 0 and transport.streams[-1].closed, spec
