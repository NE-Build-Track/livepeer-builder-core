"""#113 C27: a runner's content type reaches the caller unchanged, buffered or streamed.

The transport kept only aiohttp's parsed media type, so a runner's parameters (``charset``, ``profile``,
``boundary``) were dropped, and Starlette then appended ``charset=utf-8`` to any ``text/`` type. The
engine now passes the runner's ``Content-Type`` header through as it came.

Byte for byte: aiohttp reads header bytes as UTF-8 and Starlette writes them as Latin-1, so a value that
is not ASCII was a 500 after the job ran, and a stored one broke that job's reads (review of #141). The
transport keeps the header's bytes read as Latin-1, which goes back out as the same bytes.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from decimal import Decimal
from uuid import UUID, uuid4

import httpx
import pytest

from livepeer_builder import EngineSupply
from livepeer_builder.adapters.sdk import SdkRunnerTransport
from livepeer_builder.contracts import ActorContext, AllotRequest, JobRequest, JobResult, Money, Runner
from livepeer_builder.http import create_app
from livepeer_builder.testing import ScriptedStreamTransport, StreamScript, runner
from tests.conftest import EngineFactory
from tests.unit.livepeer_net import Net, Reply, start
from tests.unit.test_streaming import APP, allotted

TYPES = [
    "text/plain",
    "text/plain; charset=iso-8859-1",
    "application/x-ndjson; profile=chat",
    "text/event-stream",
    'text/plain; title="é"',  # not ASCII: the test runner sends it as UTF-8, two bytes
    'text/plain; title="日本"',
]
OCTETS = b"application/octet-stream"
# (the Content-Type bytes a runner sends, the bytes the caller gets): what UTF-8 cannot read is kept, and
# what no response header may hold is not.
RAW = [
    (b"text/plain; title=\xe9", b"text/plain; title=\xe9"),  # one Latin-1 byte
    (b"text/plain; t=\xff\xfe", b"text/plain; t=\xff\xfe"),
    (b"text/plain;\tt=1", b"text/plain;\tt=1"),  # a tab is allowed in a header value
    (b"text/plain; t=" + b"x" * 8000, b"text/plain; t=" + b"x" * 8000),  # as long as aiohttp reads one
    (b"text/plain; t=\x01", OCTETS),  # control characters, which aiohttp lets through
    (b"text/plain; t=\x7f", OCTETS),
    (b"a\x0bb", OCTETS),
]
RAW_IDS = ["latin-1 byte", "ff fe", "tab", "8000 bytes", "control 01", "DEL", "vertical tab"]


def kept(sent: bytes) -> str:
    """How the engine keeps a header value: its bytes read as Latin-1, as Starlette writes them back."""
    return sent.decode("latin-1")


def content_type_bytes(answer: httpx.Response) -> bytes:
    """The answer's Content-Type as the bytes on the wire: ``answer.headers`` would guess an encoding."""
    (value,) = [value for name, value in answer.headers.raw if name.lower() == b"content-type"]
    return value


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
        as_sent = kept(content_type.encode())  # ASCII reads the same either way
        assert (reply.content_type, stream.content_type) == (as_sent, as_sent)
    finally:
        await stream.aclose()


@pytest.fixture
async def rest(
    make_engine: EngineFactory, admin: ActorContext
) -> AsyncIterator[tuple[httpx.AsyncClient, dict[str, str]]]:
    engine = await allotted(make_engine, admin)
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
    sent, reported = content_type.encode(), kept(content_type.encode())  # as the SDK transport reports it
    reply = ScriptedStreamTransport.ok(b"hello").model_copy(update={"content_type": reported})
    transport.steps = [reply]
    buffered = await client.post("/v1/jobs", json={"app": APP}, headers=alice)
    assert (buffered.status_code, content_type_bytes(buffered)) == (200, sent)

    transport.opens = [StreamScript(events=[b"data: a\n\n"], content_type=reported)]
    streamed = await client.post("/v1/jobs?stream=true", json={"app": APP}, headers=alice)
    assert (streamed.status_code, content_type_bytes(streamed)) == (200, sent)

    transport.opens = [StreamScript(events=[], content_type=reported)]  # an empty streamed body
    empty = await client.post("/v1/jobs?stream=true", json={"app": APP}, headers=alice)
    assert (empty.status_code, content_type_bytes(empty)) == (200, sent)


