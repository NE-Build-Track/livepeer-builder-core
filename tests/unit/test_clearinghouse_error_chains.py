"""#168 review: an error the clearinghouse wire raises reaches no credential, by any way a logger or an
error tracker walks it.

An answer to an allotment or a payment key rotation holds a live Batteries key (``lpg_``), handed over
once, and every request holds the tenant admin key in its headers. An error raised while one of them is
being read must carry neither:

- **its chain.** ``raise ... from None`` inside an ``except`` block hides the first error from a printed
  traceback, but Python still keeps it as ``__context__``: pydantic's error holds the whole answer, and
  httpx's holds the request. Each error is built where the answer or the request is in hand, and raised
  from a frame that has neither;
- **its traceback's frames.** A tracker that records the local variables of each frame reads whatever
  the raising frames still hold. No frame of this package that an error passes through has the answer,
  the request or the key among its locals.

The discovery reader carries the same tenant admin key, to the clearinghouse's runner list, and a 401 or
a timeout there is an ordinary failed refresh. Its errors are held to the same, over every frame of the
traceback, the HTTP client's own included: an error of a read that carried the key is a new error, of the
kind the reader always raised, that was never inside the HTTP client.
"""

import asyncio
import logging
import traceback
from collections.abc import AsyncIterator, Callable
from types import TracebackType
from typing import Any

import httpx
import pytest

from livepeer_builder.adapters.clearinghouse import ClearinghouseAllocations
from livepeer_builder.adapters.clearinghouse_wire import ClearinghouseWire
from livepeer_builder.adapters.sdk import HttpDiscoverySource
from livepeer_builder.contracts import DeliveredAllocation, Money
from livepeer_builder.errors import EngineError, ProviderError, ProviderOutcomeUnknown, ProviderUnavailable
from livepeer_builder.testing import MemorySecretStore
from tests.unit.test_clearinghouse_adapters import ALLOTTED, HANDED, KEY, NoActors, saying_whose_key_it_is

BASE = "https://clearinghouse.test"
TOKEN = "lpk_tenant-admin-key-throwaway"
# a key just handed over, in an answer that is not a DeliveredAllocation: it names no actor or allocation
UNREADABLE = {"allocation_api_key": KEY, "allocation_id": "alloc_1"}
FIVE = Money(amount=5, currency="USD")


def wire(handler: httpx.MockTransport) -> ClearinghouseWire:
    return ClearinghouseWire(BASE, TOKEN, "grant_1", httpx.AsyncClient(transport=handler))


def answering(status: int, body: dict[str, Any]) -> ClearinghouseWire:
    return wire(httpx.MockTransport(lambda request: httpx.Response(status, json=body)))


def held(value: object) -> str:
    """What a local variable gives away: its repr, and for an HTTP answer or request what a tracker that
    serializes it would read, body and headers."""
    seen = [repr(value)]
    if isinstance(value, httpx.Response):
        seen += [value.text, repr(dict(value.request.headers))]
    if isinstance(value, httpx.Request):
        seen += [repr(dict(value.headers)), value.content.decode(errors="replace")]
    if isinstance(value, dict | list | tuple):
        seen += [held(item) for item in (value.values() if isinstance(value, dict) else value)]
    return "\n".join(seen)


def frames(error: BaseException) -> str:
    """The locals of every frame of this package the error passed through."""
    seen: list[str] = []
    step: TracebackType | None = error.__traceback__
    while step is not None:
        if "livepeer_builder" in step.tb_frame.f_code.co_filename:
            seen += [held(value) for value in step.tb_frame.f_locals.values()]
        step = step.tb_next
    return "\n".join(seen)


def reachable(error: BaseException) -> str:
    """Everything a logger or an error tracker can read of ``error`` and what it chains: its text, its
    repr, its traceback, the locals of its frames, and each chained error's attributes, an httpx
    request's headers among them."""
    seen = [str(error), repr(error), repr(vars(error)), "".join(traceback.format_exception(error))]
    seen.append(frames(error))
    for chained in (error.__cause__, error.__context__):
        if chained is not None:
            seen += [repr(chained), repr(vars(chained)), frames(chained)]
            request = getattr(chained, "_request", None)
            seen += [repr(dict(request.headers))] if request is not None else []
    return "\n".join(seen)


def clean(error: BaseException) -> bool:
    found = reachable(error)
    return KEY not in found and "lpg_" not in found and TOKEN not in found


