"""The wallet's deposit and reserve as Batteries reports them (#167): ``GET /v1/escrow/report``.

Batteries at the pin (v0.0.3, ``internal/store/settlements.go``, ``EscrowReport``) answers a JSON array
with one object for each chain, TicketBroker contract and sender its on-chain listener follows. The
store holds wei; the management API serves each ``..._wei`` field as ``..._eth``, a decimal string
(``internal/app/presentation.go``). ``confirmed_through_block`` is the last block the listener has
confirmed, as a decimal string. With the listener off, which is Batteries' default, the array is empty.

The addresses below are made up: no test holds a real one.
"""

from decimal import Decimal
from typing import Any

import httpx
import pytest

from livepeer_builder.adapters.batteries import TOKEN_HEADER, BatteriesClient
from livepeer_builder.contracts import EscrowAccount, Money
from livepeer_builder.errors import ProviderError, ProviderUnavailable
from livepeer_builder.protocols import EscrowSource

BASE = "https://batteries.test"
CONTRACT = "0x" + "c0" * 20
SENDER = "0x" + "5e" * 20


def row(deposit: Any = "1.5", reserve: Any = "0.25", block: Any = "412", **changed: Any) -> dict[str, Any]:
    """One account of the report. The engine does not read ``total_balance_eth``: it is the sum of the
    other two."""
    return {
        "chain_id": "42161",
        "contract_address": CONTRACT,
        "sender": SENDER,
        "confirmed_through_block": block,
        "deposit_balance_eth": deposit,
        "reserve_balance_eth": reserve,
        "total_balance_eth": "1.75",
    } | changed


def client(answer: httpx.Response | Exception, seen: list[httpx.Request] | None = None) -> BatteriesClient:
    def handle(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        if isinstance(answer, Exception):
            raise answer
        return answer

    http = httpx.AsyncClient(
        base_url=BASE, headers={TOKEN_HEADER: "t"}, transport=httpx.MockTransport(handle)
    )
    return BatteriesClient(BASE, "t", client=http)


def eth(amount: str) -> Money:
    return Money(amount=Decimal(amount), currency="ETH")


async def test_the_escrow_report_is_read_as_one_account_for_each_sender() -> None:
    seen: list[httpx.Request] = []
    other = "0x" + "0b" * 20
    report = [row(), row("0", "0.000000000000000001", "-1", sender=other)]
    batteries = client(httpx.Response(200, json=report), seen)
    assert isinstance(batteries, EscrowSource)

    accounts = await batteries.escrow_accounts()

    assert accounts == (
        EscrowAccount(
            chain_id="42161",
            contract_address=CONTRACT,
            sender=SENDER,
            deposit=eth("1.5"),
            reserve=eth("0.25"),
            confirmed_through_block=412,
        ),
        EscrowAccount(
            chain_id="42161",
            contract_address=CONTRACT,
            sender=other,
            deposit=eth("0"),
            reserve=eth("0.000000000000000001"),  # one wei, exactly
            confirmed_through_block=-1,  # a snapshot from before the first block
        ),
    )
    assert [(request.method, request.url.path, request.url.query) for request in seen] == [
        ("GET", "/v1/escrow/report", b"")
    ]


async def test_a_batteries_whose_on_chain_listener_is_off_reports_no_account() -> None:
    assert await client(httpx.Response(200, json=[])).escrow_accounts() == ()


@pytest.mark.parametrize(
    "body",
    [
        {"items": [row()]},  # an object, as the lists are served: not this report
        [row(), "0x00"],  # an item that is no object
        [row(deposit="[error]")],  # what Batteries serves for an amount it could not convert
        [row(reserve="")],
        [row(deposit="1e3x")],
        [row(deposit="NaN")],
        [row(deposit="-1")],  # a balance is never below zero: not an amount the engine reports
        [row(reserve="-0.000000000000000001")],
        [row(deposit=1.5)],  # a number, never read as money
        [{key: value for key, value in row().items() if key != "reserve_balance_eth"}],
        [row(sender="")],
        [row(chain_id=42161)],
        [row(block="latest")],
        [row(block=412)],
        [row(block="-2")],  # -1 is the one height below zero Batteries serves: before the first block
        [row(block="--1")],
        [row(block="-")],
        None,
    ],
)
async def test_a_report_the_engine_cannot_read_is_an_error_and_never_a_zero(body: Any) -> None:
    """A deposit read as zero, or a report read as empty, would tell a clearinghouse admin the wallet is
    unfunded, or hide that it is. An answer that is not the report is a provider error (HTTP 502)."""
    with pytest.raises(ProviderError) as refused:
        await client(httpx.Response(200, json=body)).escrow_accounts()
    assert refused.value.status_code == 200
    assert SENDER not in str(refused.value) and CONTRACT not in str(refused.value)


async def test_a_credential_without_the_permission_is_told_which_one_it_needs() -> None:
    """Batteries answers 403 ``Forbidden`` to a credential without ``escrow.read``, which the grants
    credential did not need before #167."""
    with pytest.raises(ProviderError) as refused:
        await client(httpx.Response(403, json={"error": "Forbidden"})).escrow_accounts()
    assert refused.value.status_code == 403
    assert refused.value.message == "Forbidden: the grants credential needs the permission escrow.read"


async def test_a_credential_batteries_does_not_know_is_not_told_it_lacks_a_permission() -> None:
    """Batteries answers 401 to a token it does not know or has revoked. No permission would mend that:
    the failure passes through as the token failure it is, and names no permission to grant."""
    with pytest.raises(ProviderError) as refused:
        await client(httpx.Response(401, json={"error": "Unauthorized"})).escrow_accounts()
    assert refused.value.status_code == 401
    assert refused.value.message == "Unauthorized"
    assert "escrow.read" not in str(refused.value) and "permission" not in str(refused.value)


async def test_a_batteries_that_cannot_be_reached_or_redirects_is_no_report() -> None:
    with pytest.raises(ProviderUnavailable):
        await client(httpx.ConnectError("refused")).escrow_accounts()
    with pytest.raises(ProviderError):
        await client(httpx.Response(301, json=[row()])).escrow_accounts()
    with pytest.raises(ProviderError) as failed:
        await client(httpx.Response(500, text="internal server error")).escrow_accounts()
    assert failed.value.status_code == 500
