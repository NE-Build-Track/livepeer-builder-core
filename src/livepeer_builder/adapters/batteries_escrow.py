"""Batteries' escrow report, read as the wallet's escrow accounts (#167).

Split from ``batteries.py``, which holds the HTTP calls: this module holds no client, only what the
answer of ``GET /v1/escrow/report`` means. Baseline: livepeer/clearinghouse-batteries v0.0.3
(docs/references/batteries-api.md). The report is a JSON array, one object for each chain, TicketBroker
contract and sender that Batteries' on-chain listener follows, and empty when the listener is off. The
store holds wei; the management API serves each amount as ``..._eth``, a decimal string, and
``confirmed_through_block`` as a decimal string too (``internal/app/presentation.go``).
"""

from decimal import Decimal, InvalidOperation
from typing import Any

from livepeer_builder.contracts import EscrowAccount, Money
from livepeer_builder.errors import ProviderError


def read_escrow_report(body: object) -> tuple[EscrowAccount, ...]:
    """The report's accounts, in Batteries' order. Anything that is not the report is an answer the
    engine cannot read: a ``ProviderError`` that quotes no value, never an empty report and never a
    zero, which would tell a clearinghouse admin the wallet holds nothing."""
    if not isinstance(body, list) or not all(isinstance(item, dict) for item in body):
        raise ProviderError(200, "Batteries answered an escrow report that is not a list of objects")
    return tuple(_account(item) for item in body)


def _account(item: dict[str, Any]) -> EscrowAccount:
    return EscrowAccount(
        chain_id=_text(item, "chain_id"),
        contract_address=_text(item, "contract_address"),
        sender=_text(item, "sender"),
        deposit=Money(amount=_eth(item, "deposit_balance_eth"), currency="ETH"),
        reserve=Money(amount=_eth(item, "reserve_balance_eth"), currency="ETH"),
        confirmed_through_block=_block(item),
    )


def _text(item: dict[str, Any], key: str) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value:
        raise ProviderError(200, f"Batteries answered an escrow account with no usable {key}")
    return value


def _decimal(text: str) -> Decimal | None:
    """A decimal string as a ``Decimal``, or ``None`` for one that is no number (``""``, ``[error]``)."""
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _eth(item: dict[str, Any], key: str) -> Decimal:
    """A decimal string of ETH. A number, ``""`` and ``[error]`` (what Batteries serves for an amount it
    could not convert) are not amounts, and neither is one below zero: a deposit or a reserve is never
    negative, so the report says nothing the engine can pass on as a balance."""
    value = item.get(key)
    amount = _decimal(value) if isinstance(value, str) else None
    if amount is None or not amount.is_finite() or amount < 0:
        raise ProviderError(200, f"Batteries answered an escrow account with no usable {key}")
    return amount


def _block(item: dict[str, Any]) -> int:
    """The last block the listener confirmed, as Batteries serves it: a string of digits, or ``-1`` for
    a snapshot taken before the chain's first block. No other height is below zero."""
    value = item.get("confirmed_through_block")
    digits = "" if not isinstance(value, str) else "0" if value == "-1" else value
    if not (digits.isascii() and digits.isdigit()):
        raise ProviderError(
            200, "Batteries answered an escrow account with no usable confirmed_through_block"
        )
    return int(str(value))