# -- the wire ---------------------------------------------------------------------------------------------


async def test_an_answer_that_does_not_parse_chains_nothing_and_leaves_nothing_in_a_frame() -> None:
    with pytest.raises(ProviderError) as refused:
        await answering(201, UNREADABLE).write_as(
            DeliveredAllocation, "/allocation", {"operation_ref": "a-1"}
        )
    assert refused.value.__cause__ is None and refused.value.__context__ is None
    assert clean(refused.value)
    assert "not DeliveredAllocation" in str(refused.value)


@pytest.mark.parametrize("failure", [httpx.ConnectError, httpx.ReadTimeout], ids=["never sent", "no answer"])
async def test_a_request_that_failed_chains_and_keeps_no_request_that_holds_the_tenant_admin_key(
    failure: type[httpx.TransportError],
) -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise failure("the network failed", request=request)

    with pytest.raises(ProviderUnavailable) as unavailable:
        await wire(httpx.MockTransport(fail)).read_as(DeliveredAllocation, "/allocation")
    assert unavailable.value.__context__ is None
    assert clean(unavailable.value)


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422, 429, 500, 503, 302])
async def test_an_error_answer_that_echoes_a_key_leaves_neither_key_in_the_error_or_its_frames(
    status: int,
) -> None:
    """Whatever the status, the answer and its request (whose headers hold the tenant admin key) are in
    no frame the error is raised from, and the words quoted from it hold no tenant admin key."""
    echoing = {"error": f"refused {TOKEN}", "allocation_api_key": KEY}
    with pytest.raises(EngineError) as failed:
        await answering(status, echoing).write_as(DeliveredAllocation, "/allocation", {"operation_ref": "a"})
    assert clean(failed.value)


# -- the adapter that reads the key out of the answer ------------------------------------------------------


def manager_over(handle: Callable[[httpx.Request], httpx.Response]) -> Any:
    """The manager for alice over a clearinghouse that answers as ``handle`` does, once it has said the
    key is tenant ``grant_1``'s."""
    answers = wire(httpx.MockTransport(saying_whose_key_it_is(handle)))
    return ClearinghouseAllocations(answers, NoActors(), MemorySecretStore()).for_actor("grant_1", "alice")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("body", "error"),
    [
        (HANDED | {"status": "no-such-status"}, ProviderError),  # the engine cannot read it
        (HANDED | {"replayed": True}, EngineError),  # it says it is a repeat, and carries a key all the same
        (HANDED | {"unknown_field": 1}, ProviderError),  # a field of a later release (T37)
        ({"allocation_api_key": KEY}, ProviderError),
    ],
    ids=["a status the engine does not know", "replayed with a key", "an unknown field", "the key alone"],
)
async def test_no_error_of_an_allotment_whose_answer_holds_a_key_carries_the_answer_or_the_key(
    body: dict[str, Any], error: type[Exception], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    handed = manager_over(lambda _: httpx.Response(201, json=body))
    with pytest.raises(error) as failed:
        await handed.create_key_for_grant("grant_1", "grant_1/alice", FIVE, "k")
    assert clean(failed.value)
    logged = "\n".join(f"{record.getMessage()} {vars(record)}" for record in caplog.records)
    assert KEY not in logged and TOKEN not in logged


async def test_no_error_of_a_rotation_whose_answer_holds_a_key_carries_the_answer_or_the_key() -> None:
    """The rotation is answered for another allocation than the one named: no success, with the key in
    hand."""
    other = HANDED | {"allocation_id": "alloc_2"}

    def handle(request: httpx.Request) -> httpx.Response:
        rotating = request.url.path.endswith("/rotate-payment-key")
        return httpx.Response(200, json=other if rotating else ALLOTTED)

    with pytest.raises(ProviderError) as refused:
        await manager_over(handle).create_key("alloc_1", "n", "k")
    assert refused.value.status_code == 200 and clean(refused.value)


@pytest.mark.parametrize("command", ["allot", "rotate"])
async def test_no_error_of_a_command_answered_for_another_tenant_carries_the_answer_or_the_key(
    command: str, caplog: pytest.LogCaptureFixture
) -> None:
    """#165: the answer states another tenant than the engine's and hands a key over. The command is no
    success, and the key goes with the answer: the error, its frames and the log hold neither."""
    caplog.set_level(logging.DEBUG)
    theirs = HANDED | {"tenant_id": "grant_2", "grant_id": "grant_2"}

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=theirs if request.method == "POST" else ALLOTTED)

    manager = manager_over(handle)
    calls = {
        "allot": lambda: manager.create_key_for_grant("grant_1", "grant_1/alice", FIVE, "k"),
        "rotate": lambda: manager.create_key("alloc_1", "n", "k"),
    }
    with pytest.raises(ProviderError) as applied:
        await calls[command]()

    assert applied.value.status_code == 200 and "'grant_2'" in applied.value.message
    assert "is not kept here" in applied.value.message and clean(applied.value)
    logged = "\n".join(f"{record.getMessage()} {vars(record)}" for record in caplog.records)
    assert KEY not in logged and TOKEN not in logged


