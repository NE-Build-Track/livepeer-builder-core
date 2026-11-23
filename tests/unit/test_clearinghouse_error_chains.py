"""#168 review: an error the clearinghouse adapter raises reaches no credential through its chain.

An answer that does not parse as the model asked for was refused with ``raise ... from None`` inside the
``except`` block. That hides the first error from a printed traceback, but Python still keeps it as
``__context__``, and a logger or error tracker that walks the chain reached it: pydantic's error, which
holds the whole answer, a payment key just issued included. A request that failed on the way kept
httpx's own error the same way, and its request carries the tenant admin key in its headers. Each error
is now built in the ``except`` block and raised after it, so nothing is chained but what is meant to be.
"""

import traceback

import httpx
import pytest

from livepeer_builder.adapters.clearinghouse_wire import ClearinghouseWire
from livepeer_builder.contracts import DeliveredAllocation
from livepeer_builder.errors import ProviderError, ProviderUnavailable

BASE = "https://clearinghouse.test"
TOKEN = "lpk_tenant-admin-key-throwaway"
PAYMENT_KEY = "lpp_throwaway-payment-key"
# a payment key just issued, in an answer that is not a DeliveredAllocation: it names no actor or allocation
UNREADABLE = {"payment_key": {"token": PAYMENT_KEY, "allocation_id": "alloc_1"}}


def wire(handler: httpx.MockTransport) -> ClearinghouseWire:
    return ClearinghouseWire(BASE, TOKEN, "grant_1", httpx.AsyncClient(transport=handler))


def reachable(error: BaseException) -> str:
    """Everything a logger can print of ``error`` and what it chains: its text, its repr, its traceback,
    and each chained error's attributes, an httpx request's headers among them."""
    seen = [str(error), repr(error), "".join(traceback.format_exception(error))]
    for chained in (error.__cause__, error.__context__):
        if chained is not None:
            seen += [repr(chained), repr(vars(chained))]
            request = getattr(chained, "_request", None)
            seen += [repr(dict(request.headers))] if request is not None else []
    return "\n".join(seen)


async def test_an_answer_that_does_not_parse_chains_nothing_that_holds_it() -> None:
    answers = wire(httpx.MockTransport(lambda request: httpx.Response(201, json=UNREADABLE)))
    with pytest.raises(ProviderError) as refused:
        await answers.write_as(DeliveredAllocation, "/allocation", {"operation_ref": "a-1"})
    assert refused.value.__cause__ is None and refused.value.__context__ is None
    assert PAYMENT_KEY not in reachable(refused.value)
    assert "not DeliveredAllocation" in str(refused.value)


@pytest.mark.parametrize("failure", [httpx.ConnectError, httpx.ReadTimeout], ids=["never sent", "no answer"])
async def test_a_request_that_failed_chains_no_request_that_holds_the_tenant_admin_key(
    failure: type[httpx.TransportError],
) -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise failure("the network failed", request=request)

    with pytest.raises(ProviderUnavailable) as unavailable:
        await wire(httpx.MockTransport(fail)).read_as(DeliveredAllocation, "/allocation")
    assert unavailable.value.__context__ is None
    assert TOKEN not in reachable(unavailable.value)
