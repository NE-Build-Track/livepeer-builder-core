"""Clearinghouse Batteries: management API (AllocationManager) and usage feed (UsageSource).

The only module that knows the Batteries wire format. Baseline: eliteprox/clearinghouse-batteries
``ca09e47`` (docs/references/upstream-baseline.md). Every route needs the least-privilege management
credential in ``Livepeer-Clearinghouse-Token``. It may ``grants.fund`` the configured grant
(``LPB_GRANT_ID``). It has no ``grants.*``.
"""

import json
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from pydantic import SecretStr

from livepeer_builder.contracts import Allowance, Currency, Money, NewAllocationKey, UsagePage, UsageRow
from livepeer_builder.errors import CursorMismatch, ProviderError, ProviderUnavailable

TOKEN_HEADER = "Livepeer-Clearinghouse-Token"  # noqa: S105 - a header name
_USAGE_STATUSES = frozenset({"applied", "quarantined", "ignored", "duplicate"})


class BatteriesClient:
    """AllocationManager over the Batteries management HTTP API."""

    def __init__(self, base_url: str, token: str, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"), headers={TOKEN_HEADER: token}, timeout=30.0
        )
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def create_key_for_grant(
        self, grant_id: str, name: str, amount: Money, idempotency_key: str
    ) -> NewAllocationKey:
        """Atomic: creates a funded allocation and its key in one call (needs allocations.create/fund)."""
        body = await self._request(
            "POST",
            "/v1/api-keys",
            json={"grant_id": grant_id, "name": name, **_amount_fields(amount)},
            idempotency_key=idempotency_key,
        )
        return _new_key(body)

    async def create_key(self, allocation_id: str, name: str, idempotency_key: str) -> NewAllocationKey:
        body = await self._request(
            "POST",
            "/v1/api-keys",
            json={"allocation_id": allocation_id, "name": name},
            idempotency_key=idempotency_key,
        )
        return _new_key(body)

    async def fund_allocation(self, allocation_id: str, amount: Money, idempotency_key: str) -> None:
        await self._request(
            "POST",
            f"/v1/allocations/{allocation_id}/fund",
            json=_amount_fields(amount),
            idempotency_key=idempotency_key,
        )

    async def fund_grant(self, grant_id: str, amount: Money, idempotency_key: str) -> None:
        await self._request(
            "POST",
            f"/v1/grants/{grant_id}/fund",
            json=_amount_fields(amount),
            idempotency_key=idempotency_key,
        )

    async def set_allocation_status(self, allocation_id: str, status: str) -> None:
        await self._request("PATCH", f"/v1/allocations/{allocation_id}/status", json={"status": status})

    async def revoke_allocation(self, allocation_id: str) -> None:
        await self._request("POST", f"/v1/allocations/{allocation_id}/revoke")  # repeat-safe upstream

    async def revoke_key(self, api_key_id: str) -> None:
        await self._request("POST", f"/v1/api-keys/{api_key_id}/revoke")

    async def read_allocation(self, allocation_id: str) -> Allowance:
        return allowance_from_body(await self._request("GET", f"/v1/allocations/{allocation_id}"))

    async def _request(
        self,
        method: str,
        path: str,
        json: dict[str, str] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        try:
            response = await self._client.request(method, path, json=json, headers=headers)
        except httpx.TransportError as exc:
            raise ProviderUnavailable(f"Batteries unreachable: {exc}") from exc
        if response.status_code >= 400:
            raise ProviderError(response.status_code, error_message(response))
        return dict(response.json()) if response.content else {}


class BatteriesUsageSource:
    """UsageSource over ``GET /v1/usage``. The cursor is opaque and bound to its filters."""

    def __init__(
        self,
        base_url: str,
        token: str,
        filters: dict[str, str] | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._filters = dict(filters or {})
        self._client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"), headers={TOKEN_HEADER: token}, timeout=30.0
        )
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def page(self, cursor: str | None, limit: int = 1000) -> UsagePage:
        params = {**self._filters, "limit": str(limit)}
        if cursor:
            params["cursor"] = cursor
        try:
            response = await self._client.get("/v1/usage", params=params)
        except httpx.TransportError as exc:
            raise ProviderUnavailable(f"Batteries unreachable: {exc}") from exc
        message = error_message(response) if response.status_code >= 400 else ""
        if response.status_code == 400 and "cursor" in message:
            raise CursorMismatch(message)
        if response.status_code >= 400:
            raise ProviderError(response.status_code, message)
        body = response.json()
        return UsagePage(
            items=tuple(usage_row_from_item(item) for item in body["items"]),
            next_cursor=str(body.get("next_cursor") or ""),
        )


def usage_row_from_item(item: dict[str, Any]) -> UsageRow:
    status = item.get("status")
    if status not in _USAGE_STATUSES:
        raise ProviderError(200, f"unknown usage status {status!r}")
    return UsageRow(
        id=str(item["id"]),
        event_id=_text(item.get("event_id")),
        status=status,
        manifest_id=_text(item.get("manifest_id")),
        allocation_id=_text(item.get("allocation_id")),
        payment_session_id=_text(item.get("payment_session_id")),
        request_id=_text(item.get("request_id")),
        pipeline=_text(item.get("pipeline")),
        computed_fee_eth=_decimal(item.get("computed_fee_eth")),
        computed_fee_usd=_decimal(item.get("computed_fee_usd")),
        currency=_text(item.get("currency")),
        created_at=datetime.fromtimestamp(int(item["created_at_ms"]) / 1000, tz=UTC),
    )


def allowance_from_body(body: dict[str, Any]) -> Allowance:
    """Allocation reads carry allocated_/available_/spent_ suffixed by the allocation's currency."""
    currency: Currency = "USD" if str(body.get("currency", "")).lower() == "usd" else "ETH"
    suffix = currency.lower()
    return Allowance(
        allocation_id=str(body["id"]),
        status=str(body.get("status", "")),
        granted=_money(body.get(f"allocated_{suffix}"), currency),
        spent=_money(body.get(f"spent_{suffix}"), currency),
        available=_money(body.get(f"available_{suffix}"), currency),
    )


def error_message(response: httpx.Response) -> str:
    try:
        body = response.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return response.text
    if isinstance(body, dict) and isinstance(body.get("error"), str):
        return str(body["error"])
    return response.text


def _amount_fields(amount: Money) -> dict[str, str]:
    return {f"amount_{amount.currency.lower()}": format(amount.amount, "f")}


def _new_key(body: dict[str, Any]) -> NewAllocationKey:
    return NewAllocationKey(
        allocation_id=str(body["allocation_id"]),
        api_key_id=str(body["id"]),
        secret=SecretStr(str(body["api_key"])),
    )


def _money(value: Any, currency: Currency) -> Money | None:
    amount = _decimal(value)
    return None if amount is None else Money(amount=amount, currency=currency)


def _text(value: Any) -> str | None:
    return None if value is None or value == "" else str(value)


def _decimal(value: Any) -> Decimal | None:
    if value is None or value in ("", "[error]"):
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None