async def test_a_rotation_whose_answer_is_lost_carries_nothing_of_the_request() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=ALLOTTED)
        raise httpx.ReadError("the connection dropped", request=request)

    with pytest.raises(ProviderOutcomeUnknown) as unknown:
        await manager_over(handle).create_key("alloc_1", "n", "k")
    assert clean(unknown.value)


# -- the discovery reader, which carries the key to the clearinghouse's runner list -----------------------

RUNNER_LIST = BASE + "/v1/clearinghouse/discovery"
Handler = Callable[[httpx.Request], Any]


def everywhere(error: BaseException) -> str:
    """Everything reachable from ``error``: its own words and attributes, the request and the answer an
    httpx error holds, the locals of every frame of its traceback outside this file (the HTTP client's
    frames too), and the same for every error it chains, however deep."""
    seen: list[str] = []
    pending: list[BaseException] = [error]
    visited: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in visited:
            continue
        visited.add(id(current))
        seen += [
            str(current),
            repr(current),
            repr(vars(current)),
            "".join(traceback.format_exception(current)),
        ]
        seen += [held(getattr(current, name, None)) for name in ("_request", "response")]
        step: TracebackType | None = current.__traceback__
        while step is not None:
            if step.tb_frame.f_code.co_filename != __file__:
                seen += [held(value) for value in step.tb_frame.f_locals.values()]
            step = step.tb_next
        pending += [chained for chained in (current.__cause__, current.__context__) if chained is not None]
    return "\n".join(seen)


def reader(handler: Handler, *, bearer: str | None = TOKEN, timeout_s: float = 5.0) -> HttpDiscoverySource:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return HttpDiscoverySource(RUNNER_LIST, client=client, bearer=bearer, timeout_s=timeout_s)


def answers(status: int, **more: Any) -> Handler:
    return lambda request: httpx.Response(status, **more)


def fails(failure: type[httpx.TransportError]) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        raise failure("the network failed", request=request)

    return handler


class NotGzip(httpx.AsyncByteStream):
    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b"this is not gzip"


async def never_answers(request: httpx.Request) -> httpx.Response:
    await asyncio.sleep(30)
    return httpx.Response(200, json=[])


FAILED_READS: list[Any] = [
    pytest.param(
        answers(401, json={"error": f"unknown key {TOKEN}"}), httpx.HTTPStatusError, "401", id="401"
    ),
    pytest.param(answers(503, json={"error": "no list yet"}), httpx.HTTPStatusError, "503", id="503"),
    pytest.param(answers(500, text=f"boom {TOKEN}"), httpx.HTTPStatusError, "500", id="500"),
    pytest.param(
        answers(307, headers={"Location": "https://elsewhere.test/discovery"}),
        httpx.HTTPStatusError,
        "307",
        id="a redirect",
    ),
    pytest.param(fails(httpx.ReadTimeout), TimeoutError, "no inventory", id="a timeout of the client"),
    pytest.param(never_answers, TimeoutError, "no inventory", id="a timeout of the whole read"),
    pytest.param(fails(httpx.ConnectError), httpx.ConnectError, "the network failed", id="no connection"),
    pytest.param(fails(httpx.ReadError), httpx.ReadError, "the network failed", id="a dropped connection"),
    pytest.param(answers(200, json={"runners": []}), ValueError, "not a JSON list", id="not the document"),
    pytest.param(answers(200, text="<html>a proxy's page</html>"), ValueError, "", id="not JSON"),
    pytest.param(
        lambda request: httpx.Response(200, headers={"Content-Encoding": "gzip"}, stream=NotGzip()),
        ValueError,
        "did not decode",
        id="a body that does not decode",
    ),
]


