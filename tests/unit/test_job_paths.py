"""#113 C3: a job's path reaches its own runner, as written.

A runner URL names one app on an orchestrator (``https://orch:8936/apps/app-1/app``). The engine appended
the job's path and aiohttp read the address with yarl, which removes dot segments, ``%2e`` ones included:
``/../../app-2/app`` reached another app on the same orchestrator. The contract now refuses a path that is
not already normal, with a 422 before anything is written. The transport sends the address without
re-normalizing it, and checks that it still begins with the runner's.
"""

import json
from collections.abc import AsyncIterator
from decimal import Decimal

import httpx
import pytest
from pydantic import ValidationError

from livepeer_builder import BuilderEngine
from livepeer_builder.adapters.runner_address import as_built, origin, runner_address
from livepeer_builder.adapters.sdk import SdkRunnerTransport
from livepeer_builder.contracts import ActorContext, JobRequest, Money, ProvisionRequest
from livepeer_builder.errors import RunnerCallError
from livepeer_builder.http import create_app
from livepeer_builder.testing import ScriptedStreamTransport
from tests.conftest import EngineFactory
from tests.unit.livepeer_net import Net, start

APP = "example/echo"

REFUSED = [
    "/..",
    "/../other",
    "/a/../../b",
    "/a/..",
    "/a/.",
    "/./a",
    "/%2e%2e/other",  # percent-encoded, in any case
    "/%2E%2e/other",
    "/.%2e/other",
    "/%2E/a",
    "//",  # empty segments
    "//other/a",
    "/a//b",
    "/a\\b",  # a backslash, in the path or the query
    "\\a",
    "/a?q=\\",
    "/a\x00b",  # control characters, C0, DEL and C1
    "/a\r\nX-Injected: 1",
    "/a\x7f",
    "/a\x85",
    "/a?q=\n",
    "/a\ud800b",  # a lone surrogate, which no request target can encode
    "/a?q=\udfff",
    "/a#fragment",  # a fragment
    "/a?q=1#fragment",
    "/a#",
]
ALLOWED = [
    "",
    "/",
    "/v1/chat/completions",
    "/a/",  # a trailing slash is the runner's to read, not an empty segment in between
    "/a.b",
    "/..a",
    "/a..",
    "/...",
    "/.well-known/x",
    "/a%2Fb",  # an escaped slash is data, not a segment
    "/a%252e%252e",  # an escaped "%2e" is data too
    "/a b/é",
    "/run?x=1&y=2",
    "/run?next=/../other",  # a query string stays allowed: its slashes and dots are data
    "/run?u=http://h//x",
    "/run?",  # kept by the contract; the "?" of an empty query is not sent (see below)
]


@pytest.mark.parametrize("path", REFUSED)
def test_a_path_that_is_not_already_normal_is_refused(path: str) -> None:
    with pytest.raises(ValidationError, match="path"):
        JobRequest(app=APP, path=path)


@pytest.mark.parametrize("path", ALLOWED)
def test_a_normal_path_is_kept_as_written(path: str) -> None:
    assert JobRequest(app=APP, path=path).path == path


