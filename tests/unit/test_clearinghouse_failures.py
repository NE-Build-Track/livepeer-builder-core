"""#168: what a failure at the clearinghouse means to the engine that called it (D87).

A clearinghouse failure is classified like a signer failure: unreachable or not ready is ``retry``, an
unknown outcome is ``check``, and a refusal keeps its reason. For the four ports the remote adapters
serve, that is three kinds of error:

- ``ProviderUnavailable``: the clearinghouse was not reached, or said it cannot answer now. Nothing was
  applied;
- ``ProviderOutcomeUnknown``: a command left and no answer says whether it was applied. The operation
  ledger keeps the reference, and the repeat is answered by the clearinghouse's own ledger;
- ``ProviderError``: the clearinghouse answered, with its status and its own words.

A read applies nothing, so a read is never an unknown outcome. The adapter itself never sends a request
twice.
"""

import asyncio
import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from livepeer_builder.adapters.clearinghouse_wire import ClearinghouseWire
from livepeer_builder.contracts import TenantGrant
from livepeer_builder.errors import CursorMismatch, ProviderError, ProviderOutcomeUnknown, ProviderUnavailable
from livepeer_builder.services.ledger import definite_refusal

BASE = "https://clearinghouse.test"
TOKEN = "lpk_tenant-admin-key-not-to-be-shown"
Handler = Callable[[httpx.Request], httpx.Response]

RETRY, CHECK, REFUSED, UNREADABLE = "retry", "check", "refused", "unreadable"


def kind_of(error: Exception) -> str:
    if isinstance(error, ProviderOutcomeUnknown):
        return CHECK
    if isinstance(error, ProviderUnavailable):
        return RETRY
    assert isinstance(error, ProviderError)
    return REFUSED if 400 <= error.status_code < 500 else UNREADABLE


class Clearinghouse:
    """A clearinghouse that answers as the test says, and keeps every request it was sent."""

    def __init__(self, answer: Handler) -> None:
        self.requests: list[httpx.Request] = []
        self._answer = answer
        self.wire = ClearinghouseWire(
            BASE, TOKEN, "grant_1", httpx.AsyncClient(transport=httpx.MockTransport(self._handle))
        )

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._answer(request)


def raises(error: Exception) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        raise error

    return handler


def answers(status: int, body: Any = None, **headers: str) -> Handler:
    content = body if isinstance(body, bytes) else json.dumps(body if body is not None else {}).encode()
    return lambda request: httpx.Response(status, content=content, headers=headers)


def problem(status: int, message: str = "said no", **extra: Any) -> Handler:
    return answers(status, {"error": message, **extra})


NEVER_REACHED = [
    httpx.ConnectError("connection refused"),
    httpx.ConnectTimeout("connect timed out"),
    httpx.PoolTimeout("no connection free"),
]
AFTER_SENDING = [
    httpx.ReadTimeout("no answer"),
    httpx.WriteTimeout("stalled"),
    httpx.ReadError("reset"),
    httpx.WriteError("broken pipe"),
    httpx.RemoteProtocolError("server disconnected"),
]
# status -> what a failed write is, and a failed read
STATUSES: dict[int, tuple[str, str]] = {
    400: (REFUSED, REFUSED),
    401: (REFUSED, REFUSED),
    403: (REFUSED, REFUSED),
    404: (REFUSED, REFUSED),
    409: (REFUSED, REFUSED),
    422: (REFUSED, REFUSED),
    408: (CHECK, RETRY),  # the clearinghouse gave up waiting: the command may have arrived
    429: (RETRY, RETRY),  # turned away before it was handled
    500: (CHECK, UNREADABLE),
    502: (CHECK, UNREADABLE),
    503: (CHECK, RETRY),  # not ready; a write that was answered so may still have been applied
    504: (CHECK, UNREADABLE),
}


