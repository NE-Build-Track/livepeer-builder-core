"""Clearinghouse Batteries: management API (AllocationManager) and usage feed (UsageSource).

The only module that knows the Batteries wire format. Baseline: livepeer/clearinghouse-batteries
``ca09e47`` (docs/references/upstream-baseline.md). Every route needs the least-privilege management
credential in ``Livepeer-Clearinghouse-Token``. It may ``grants.fund`` any grant the engine names.
It has no ``grants.*``.
"""

import asyncio
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from pydantic import SecretStr

from livepeer_builder.contracts import (
    Allowance,
    Currency,
    GrantRecord,
    Money,
    NewAllocationKey,
    UsagePage,
    UsageRow,
)
from livepeer_builder.errors import CursorMismatch, ProviderError, ProviderUnavailable

TOKEN_HEADER = "Livepeer-Clearinghouse-Token"  # noqa: S105 - a header name
_USAGE_STATUSES = frozenset({"applied", "quarantined", "ignored", "duplicate"})


def batteries_http_client(base_url: str, token: str, *, timeout_s: float) -> httpx.AsyncClient:
    """The HTTP client the Batteries adapters build when none is passed in (they own and close it)."""
    return httpx.AsyncClient(base_url=base_url.rstrip("/"), headers={TOKEN_HEADER: token}, timeout=timeout_s)


class BatteriesClient:
    """AllocationManager over the Batteries management HTTP API."""

    def __init__(
        self, base_url: str, token: str, client: httpx.AsyncClient | None = None, *, timeout_s: float = 30.0
    ) -> None:
        self._client = client or batteries_http_client(base_url, token, timeout_s=timeout_s)
        self._owns_client = client is None
        self._timeout_s = timeout_s

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

    async def list_grants(self) -> list[GrantRecord]:
        """Every grant, page by page: a resumed grant create looks for its tag among them (D57)."""
        grants: list[GrantRecord] = []
        params = {"limit": "1000"}  # Batteries' largest page
        while True:
            body = await self._request("GET", "/v1/grants", params=params)
            items = body.get("items")
            grants += (
                [grant_from_item(item) for item in items if isinstance(item, dict)]
                if isinstance(items, list)
                else []
            )
            cursor = body.get("next_cursor")
            if not cursor:
                return grants
            params = {"limit": "1000", "cursor": str(cursor)}

    async def create_grant(
        self, name: str, amount: Money, metadata: Mapping[str, str] | None = None
    ) -> GrantRecord:
        body = {"name": name, "status": "active", "sponsor": name, **_amount_fields(amount)}
        if metadata:  # Batteries takes string fields only: the object travels as JSON text
            body["metadata"] = json.dumps(dict(metadata), sort_keys=True)
        created = await self._request("POST", "/v1/grants", json=body)
        if not created.get("id"):
            raise ProviderError(200, "grant response missing id")
        # Batteries answers the create with the id only; the record is the grant as it stored it. The grant
        # exists by now, so a failed read is an unknown outcome, never a refusal that frees the reference.
        try:
            return grant_from_item(await self._request("GET", f"/v1/grants/{created['id']}"))
        except ProviderError as exc:
            raise ProviderUnavailable(f"grant {created['id']} created but not read back: {exc}") from exc

    async def _request(
        self,
        method: str,
        path: str,
        json: dict[str, str] | None = None,
        idempotency_key: str | None = None,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        try:
            # One deadline for the whole call: httpx times each phase separately, and the operation ledger's
            # stale window is counted in whole calls (D57).
            async with asyncio.timeout(self._timeout_s):
                response = await self._client.request(method, path, json=json, headers=headers, params=params)
        except TimeoutError as exc:
            raise ProviderUnavailable(f"Batteries call timed out after {self._timeout_s:g} s") from exc
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
        *,
        timeout_s: float = 30.0,
    ) -> None:
        self._filters = dict(filters or {})
        self._client = client or batteries_http_client(base_url, token, timeout_s=timeout_s)
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


def grant_from_item(item: dict[str, Any]) -> GrantRecord:
    currency = str(item.get("currency", "")).lower()
    total_key = "total_usd" if currency == "usd" else "total_eth"
    return GrantRecord(
        id=str(item.get("id", "")),
        name=str(item.get("name", "")),
        status=str(item.get("status", "")),
        currency=currency.upper() or "USD",
        total=str(item.get(total_key, "")),
        metadata=_metadata(item.get("metadata")),
    )


def _metadata(raw: Any) -> dict[str, str]:
    """A grant's metadata when it is a JSON object of strings (the engine's tag); free text reads as {}."""
    try:
        parsed = json.loads(raw) if isinstance(raw, str) and raw else None
    except ValueError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {str(key): value for key, value in parsed.items() if isinstance(value, str)}


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