@pytest.mark.parametrize("content_type", TYPES)
async def test_a_stored_result_answers_with_its_content_type_unchanged(
    make_engine: EngineFactory, admin: ActorContext, monkeypatch: pytest.MonkeyPatch, content_type: str
) -> None:
    """``GET /v1/jobs/{id}/result`` answers 501 until results are stored (ISS-26), so the service method is
    replaced here by one that returns a result. The route built its answer with ``media_type=``."""
    engine = await make_engine()
    key = await engine.keys.bootstrap_admin()
    stored = kept(content_type.encode())

    async def result(actor: ActorContext, job_id: UUID) -> JobResult:
        return JobResult.model_construct(status_code=200, content=b"hello", content_type=stored)

    monkeypatch.setattr(engine.jobs, "result", result)
    app = create_app(engine, manage_lifecycle=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://engine") as client:
        answer = await client.get(
            f"/v1/jobs/{uuid4()}/result", headers={"Authorization": f"Bearer {key.token}"}
        )
    assert (answer.status_code, content_type_bytes(answer)) == (200, content_type.encode())
    assert answer.content == b"hello"


# -- through the real transport --------------------------------------------------------------------------


async def serving(
    make_engine: EngineFactory, admin: ActorContext, runner: Runner
) -> AsyncIterator[tuple[httpx.AsyncClient, dict[str, str]]]:
    """REST over an engine whose SDK transport calls ``runner``, with alice allotted and holding a key."""
    engine = await make_engine(runners=(runner,), supply=EngineSupply(transport=SdkRunnerTransport(None)))
    amount = Money(amount=Decimal(5), currency="USD")
    request = AllotRequest(operation_ref="p1", tenant_id="default", actor_id="alice", amount=amount)
    await engine.payments.allot(admin, request)
    issued = await engine.keys.issue(admin, kind="actor", actor_id="alice", operation_ref="issue-live")
    app = create_app(engine, manage_lifecycle=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://engine") as client:
        yield client, {"Authorization": f"Bearer {issued.token}"}


async def run_both_ways_then_read(
    client: httpx.AsyncClient, alice: dict[str, str], app: str, sent: bytes
) -> None:
    """A buffered and a streamed job answer with ``sent`` as their Content-Type, and both jobs read back."""
    buffered = await client.post("/v1/jobs", json={"app": app}, headers=alice)
    assert (buffered.status_code, content_type_bytes(buffered)) == (200, sent), buffered.text
    streamed = await client.post("/v1/jobs?stream=true", json={"app": app}, headers=alice)
    assert (streamed.status_code, content_type_bytes(streamed)) == (200, sent), streamed.text
    listed = await client.get("/v1/jobs", headers=alice)
    assert listed.status_code == 200, listed.text  # a stored surrogate made every read of the job a 500
    assert [job["result"]["content_type"] for job in listed.json()] == [kept(sent)] * 2
    for job in listed.json():
        one = await client.get(f"/v1/jobs/{job['id']}", headers=alice)
        assert (one.status_code, one.json()["result"]["content_type"]) == (200, kept(sent))


@pytest.mark.parametrize("content_type", TYPES)
async def test_rest_answers_with_the_content_type_bytes_the_runner_sent_and_the_jobs_still_read(
    make_engine: EngineFactory, admin: ActorContext, net: Net, content_type: str
) -> None:
    net.replies = [Reply(content_type=content_type), Reply(content_type=content_type, chunks=(b"a",))]
    async for client, alice in serving(make_engine, admin, net.runner()):
        await run_both_ways_then_read(client, alice, net.runner().app, content_type.encode())


async def answering(head: bytes) -> AsyncIterator[Runner]:
    """A loopback runner that answers every request with exactly the header lines in ``head``: aiohttp's
    server writes header text as UTF-8, so it cannot send a byte that is not."""
    head = b"HTTP/1.1 200 OK\r\n" + head + b"\r\nContent-Length: 5\r\n"

    async def answer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        asked = await reader.readuntil(b"\r\n\r\n")
        (length,) = [
            int(line[15:]) for line in asked.lower().split(b"\r\n") if line[:15] == b"content-length:"
        ]
        await reader.readexactly(length)
        writer.write(head + b"Connection: close\r\n\r\nhello")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(answer, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield runner(APP, url=f"http://127.0.0.1:{port}/app", orchestrator_url=None)
    finally:
        server.close()
        await server.wait_closed()


@pytest.fixture
async def raw_runner(request: pytest.FixtureRequest) -> AsyncIterator[Runner]:
    """A loopback runner whose Content-Type is exactly ``request.param``."""
    async for value in answering(b"Content-Type: " + request.param):
        yield value


@pytest.mark.parametrize(("raw_runner", "sent"), RAW, ids=RAW_IDS, indirect=["raw_runner"])
async def test_the_transport_keeps_content_type_bytes_as_latin_1_or_not_at_all(
    raw_runner: Runner, sent: bytes
) -> None:
    transport = SdkRunnerTransport(None)
    reply = await transport.call(raw_runner, JobRequest(app=APP), None)
    stream = await transport.open(raw_runner, JobRequest(app=APP), None)
    try:
        assert (reply.content_type, stream.content_type) == (kept(sent), kept(sent))
    finally:
        await stream.aclose()


async def test_a_runner_that_sends_no_content_type_is_octet_stream() -> None:
    transport = SdkRunnerTransport(None)
    async for bare in answering(b"X-Other: 1"):
        reply = await transport.call(bare, JobRequest(app=APP), None)
        stream = await transport.open(bare, JobRequest(app=APP), None)
        try:
            assert (reply.content_type, stream.content_type) == (OCTETS.decode(), OCTETS.decode())
        finally:
            await stream.aclose()


@pytest.mark.parametrize(("raw_runner", "sent"), RAW, ids=RAW_IDS, indirect=["raw_runner"])
async def test_rest_answers_a_content_type_that_is_not_utf_8_byte_for_byte_and_the_jobs_still_read(
    make_engine: EngineFactory, admin: ActorContext, raw_runner: Runner, sent: bytes
) -> None:
    async for client, alice in serving(make_engine, admin, raw_runner):
        await run_both_ways_then_read(client, alice, APP, sent)


# -- every other header of a stream (review of #141) ------------------------------------------------------

# (a header line a runner sends, the pair an embedded caller reads): the bytes read as Latin-1, as the
# content type is. aiohttp reads them as UTF-8 and writes a byte that is not as a lone surrogate.
HEADERS = [
    (b"X-Title: caf\xe9", ("X-Title", "caf\xe9")),  # one Latin-1 byte, no UTF-8
    (b"X-Trace: \xff\xfe", ("X-Trace", "\xff\xfe")),
    (b"X-Title: caf\xc3\xa9", ("X-Title", "caf\xc3\xa9")),  # written as UTF-8: its two bytes, not one e-acute
    (b"X-Trace: plain", ("X-Trace", "plain")),
]
HEADER_IDS = ["latin-1 byte", "ff fe", "utf-8", "ascii"]


@pytest.fixture
async def header_runner(request: pytest.FixtureRequest) -> AsyncIterator[Runner]:
    """A loopback runner that answers JSON with exactly ``request.param`` as one more header line."""
    async for value in answering(b"Content-Type: application/json\r\n" + request.param):
        yield value


@pytest.mark.parametrize(
    ("header_runner", "line", "pair"),
    [(line, line, pair) for line, pair in HEADERS],
    ids=HEADER_IDS,
    indirect=["header_runner"],
)
async def test_a_stream_s_headers_are_the_runner_s_bytes_read_as_latin_1(
    header_runner: Runner, line: bytes, pair: tuple[str, str]
) -> None:
    """``JobStreamApi.headers`` is for an embedded caller to log, store or send on. A lone surrogate in
    one value made each of those a ``UnicodeEncodeError``, as it did for the content type."""
    stream = await SdkRunnerTransport(None).open(header_runner, JobRequest(app=APP), None)
    try:
        headers = stream.headers
    finally:
        await stream.aclose()
    assert pair in headers and ("Content-Type", "application/json") in headers
    assert json.dumps(headers, ensure_ascii=False).encode()  # it can be logged and stored
    sent_on = [name.encode("latin-1") + b": " + value.encode("latin-1") for name, value in headers]
    assert line in sent_on  # and sent on, as the bytes that came


# -- a supplied transport's content type (#158) -----------------------------------------------------------

# What a supplied transport reported and no response header can hold. Starlette writes a header as
# Latin-1, so the first two were a 500 after the job ran, and the streamed job was left ``running``; a
# stored lone surrogate made every later read of the job a 500. The rest went out as written.
NOT_A_HEADER = [
    'text/plain; title="日本"',  # the text itself, which Latin-1 cannot encode
    "text/plain; t=\udce9",  # a lone surrogate, as aiohttp reads a byte that is not UTF-8
    "text/plain\r\nX-Injected: 1",
    "text/plain; t=\x7f",
    "",
]
NOT_A_HEADER_IDS = ["above latin-1", "lone surrogate", "line break", "DEL", "empty"]


@pytest.mark.parametrize("reported", NOT_A_HEADER, ids=NOT_A_HEADER_IDS)
async def test_rest_answers_octet_stream_for_a_supplied_transport_s_content_type_no_header_holds(
    rest: tuple[httpx.AsyncClient, dict[str, str]], transport: ScriptedStreamTransport, reported: str
) -> None:
    client, alice = rest
    transport.steps = [ScriptedStreamTransport.ok(b"hello").model_copy(update={"content_type": reported})]
    buffered = await client.post("/v1/jobs", json={"app": APP}, headers=alice)
    assert (buffered.status_code, content_type_bytes(buffered)) == (200, OCTETS), buffered.text
    assert buffered.content == b"hello" and buffered.headers.get("x-injected") is None

    transport.opens = [StreamScript(events=[b"data: a\n\n"], content_type=reported)]
    streamed = await client.post("/v1/jobs?stream=true", json={"app": APP}, headers=alice)
    assert (streamed.status_code, content_type_bytes(streamed)) == (200, OCTETS), streamed.text
    assert streamed.content == b"data: a\n\n" and streamed.headers.get("x-injected") is None

    transport.opens = [StreamScript(events=[], content_type=reported)]  # an empty streamed body
    empty = await client.post("/v1/jobs?stream=true", json={"app": APP}, headers=alice)
    assert (empty.status_code, content_type_bytes(empty)) == (200, OCTETS), empty.text

    listed = await client.get("/v1/jobs", headers=alice)
    assert listed.status_code == 200, listed.text  # the jobs read back, and none is left running
    assert [(job["state"], job["result"]["content_type"]) for job in listed.json()] == [
        ("succeeded", OCTETS.decode())
    ] * 3


@pytest.mark.parametrize("reported", NOT_A_HEADER, ids=NOT_A_HEADER_IDS)
async def test_an_embedded_caller_reads_the_same_content_type_the_job_record_keeps(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, transport: ScriptedStreamTransport,
    reported: str,
) -> None:  # fmt: skip
    engine = await allotted(make_engine, admin)
    transport.steps = [ScriptedStreamTransport.ok(b"hello").model_copy(update={"content_type": reported})]
    result = await engine.jobs.run(alice, JobRequest(app=APP))
    assert result.job.result is not None
    assert (result.content_type, result.job.result.content_type) == (OCTETS.decode(), OCTETS.decode())

    transport.opens = [StreamScript(events=[b"data: a\n\n"], content_type=reported)]
    async with await engine.jobs.streaming.stream(alice, JobRequest(app=APP)) as stream:
        assert stream.content_type == OCTETS.decode()
        assert [chunk async for chunk in stream] == [b"data: a\n\n"]
    assert stream.job.result is not None and stream.job.result.content_type == OCTETS.decode()


@pytest.mark.parametrize(
    "reported", ["text/plain; t=\xe9", "text/plain; t=\x80\xff", "text/plain;\tt=1", "a"], ids=ascii
)
async def test_a_supplied_transport_s_content_type_a_header_holds_is_kept_as_reported(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, transport: ScriptedStreamTransport,
    reported: str,
) -> None:  # fmt: skip
    engine = await allotted(make_engine, admin)
    transport.steps = [ScriptedStreamTransport.ok(b"hello").model_copy(update={"content_type": reported})]
    result = await engine.jobs.run(alice, JobRequest(app=APP))
    assert result.job.result is not None
    assert (result.content_type, result.job.result.content_type) == (reported, reported)
    transport.opens = [StreamScript(events=[b"data: a\n\n"], content_type=reported)]
    async with await engine.jobs.streaming.stream(alice, JobRequest(app=APP)) as stream:
        assert stream.content_type == reported
        assert [chunk async for chunk in stream] == [b"data: a\n\n"]
    assert stream.job.result is not None and stream.job.result.content_type == reported