@pytest.mark.parametrize(("handler", "kind", "words"), FAILED_READS)
async def test_a_failed_read_of_the_runner_list_leads_to_no_tenant_admin_key(
    handler: Handler, kind: type[Exception], words: str, caplog: pytest.LogCaptureFixture
) -> None:
    """The error is of the kind the reader always raised, with the same words, and nothing reachable from
    it holds the key: not the error, not what it chains, not a frame of its traceback, not a log record."""
    caplog.set_level(logging.DEBUG)
    sent: list[str | None] = []

    async def recorded(request: httpx.Request) -> httpx.Response:
        sent.append(request.headers.get("Authorization"))
        answer = handler(request)
        return await answer if asyncio.iscoroutine(answer) else answer

    source = reader(recorded, timeout_s=0.05 if handler is never_answers else 5.0)
    with pytest.raises(kind) as failed:
        await source.fetch()

    assert sent == [f"Bearer {TOKEN}"]  # the key was on the request that failed
    assert words in str(failed.value)
    assert TOKEN not in everywhere(failed.value)
    assert TOKEN not in "\n".join(f"{record.getMessage()} {vars(record)}" for record in caplog.records)
    assert TOKEN not in repr(source) + repr(vars(source))
    assert source.document() is None


async def test_an_error_status_of_the_runner_list_still_says_its_status_and_its_address() -> None:
    """What a caller read from the reader's error before still reads: the status, on the answer, and the
    address, on the request. Neither is the one that carried the key."""
    with pytest.raises(httpx.HTTPStatusError) as refused:
        await reader(answers(401, headers={"WWW-Authenticate": "Bearer"})).fetch()
    assert refused.value.response.status_code == 401
    assert str(refused.value.request.url) == RUNNER_LIST and refused.value.request.method == "GET"
    assert "authorization" not in refused.value.request.headers
    assert refused.value.response.request is refused.value.request


async def test_a_timeout_of_the_runner_list_names_what_timed_out_by_its_kind_alone() -> None:
    with pytest.raises(TimeoutError) as slow:
        await reader(fails(httpx.ReadTimeout)).fetch()
    cause = slow.value.__cause__
    assert type(cause) is httpx.ReadTimeout and cause.__traceback__ is None
    with pytest.raises(RuntimeError):  # no request is set on it
        _ = cause.request


async def test_a_plain_discovery_address_fails_as_it_always_did() -> None:
    """With no credential there is nothing to keep off an error, and nothing changes: the client's own
    error is raised, with the request it sent and the answer it got, and a timeout chains the client's."""
    requests: list[httpx.Request] = []

    def refuse(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(401, json={"error": "no"}, headers={"WWW-Authenticate": "Bearer"})

    with pytest.raises(httpx.HTTPStatusError) as refused:
        await reader(refuse, bearer=None).fetch()
    assert refused.value.request is requests[0] and "authorization" not in requests[0].headers
    assert refused.value.response.headers["WWW-Authenticate"] == "Bearer"

    def slow(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise httpx.ReadTimeout("", request=request)

    with pytest.raises(TimeoutError, match="no inventory") as timed_out:
        await reader(slow, bearer=None).fetch()
    cause = timed_out.value.__cause__
    assert isinstance(cause, httpx.ReadTimeout) and cause.request is requests[1]

    def refused_connection(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(httpx.ConnectError) as unreachable:
        await reader(refused_connection, bearer=None).fetch()
    assert unreachable.value.request is not None


class Odd(Exception):
    """An error whose constructor takes more than its words, as a transport's own may."""

    def __init__(self, request: httpx.Request, reason: str) -> None:
        super().__init__(f"odd failure: {reason}")
        self.request = request


async def test_an_error_that_cannot_be_made_again_from_its_words_is_named_and_holds_no_request() -> None:
    def odd(request: httpx.Request) -> httpx.Response:
        raise Odd(request, "the transport broke")

    with pytest.raises(RuntimeError, match="Odd: odd failure: the transport broke") as failed:
        await reader(odd).fetch()
    assert TOKEN not in everywhere(failed.value)
    with pytest.raises(Odd) as plain:  # with no credential, the transport's own error, as before
        await reader(odd, bearer=None).fetch()
    assert plain.value.request is not None
