"""Clearinghouse Batteries: management API (AllocationManager) and usage feed (UsageSource).

With ``batteries_usage_rows.py``, which reads one usage item, the only module that knows the Batteries
wire format. Baseline: livepeer/clearinghouse-batteries
v0.0.3, ``03bc9fb`` (docs/references/upstream-baseline.md). Every route needs the least-privilege
management credential in ``Livepeer-Clearinghouse-Token``. It may ``grants.fund`` any grant the engine
names. It has no ``grants.*``: creating, reading and pausing a grant use the grants credential
(``LPB_BATTERIES_GRANTS_TOKEN``). Batteries' ``grant_id`` is the engine's ``tenant_id`` (D83).
"""

import asyncio
import json
from collections.abc import Mapping
from decimal import Decimal
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import SecretStr

from livepeer_builder.adapters.batteries_usage_rows import decimal_or_none, read_usage_item
from livepeer_builder.contracts import (
    Allowance,
    Currency,
    GrantPage,
    GrantRecord,
    Money,
    NewAllocationKey,
    SetAsideUsage,
    TenantGrant,
    UsagePage,
    UsageRow,
)
from livepeer_builder.errors import CursorMismatch, ProviderError, ProviderUnavailable

TOKEN_HEADER = "Livepeer-Clearinghouse-Token"  # noqa: S105 - a header name
_GRANTS = "/v1/grants"
_NOTHING_HELD = (Decimal(0), Decimal(0))  # a grant with no allocation: nothing allotted, nothing spent


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

    async def top_up_allocation(self, allocation_id: str, amount: Money, idempotency_key: str) -> None:
        await self._request(
            "POST",
            f"/v1/allocations/{segment(allocation_id)}/fund",
            json=_amount_fields(amount),
            idempotency_key=idempotency_key,
        )

    async def top_up_grant(self, grant_id: str, amount: Money, idempotency_key: str) -> None:
        await self._request(
            "POST",
            f"/v1/grants/{segment(grant_id)}/fund",
            json=_amount_fields(amount),
            idempotency_key=idempotency_key,
        )

    async def set_allocation_status(self, allocation_id: str, status: str) -> None:
        await self._request(
            "PATCH", f"/v1/allocations/{segment(allocation_id)}/status", json={"status": status}
        )

    async def revoke_allocation(self, allocation_id: str) -> None:
        await self._request("POST", f"/v1/allocations/{segment(allocation_id)}/revoke")  # repeat-safe

    async def revoke_key(self, api_key_id: str) -> None:
        await self._request("POST", f"/v1/api-keys/{segment(api_key_id)}/revoke")

    async def read_allocation(self, allocation_id: str) -> Allowance:
        return allowance_from_body(await self._request("GET", f"/v1/allocations/{segment(allocation_id)}"))

    async def list_grants(self) -> list[GrantRecord]:
        return [grant_from_item(item) for item in await self._pages(_GRANTS, {})]

    async def grant_page(self, cursor: str | None) -> GrantPage:
        """One page, oldest first: a resumed grant create looks for its tag page by page (D57)."""
        params = {"limit": "1000"} | ({"cursor": cursor} if cursor else {})  # Batteries' largest page
        items, following = list_page(await self._request("GET", _GRANTS, params=params))
        return GrantPage(grants=tuple(grant_from_item(item) for item in items), next_cursor=following or None)

    async def create_grant(
        self, name: str, amount: Money, metadata: Mapping[str, str] | None = None
    ) -> GrantRecord:
        body = {"name": name, "status": "active", "sponsor": name, **_amount_fields(amount)}
        if metadata:  # Batteries takes string fields only: the object travels as JSON text
            body["metadata"] = json.dumps(dict(metadata), sort_keys=True)
        grant_id = _required(await self._request("POST", _GRANTS, json=body), "id")
        # Batteries answers the create with the id only; the record is the grant as it stored it. The grant
        # exists by now, so a failed read is an unknown outcome, never a refusal that frees the reference.
        try:
            return grant_from_item(await self._request("GET", f"/v1/grants/{segment(grant_id)}"))
        except ProviderError as exc:
            raise ProviderUnavailable(f"grant {grant_id} created but not read back: {exc}") from exc

    async def read_grant(self, grant_id: str) -> TenantGrant:
        """The grant, with what is left of it to allot (``tenant_grant``): two reads, the grant first."""
        grant = await self._request("GET", f"/v1/grants/{segment(grant_id)}")
        return tenant_grant(grant, (await self._allotted(grant_id)).get(grant_id, _NOTHING_HELD))

    async def grant_balances(self) -> list[TenantGrant]:
        """Every grant with its unallocated balance, oldest first."""
        grants = await self._pages(_GRANTS, {})
        allotted = await self._allotted(None)
        return [tenant_grant(grant, allotted.get(_required(grant, "id"), _NOTHING_HELD)) for grant in grants]

    async def set_grant_status(self, grant_id: str, status: str) -> None:
        await self._request("PATCH", f"/v1/grants/{segment(grant_id)}/status", json={"status": status})

    async def _allotted(self, grant_id: str | None) -> dict[str, tuple[Decimal, Decimal]]:
        """What the allocations of each grant hold and what they have spent, by grant; of one grant when
        it is named. One read of the allocations gives both (#166)."""
        held: dict[str, tuple[Decimal, Decimal]] = {}
        for item in await self._pages("/v1/allocations", {"grant_id": grant_id} if grant_id else {}):
            grant = _required(item, "grant_id")
            allocated, spent = held.get(grant, _NOTHING_HELD)
            held[grant] = (allocated + _amount(item, "allocated"), spent + _amount(item, "spent"))
        return held

    async def _pages(self, path: str, filters: dict[str, str]) -> list[dict[str, Any]]:
        """Every item of a list. A page that answers the cursor it was asked with is an answer the
        engine cannot read: asking again would never end."""
        items: list[dict[str, Any]] = []
        cursor = ""
        while True:
            params = {**filters, "limit": "1000"} | ({"cursor": cursor} if cursor else {})
            page, following = list_page(await self._request("GET", path, params=params))
            items += page
            if following == cursor and cursor:
                raise ProviderError(200, "Batteries answered a list whose cursor did not advance")
            if not following:
                return items
            cursor = following

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
        return json_object(response)