async def send(house: Clearinghouse, method: str) -> None:
    if method == "POST":
        await house.wire.write("/v1/clearinghouse/x", {"operation_ref": "r"})
    else:
        await house.wire.read("/v1/clearinghouse/x")


async def failure_of(house: Clearinghouse, method: str) -> Exception:
    with pytest.raises((ProviderError, ProviderUnavailable)) as failed:
        await send(house, method)
    assert len(house.requests) <= 1  # never sent twice
    return failed.value


@pytest.mark.parametrize("error", NEVER_REACHED, ids=lambda error: type(error).__name__)
@pytest.mark.parametrize("method", ["GET", "POST"])
async def test_a_clearinghouse_that_was_not_reached_is_retry_for_a_read_and_a_write(
    error: Exception, method: str
) -> None:
    failure = await failure_of(Clearinghouse(raises(error)), method)
    assert kind_of(failure) == RETRY
    assert "nothing was sent" in str(failure) or method == "GET"
    assert not definite_refusal(failure)  # the ledger keeps the reference for the repeat


@pytest.mark.parametrize("error", AFTER_SENDING, ids=lambda error: type(error).__name__)
async def test_a_write_that_left_and_got_no_answer_is_an_unknown_outcome(error: Exception) -> None:
    """The request left and no answer came: it may have been applied. Never a refusal, which would free
    the reference, and never sent again by the adapter, which could allot twice under another key."""
    house = Clearinghouse(raises(error))
    failure = await failure_of(house, "POST")
    assert kind_of(failure) == CHECK and len(house.requests) == 1
    assert "may have been applied" in str(failure)
    assert not definite_refusal(failure)


@pytest.mark.parametrize("error", AFTER_SENDING, ids=lambda error: type(error).__name__)
async def test_a_read_that_got_no_answer_applied_nothing_and_is_retry(error: Exception) -> None:
    failure = await failure_of(Clearinghouse(raises(error)), "GET")
    assert kind_of(failure) == RETRY


@pytest.mark.parametrize("status", sorted(STATUSES))
@pytest.mark.parametrize("method", ["GET", "POST"])
async def test_each_error_status_is_classified(status: int, method: str) -> None:
    failure = await failure_of(Clearinghouse(problem(status)), method)
    write, read = STATUSES[status]
    expected = write if method == "POST" else read
    assert kind_of(failure) == expected, failure
    assert definite_refusal(failure) is (expected == REFUSED)


@pytest.mark.parametrize("status", [401, 403, 404, 409, 422])
async def test_a_refusal_keeps_its_status_and_the_clearinghouse_own_words(status: int) -> None:
    failure = await failure_of(Clearinghouse(problem(status, "resource belongs to another tenant")), "POST")
    assert isinstance(failure, ProviderError)
    assert (failure.status_code, failure.message) == (status, "resource belongs to another tenant")


async def test_a_refusal_with_no_json_body_keeps_its_status() -> None:
    failure = await failure_of(Clearinghouse(answers(403, b"<html>forbidden</html>")), "POST")
    assert isinstance(failure, ProviderError) and failure.status_code == 403


async def test_a_command_the_clearinghouse_is_still_running_is_an_unknown_outcome_not_a_refusal() -> None:
    """409 ``operation_in_progress``: an earlier request with this reference has not finished there. A
    refusal would free the reference here while the command may still be applied there."""
    running = problem(409, "still running", code="operation_in_progress")
    failure = await failure_of(Clearinghouse(running), "POST")
    assert kind_of(failure) == CHECK and not definite_refusal(failure)
    for code in ("operation_conflict", "already_allotted", "operation_exists"):
        refused = await failure_of(Clearinghouse(problem(409, "no", code=code)), "POST")
        assert kind_of(refused) == REFUSED and definite_refusal(refused)


async def test_a_refused_cursor_is_a_cursor_mismatch() -> None:
    house = Clearinghouse(problem(400, "invalid cursor", code="cursor_mismatch"))
    with pytest.raises(CursorMismatch):
        await house.wire.read("/v1/clearinghouse/usage", {"cursor": "c9"})


