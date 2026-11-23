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
"""

import logging
import traceback
from types import TracebackType
from typing import Any

import httpx
import pytest

from livepeer_builder.adapters.clearinghouse import ClearinghouseAllocations
from livepeer_builder.adapters.clearinghouse_wire import ClearinghouseWire
from livepeer_builder.contracts import DeliveredAllocation, Money
from livepeer_builder.errors import EngineError, ProviderError, ProviderOutcomeUnknown, ProviderUnavailable
from livepeer_builder.testing import MemorySecretStore
from tests.unit.test_clearinghouse_adapters import ALLOTTED, HANDED, KEY, NoActors

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


def manager_over(answers: ClearinghouseWire) -> Any:
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
    with pytest.raises(error) as failed:
        await manager_over(answering(201, body)).create_key_for_grant("grant_1", "grant_1/alice", FIVE, "k")
    assert clean(failed.value)
    logged = "\n".join(f"{record.getMessage()} {vars(record)}" for record in caplog.records)
    assert KEY not in logged and TOKEN not in logged


async def test_no_error_of_a_rotation_whose_answer_holds_a_key_carries_the_answer_or_the_key() -> None:
    """The rotation is answered for another allocation than the one named: refused, with the key in hand."""
    other = HANDED | {"allocation_id": "alloc_2"}

    def handle(request: httpx.Request) -> httpx.Response:
        rotating = request.url.path.endswith("/rotate-payment-key")
        return httpx.Response(200, json=other if rotating else ALLOTTED)

    with pytest.raises(ProviderError) as refused:
        await manager_over(wire(httpx.MockTransport(handle))).create_key("alloc_1", "n", "k")
    assert refused.value.status_code == 404 and clean(refused.value)


async def test_a_rotation_whose_answer_is_lost_carries_nothing_of_the_request() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=ALLOTTED)
        raise httpx.ReadError("the connection dropped", request=request)

    with pytest.raises(ProviderOutcomeUnknown) as unknown:
        await manager_over(wire(httpx.MockTransport(handle))).create_key("alloc_1", "n", "k")
    assert clean(unknown.value)