class BatteriesUsageSource:
    """UsageSource, and TenantUsageSource, over ``GET /v1/usage``. The cursor is opaque and bound to its
    filters."""

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
        return await self._page(self._filters, cursor, limit)

    async def tenant_page(self, tenant_id: str, cursor: str | None, limit: int = 1000) -> UsagePage:
        """One tenant's rows (``TenantUsageSource``, #166): Batteries' own ``grant_id`` filter, with the
        tenant's id as the grant's (D83). Batteries keeps a row when the payment session it was charged
        to belongs to an allocation of that grant; a row with no session is in the unfiltered list only."""
        return await self._page({"grant_id": tenant_id}, cursor, limit)

    async def _page(self, filters: dict[str, str], cursor: str | None, limit: int) -> UsagePage:
        params = {**filters, "limit": str(limit)}
        if cursor:
            params["cursor"] = cursor
        try:
            async with self._client.stream("GET", "/v1/usage", params=params) as response:
                try:
                    await response.aread()
                except httpx.DecodingError as exc:  # it answered: the status says what that was (#113, C7)
                    raise ProviderError(response.status_code, f"the body did not decode: {exc}") from exc
        except httpx.TransportError as exc:
            raise ProviderUnavailable(f"Batteries unreachable: {exc}") from exc
        message = error_message(response) if response.status_code >= 400 else ""
        if response.status_code == 400 and "cursor" in message:
            raise CursorMismatch(message)
        if response.status_code >= 400:
            raise ProviderError(response.status_code, message)
        items, next_cursor = list_page(json_object(response))  # which refuses a redirect (#113, C7)
        read = [read_usage_item(item) for item in items]
        return UsagePage(
            items=tuple(row for row in read if isinstance(row, UsageRow)),
            set_aside=tuple(row for row in read if isinstance(row, SetAsideUsage)),
            next_cursor=next_cursor,
        )


def json_object(response: httpx.Response) -> dict[str, Any]:
    """The one reader of every Batteries answer that is not an error status (#114, C21): a JSON object,
    or a ``ProviderError`` with the answer's own status. That is HTTP 502 to a caller and an unknown outcome
    to the operation ledger, never a refusal, so a reference stays held. The body is not quoted.

    Only a 2xx is an answer. A redirect is none, whatever its body: the engine follows no redirect, and a
    3xx that held a JSON object read as the call's result (Go's ``ServeMux`` answers a 301 for a path it
    would clean)."""
    if not response.is_success:
        raise ProviderError(response.status_code, "Batteries answered with a redirect, which is not followed")
    try:
        body = response.json()
    except (ValueError, RecursionError):  # not JSON, not UTF-8, or nested past the decoder's depth
        body = None
    if not isinstance(body, dict):
        raise ProviderError(response.status_code, "Batteries answered with a body that is not a JSON object")
    return body