@pytest.mark.parametrize("body", [b"not json", b"[1, 2]", b'"text"', b"", b'{"tenant_id": 7}'])
@pytest.mark.parametrize("method", ["GET", "POST"])
async def test_an_answer_that_is_not_the_expected_shape_is_never_a_refusal(body: bytes, method: str) -> None:
    """A 2xx the engine cannot read: the command may have been applied, so the reference stays held."""
    house = Clearinghouse(answers(200, body))
    ask = (
        house.wire.write_as(TenantGrant, "/x", {})
        if method == "POST"
        else house.wire.read_as(TenantGrant, "/x")
    )
    with pytest.raises(ProviderError) as unreadable:
        await ask
    assert unreadable.value.status_code == 200 and not definite_refusal(unreadable.value)
    assert "7" not in unreadable.value.message  # the body is not quoted


@pytest.mark.parametrize("method", ["GET", "POST"])
async def test_a_redirect_is_not_followed_and_is_no_answer(method: str) -> None:
    """The tenant admin key would follow a redirect to whatever address it names."""
    house = Clearinghouse(answers(307, {"tenant_id": "x"}, location="https://elsewhere.test/x"))
    failure = await failure_of(house, method)
    assert isinstance(failure, ProviderError) and failure.status_code == 307
    assert not definite_refusal(failure)
    assert [request.url.host for request in house.requests] == ["clearinghouse.test"]


async def test_a_request_past_its_deadline_is_stopped_and_classified() -> None:
    async def never(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(30)
        return httpx.Response(200, json={})

    client = httpx.AsyncClient(transport=httpx.MockTransport(never))
    wire = ClearinghouseWire(BASE, TOKEN, "grant_1", client, timeout_s=0.05)
    with pytest.raises(ProviderOutcomeUnknown):
        await wire.write("/x", {})
    with pytest.raises(ProviderUnavailable) as read:
        await wire.read("/x")
    assert not isinstance(read.value, ProviderOutcomeUnknown)


# -- the tenant admin key -------------------------------------------------------------------------------


async def test_every_request_carries_the_key_as_a_bearer_and_goes_to_the_clearinghouse() -> None:
    house = Clearinghouse(answers(200, {}))
    await house.wire.read("/v1/clearinghouse/grant", {"limit": "1"})
    await house.wire.write("/v1/clearinghouse/actors/a/allocation/revoke")
    assert [request.headers["authorization"] for request in house.requests] == [f"Bearer {TOKEN}"] * 2
    assert [str(request.url) for request in house.requests] == [
        BASE + "/v1/clearinghouse/grant?limit=1",
        BASE + "/v1/clearinghouse/actors/a/allocation/revoke",
    ]


async def test_the_key_is_in_no_repr_and_no_error_even_when_the_clearinghouse_echoes_it() -> None:
    echo = problem(403, f"bad key {TOKEN}")
    house = Clearinghouse(echo)
    failure = await failure_of(house, "POST")
    for text in (repr(house.wire), str(house.wire), str(vars(house.wire)), str(failure), repr(failure)):
        assert TOKEN not in text
    for error in (*NEVER_REACHED, *AFTER_SENDING):
        for method in ("GET", "POST"):
            lost = await failure_of(Clearinghouse(raises(error)), method)
            assert TOKEN not in str(lost) and TOKEN not in repr(lost.__cause__)


async def test_a_wire_that_made_its_own_client_closes_it_and_a_borrowed_one_is_left_open() -> None:
    own = ClearinghouseWire(BASE, TOKEN, "grant_1")
    await own.aclose()
    borrowed = httpx.AsyncClient(transport=httpx.MockTransport(answers(200, {})))
    wire = ClearinghouseWire(BASE, TOKEN, "grant_1", borrowed)
    await wire.aclose()
    assert not borrowed.is_closed
    await borrowed.aclose()