@pytest.fixture
async def api(
    make_engine: EngineFactory, admin: ActorContext
) -> AsyncIterator[tuple[httpx.AsyncClient, BuilderEngine, str]]:
    engine = await make_engine()
    alice = await engine.keys.issue(admin, kind="actor", actor_id="alice", operation_ref="issue-paths-1")
    amount = Money(amount=Decimal(5), currency="USD")
    alice_allocation = {"application_id": "default", "actor_id": "alice", "grant_id": "grant_1"}
    await engine.payments.provision(
        admin, ProvisionRequest(**alice_allocation, amount=amount, operation_ref="p1")
    )
    app = create_app(engine, manage_lifecycle=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://engine") as client:
        yield client, engine, alice.token


@pytest.mark.parametrize("path", ["/../../app-2/app", "/%2e%2E/app-2/app", "//x", "/a\\b", "/a#f"])
async def test_rest_refuses_such_a_path_with_422_before_anything_is_written(
    api: tuple[httpx.AsyncClient, BuilderEngine, str], transport: ScriptedStreamTransport, path: str
) -> None:
    client, engine, token = api
    transport.steps = [ScriptedStreamTransport.ok()]  # what a runner would answer, were it reached
    body = {"app": APP, "path": path, "operation_ref": "order-1"}
    answer = await client.post("/v1/jobs", json=body, headers={"Authorization": f"Bearer {token}"})
    assert answer.status_code == 422, answer.text
    assert answer.json()["detail"][0]["loc"] == ["body", "path"]
    alice = ActorContext(actor_id="alice", permissions=frozenset({"jobs.read"}))
    assert await engine.jobs.list_jobs(alice) == []  # no job, so the reference is still free
    assert transport.calls == []


RELATIVE = ["run", "a/b", "a//b", "../run", "./x", "..", "%2e%2e/run", "a/../b", "run?x=1"]


@pytest.mark.parametrize("path", RELATIVE)
def test_a_path_with_no_leading_slash_is_left_to_the_job_service(path: str) -> None:
    assert JobRequest(app=APP, path=path).path == path  # one mistake, one answer: the 400 below


@pytest.mark.parametrize("path", RELATIVE)
async def test_rest_refuses_a_path_with_no_leading_slash_with_400_before_anything_is_written(
    api: tuple[httpx.AsyncClient, BuilderEngine, str], transport: ScriptedStreamTransport, path: str
) -> None:
    client, engine, token = api
    transport.steps = [ScriptedStreamTransport.ok()]  # what a runner would answer, were it reached
    body = {"app": APP, "path": path, "operation_ref": "order-1"}
    answer = await client.post("/v1/jobs", json=body, headers={"Authorization": f"Bearer {token}"})
    assert answer.status_code == 400, answer.text
    assert "path must start with '/'" in answer.text
    alice = ActorContext(actor_id="alice", permissions=frozenset({"jobs.read"}))
    assert await engine.jobs.list_jobs(alice) == []  # no job, so the reference is still free
    assert transport.calls == []


@pytest.mark.parametrize("path", ["a\\b", "a#f", "a\x00b"])
async def test_rest_still_answers_422_for_what_no_path_may_hold(
    api: tuple[httpx.AsyncClient, BuilderEngine, str], transport: ScriptedStreamTransport, path: str
) -> None:
    client, _, token = api
    body = {"app": APP, "path": path, "operation_ref": "order-1"}
    answer = await client.post("/v1/jobs", json=body, headers={"Authorization": f"Bearer {token}"})
    assert answer.status_code == 422, answer.text
    assert transport.calls == []


@pytest.mark.parametrize("path", ["/a\ud800b", "/run?q=\udfff", "a\ud800b"])
async def test_rest_refuses_a_lone_surrogate_with_422_before_anything_is_written(
    api: tuple[httpx.AsyncClient, BuilderEngine, str], transport: ScriptedStreamTransport, path: str
) -> None:
    """JSON can write one (``"\\ud800"``), and the runner address cannot be encoded with it: a 500, and a
    job left ``uncertain`` (review of #134)."""
    client, engine, token = api
    transport.steps = [ScriptedStreamTransport.ok()]  # what a runner would answer, were it reached
    body = json.dumps({"app": APP, "path": path, "operation_ref": "order-1"}).encode()  # ASCII: "\ud800"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    answer = await client.post("/v1/jobs", content=body, headers=headers)
    assert answer.status_code == 422, answer.text
    refusal = answer.json()["detail"][0]
    assert (refusal["loc"], refusal["input"]) == (["body", "path"], path)
    assert answer.content.isascii()  # the 422 repeats the surrogate as the escape it came in as
    alice = ActorContext(actor_id="alice", permissions=frozenset({"jobs.read"}))
    assert await engine.jobs.list_jobs(alice) == []  # no job, so the reference is still free
    assert transport.calls == []


async def test_a_422_still_repeats_any_other_input_as_utf_8(
    api: tuple[httpx.AsyncClient, BuilderEngine, str],
) -> None:
    client, _, token = api
    body = {"app": APP, "path": "/é#", "operation_ref": "order-1"}
    answer = await client.post("/v1/jobs", json=body, headers={"Authorization": f"Bearer {token}"})
    assert answer.status_code == 422, answer.text
    assert answer.headers["content-type"] == "application/json"
    assert '"input":"/é#"'.encode() in answer.content  # as FastAPI writes it: not escaped


# -- the transport ---------------------------------------------------------------------------------------


@pytest.fixture
async def net() -> AsyncIterator[Net]:
    async for value in start():
        net = value
        net.paid = False
        yield net


def past_the_contract(path: str) -> JobRequest:
    """A request whose path never met the contract, as a caller's ``model_copy(update=...)`` can make."""
    return JobRequest(app=APP).model_copy(update={"path": path})


async def call(net: Net, request: JobRequest, runner_name: str = "app-1") -> None:
    await SdkRunnerTransport(None).call(net.runner(name=runner_name), request, None)


@pytest.mark.parametrize(
    ("path", "sent"),
    [
        ("/run", "/app/app-1/run"),
        ("/../app-2/run", "/app/app-1/../app-2/run"),  # yarl sent /app/app-2/run: another app
        ("/%2e%2E/app-2/run", "/app/app-1/%2e%2E/app-2/run"),
        ("/./run", "/app/app-1/./run"),
        ("/run?x=%41", "/app/app-1/run?x=%41"),  # nothing is decoded either
    ],
)
async def test_the_transport_sends_the_address_without_re_normalizing_it(
    net: Net, path: str, sent: str
) -> None:
    await call(net, past_the_contract(path))
    assert net.targets == [sent]


async def test_what_a_request_target_cannot_hold_as_written_is_still_percent_encoded(net: Net) -> None:
    await call(net, JobRequest(app=APP, path="/a b/é?q=x"))
    assert net.targets == ["/app/app-1/a%20b/%C3%A9?q=x"]


@pytest.mark.parametrize("path", ["x", "-2/run", "@127.0.0.1/run"])
async def test_an_address_that_no_longer_begins_with_the_runner_s_is_refused_before_any_request(
    net: Net, path: str
) -> None:
    with pytest.raises(RunnerCallError, match="runner") as refused:
        await call(net, past_the_contract(path))
    error = refused.value
    assert (error.kind, error.payment_sent, error.status_code) == ("http", False, None)
    assert net.log == [] and net.targets == []


async def test_an_address_with_another_host_is_refused_before_any_request(net: Net) -> None:
    runner = net.runner().model_copy(update={"url": net.url})  # an orchestrator's root, with no path
    port = net.url.rsplit(":", 1)[1]
    with pytest.raises(RunnerCallError, match="runner"):
        await SdkRunnerTransport(None).call(runner, past_the_contract(f"@localhost:{port}/app"), None)
    assert net.log == [] and net.targets == []


@pytest.mark.parametrize(
    ("name", "sent"),
    [
        ("my app", "/app/my%20app/run"),  # sent as written, aiohttp put a literal space on the request line
        ("café", "/app/caf%C3%A9/run"),  # ... and a raw non-ASCII byte
        ("100%", "/app/100%25/run"),  # a "%" that begins no escape
        ("a%20b", "/app/a%20b/run"),  # an escape the runner URL already holds is kept
        ("a%2Fb", "/app/a%2Fb/run"),  # ... and never decoded
    ],
)
async def test_the_runner_s_own_path_is_percent_encoded_too(net: Net, name: str, sent: str) -> None:
    await call(net, JobRequest(app=APP, path="/run"), runner_name=name)
    assert net.targets == [sent]


@pytest.mark.parametrize("path", ["x", "/../app-2/run"])
async def test_the_prefix_check_reads_the_encoded_runner_path(net: Net, path: str) -> None:
    """``/app/my app`` and ``/app/my%20app`` are one runner path once both are encoded."""
    if path.startswith("/"):
        await call(net, past_the_contract(path), runner_name="my app")
        assert net.targets == ["/app/my%20app/../app-2/run"]  # as built, the orchestrator's to refuse
    else:
        with pytest.raises(RunnerCallError, match="runner"):
            await call(net, past_the_contract(path), runner_name="my app")
        assert net.targets == []


@pytest.mark.parametrize(
    "url",
    [
        "http://[::1/app",  # yarl: Invalid IPv6 URL
        "http://[::1]x/app",
        "http://host:port/app",
        "http://host:99999/app",
    ],
)
async def test_a_runner_url_that_cannot_be_read_is_an_unpaid_runner_call_error(net: Net, url: str) -> None:
    """A supplied discovery source can return one: only the default source checks its runner URLs."""
    runner = net.runner().model_copy(update={"url": url})
    with pytest.raises(RunnerCallError, match="invalid runner URL") as refused:
        await SdkRunnerTransport(None).call(runner, JobRequest(app=APP, path="/run"), None)
    error = refused.value
    assert (error.kind, error.payment_sent, error.status_code) == ("http", False, None)
    assert net.log == [] and net.targets == []


def test_a_runner_host_that_is_not_ascii_is_sent_by_its_idna_name() -> None:
    """As before #113: aiohttp read the address with yarl, which encodes the host. Sent as built, the
    host must already be encoded, or aiohttp reading it raises a bare ``UnicodeError`` mid-request."""
    address = runner_address("http://café.example:8936/apps/app-1/app", "/run")
    assert address == "http://xn--caf-dma.example:8936/apps/app-1/app/run"
    assert as_built(address).host == "café.example"  # readable, as aiohttp reads it


def test_an_ascii_runner_authority_is_left_as_written() -> None:
    assert runner_address("http://HOST:80/app", "/run") == "http://HOST:80/app/run"
    assert runner_address("http://[::1]:8936/app/", "/run") == "http://[::1]:8936/app/run"


@pytest.mark.parametrize(
    "url", ["http:///app", "http:/\n/host/app", "http://ho\nst/app", "http://ho st/app", "host/app"]
)
def test_a_runner_url_with_no_host_a_request_can_name_is_refused(url: str) -> None:
    with pytest.raises(RunnerCallError, match="invalid runner URL") as refused:
        runner_address(url, "/run")
    assert (refused.value.kind, refused.value.payment_sent) == ("http", False)


@pytest.mark.parametrize("path", ["@", "[", "@[::1"])
def test_a_path_that_makes_the_authority_unreadable_leaves_the_runner(path: str) -> None:
    """yarl raises its own errors for these (an ``IndexError`` for an authority that ends in ``@``)."""
    with pytest.raises(RunnerCallError, match="leaves the runner") as refused:
        runner_address("http://127.0.0.1:8936", path)
    assert (refused.value.kind, refused.value.payment_sent) == ("http", False)


@pytest.mark.parametrize("path", ["/a\ud800b", "/run?q=\udfff", "a\ud800b"])
def test_a_path_that_cannot_be_encoded_leaves_the_runner(path: str) -> None:
    """A lone surrogate that got past the contract: ``quote`` raises a ``UnicodeEncodeError`` for it."""
    with pytest.raises(RunnerCallError, match="leaves the runner") as refused:
        runner_address("http://127.0.0.1:8936/app/app-1", path)
    assert (refused.value.kind, refused.value.payment_sent) == ("http", False)


async def test_the_transport_refuses_a_path_that_cannot_be_encoded_before_any_request(net: Net) -> None:
    with pytest.raises(RunnerCallError, match="leaves the runner") as refused:
        await call(net, past_the_contract("/a\ud800b"))
    error = refused.value
    assert (error.kind, error.payment_sent, error.status_code) == ("http", False, None)
    assert net.log == [] and net.targets == []


@pytest.mark.parametrize("runner_url", ["http://127.0.0.1:8936", "http://127.0.0.1:8936/app/app-1/"])
@pytest.mark.parametrize("path", RELATIVE)
def test_the_transport_refuses_a_path_with_no_leading_slash_too(runner_url: str, path: str) -> None:
    """The contract does not read such a path's segments, so the transport is the second refusal."""
    with pytest.raises(RunnerCallError, match="leaves the runner") as refused:
        runner_address(runner_url, path)
    assert (refused.value.kind, refused.value.payment_sent) == ("http", False)


@pytest.mark.parametrize("host", ["café.example", "CAFÉ.example", "straße.example"])
def test_the_payment_origin_check_reads_a_host_that_is_not_ascii_in_one_form(host: str) -> None:
    """The runner is dialed by its IDNA name, and a 402 may name the same host as the runner URL wrote it.
    Compared as written, the two differed, and a valid challenge was refused as an invalid one (review of
    #134). ``origin`` names every host the way yarl dials it."""
    dialed = origin(runner_address(f"https://{host}/app", "/run"))
    assert dialed is not None and dialed.isascii() and "xn--" in dialed
    assert origin(f"https://{host}/payment") == dialed
    assert origin(f"https://{host}:443/") == dialed


def test_an_ascii_host_s_origin_is_unchanged() -> None:
    assert origin("https://Orch.Example:8936/app") == "https://orch.example:8936"
    assert origin("http://[::1]:8936/app") == "http://::1:8936"
    assert origin("http://xn--caf-dma.example/app") == "http://xn--caf-dma.example:80"


@pytest.mark.parametrize(
    ("path", "sent"),
    [
        ("/run?", "/app/app-1/run"),  # the one thing not sent: the HTTP client omits an empty query's "?"
        ("/run?x=", "/app/app-1/run?x="),
        ("/run?&", "/app/app-1/run?&"),
    ],
)
async def test_an_empty_query_s_delimiter_is_not_sent(net: Net, path: str, sent: str) -> None:
    await call(net, JobRequest(app=APP, path=path))
    assert net.targets == [sent]