def list_page(body: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    """A list answer's items and its cursor, ``""`` on the last page (#114, C21). ``items`` is a list of
    objects and ``next_cursor`` a string or absent; anything else is a ``ProviderError``. A malformed page
    never reads as an empty one, which would end a listing early, and one malformed item fails the page."""
    items, next_cursor = body.get("items"), body.get("next_cursor", "")
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise ProviderError(200, "Batteries answered a list whose items are not a list of objects")
    if not isinstance(next_cursor, str):
        raise ProviderError(200, "Batteries answered a list whose next_cursor is not a string")
    return items, next_cursor


def grant_from_item(item: dict[str, Any]) -> GrantRecord:
    """A grant as Batteries serves it, in a list or alone: ``id`` a non-empty string, and ``name``,
    ``status``, ``currency``, the total and ``metadata`` strings where present. Any other object is an
    answer the engine cannot read (#114, C21), never a grant with no tag: a resumed create would read
    "no such grant" and create a second one."""
    currency = _optional(item, "currency").lower()
    total_key = "total_usd" if currency == "usd" else "total_eth"
    return GrantRecord(
        id=_required(item, "id"),
        name=_optional(item, "name"),
        status=_optional(item, "status"),
        currency=currency.upper() or "USD",
        total=_optional(item, total_key),
        metadata=_metadata(_optional(item, "metadata")),
    )


def tenant_grant(item: dict[str, Any], held: tuple[Decimal, Decimal]) -> TenantGrant:
    """A grant as Batteries serves it, as the tenant whose id it is. Batteries serves a grant's total and
    no unallocated balance (v0.0.3, ``internal/store/manage.go``, ``listQuery``), so that is the total
    less ``allotted``, what the grant's allocations hold: each one's ``allocated`` amount, which a revoke
    lowers by what it returns. The grant is read first: an allotment made between the two reads is
    counted and a top-up is not yet, so the difference is held at zero, below which the provider never
    lets the balance go. A figure to show, never one the engine refuses by.

    ``held`` is what the grant's allocations hold and what they have spent. The second is the tenant's
    payment usage (#166): Batteries serves ``spent_<currency>`` on each allocation, a revoked one too."""
    currency: Currency = "USD" if _optional(item, "currency").lower() == "usd" else "ETH"
    total = _amount(item, "total")
    allotted, spent = held
    return TenantGrant(
        tenant_id=_required(item, "id"),
        name=_optional(item, "name"),
        status=_optional(item, "status"),
        total=Money(amount=total, currency=currency),
        unallocated=Money(amount=max(total - allotted, Decimal(0)), currency=currency),
        spent=Money(amount=spent, currency=currency),
    )


def _amount(item: dict[str, Any], name: str) -> Decimal:
    """``<name>_usd`` or ``<name>_eth``, by the item's currency: a decimal string, or an answer the
    engine cannot read (#114, C21), never a zero."""
    key = f"{name}_{_optional(item, 'currency').lower() or 'usd'}"
    value = item.get(key)
    amount = decimal_or_none(value) if isinstance(value, str) else None
    if amount is None or not amount.is_finite():
        raise ProviderError(200, f"Batteries answered with no usable {key}")
    return amount


def _required(body: dict[str, Any], key: str) -> str:
    """A value the answer has to carry: a non-empty string, as Batteries serves every id and a new key's
    secret. Anything else, a missing key included, is an answer the engine cannot read (#114, C21): a
    ``ProviderError`` that is no refusal and quotes no value."""
    value = body.get(key)
    if not isinstance(value, str) or not value:
        raise ProviderError(200, f"Batteries answered with no usable {key}")
    return value


def _optional(body: dict[str, Any], key: str) -> str:
    """A text the answer may leave out, ``""`` then. One that is no string is never turned into text."""
    value = body.get(key, "")
    if not isinstance(value, str):
        raise ProviderError(200, f"Batteries answered with a {key} that is not a string")
    return value


def _metadata(raw: str) -> dict[str, str]:
    """A grant's metadata when it is a JSON object of strings (the engine's tag); free text reads as {}."""
    try:
        parsed = json.loads(raw) if raw else None
    except ValueError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {str(key): value for key, value in parsed.items() if isinstance(value, str)}


def allowance_from_body(body: dict[str, Any]) -> Allowance:
    """Allocation reads carry allocated_/available_/spent_ suffixed by the allocation's currency."""
    currency: Currency = "USD" if _optional(body, "currency").lower() == "usd" else "ETH"
    suffix = currency.lower()
    return Allowance(
        allocation_id=_required(body, "id"),
        status=_optional(body, "status"),
        granted=_money(body.get(f"allocated_{suffix}"), currency),
        spent=_money(body.get(f"spent_{suffix}"), currency),
        available=_money(body.get(f"available_{suffix}"), currency),
    )


def segment(value: str) -> str:
    """An id as one path segment, whatever its source (#113, C10): every reserved character is
    percent-encoded, and so are the ids ``.`` and ``..`` (``%2E``, ``%2E%2E``), which httpx would remove
    as a dot segment. ``...`` is no dot segment and goes out as written."""
    return "%2E" * len(value) if value in (".", "..") else quote(value, safe="")


def error_message(response: httpx.Response) -> str:
    """The provider's own words for an error status: its ``error`` text, else the body as text."""
    try:
        body = response.json()
    except (ValueError, RecursionError):  # as in json_object; a number past the digit limit is one too
        return response.text
    if isinstance(body, dict) and isinstance(body.get("error"), str):
        return str(body["error"])
    return response.text


def _amount_fields(amount: Money) -> dict[str, str]:
    return {f"amount_{amount.currency.lower()}": format(amount.amount, "f")}


def _new_key(body: dict[str, Any]) -> NewAllocationKey:
    return NewAllocationKey(
        allocation_id=_required(body, "allocation_id"),
        api_key_id=_required(body, "id"),
        secret=SecretStr(_required(body, "api_key")),
    )


def _money(value: Any, currency: Currency) -> Money | None:
    amount = decimal_or_none(value)
    return None if amount is None else Money(amount=amount, currency=currency)
