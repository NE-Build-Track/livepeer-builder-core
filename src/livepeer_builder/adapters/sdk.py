"""Python gateway adapters: runner discovery and the Live Runner transport.

Discovery reads a ``/discovery`` inventory itself, not through the SDK's ``discover_runners``, which
parses ``price_usd`` as a float (livepeer/livepeer-python-gateway#88). The paid runner call is the
engine's own (``adapters/runner_call.py``, D53): it signs through the SDK's signer client and reports
each payment session before the signer is asked, so cost attribution never depends on a callback that
a lost signer reply would skip (docs/references/sdk-gateway.md)."""

import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from livepeer_builder.adapters.runner_call import (
    PaidRequest,
    PaymentEvents,
    SdkRunnerStream,
    json_object,
)
from livepeer_builder.contracts import (
    JobRequest,
    NetworkRate,
    Runner,
    RunnerGPU,
    RunnerReply,
)
from livepeer_builder.contracts.discovery import PRICE_LIMIT
from livepeer_builder.protocols import OnSigned
from livepeer_builder.utils import loads_json_numbers


class HttpDiscoverySource:
    """Reads a ``/discovery`` inventory from ``LPB_DISCOVERY_URL``: the discovery service
    (``lpb discovery``, docs/contracts/discovery.md), livepeer-node's own, or one orchestrator. Sends no
    credentials; the signer is not involved in discovery (#45). ``price_usd`` stays a ``Decimal``."""

    def __init__(self, discovery_url: str, *, client: httpx.AsyncClient | None = None) -> None:
        self._discovery_url = discovery_url
        self._client = client

    async def fetch(self) -> tuple[Runner, ...]:
        client = self._client or httpx.AsyncClient(timeout=5.0, follow_redirects=False)
        try:
            response = await client.get(self._discovery_url, headers={"Accept": "application/json"})
            response.raise_for_status()
            entries = loads_json_numbers(response.content)
        finally:
            if self._client is None:
                await client.aclose()
        if not isinstance(entries, list):
            raise ValueError("discovery response is not a JSON list")
        return tuple(runners_from_entries(entries))


class SdkRunnerTransport:
    """The Live Runner call with the actor's credential as the signer bearer. Unpaid when there is none.

    ``on_signed`` gets each session the signer signed, before the paid request goes out (D42). A recorder
    that implements ``remember`` is told the manifest, from ``LivePaymentSession.manifest_id``, before
    ``get_payment`` (D53).
    """

    def __init__(self, signer_url: str | None, max_payment_challenge_retries: int = 3) -> None:
        self._signer_url = signer_url
        self._retries = max_payment_challenge_retries

    def _request(
        self, runner: Runner, request: JobRequest, credential: str | None, events: PaymentEvents
    ) -> PaidRequest:
        signer_url = self._signer_url if credential is not None else None
        return PaidRequest(
            runner.url.rstrip("/") + request.path,
            method=request.method,
            # Runners expect a JSON object; an empty body for ``None`` is not the contract yet (T15).
            body=request.raw_body
            if request.raw_body is not None
            else json.dumps({} if request.payload is None else request.payload).encode(),
            content_type=request.content_type or "application/json",
            headers=request.headers,
            return_http_errors=request.return_http_errors,
            app=runner.app,
            rate=runner.rate,
            signer_url=signer_url,
            signer_headers={"Authorization": f"Bearer {credential}"} if signer_url is not None else None,
            retries=self._retries,
            events=events,
            orchestrator_url=runner.orchestrator_url,
        )

    async def call(
        self,
        runner: Runner,
        request: JobRequest,
        credential: str | None,
        on_signed: OnSigned | None = None,
    ) -> RunnerReply:
        events = PaymentEvents(on_signed)
        paid = self._request(runner, request, credential, events)
        status, body, content_type = await paid.respond(request.timeout_s)
        return RunnerReply(
            status_code=status,
            content=body,
            content_type=content_type or "application/json",
            data=json_object(body, content_type),
            manifest_ids=events.ids,
            payment_sent=events.signed,
        )

    async def open(
        self,
        runner: Runner,
        request: JobRequest,
        credential: str | None,
        on_signed: OnSigned | None = None,
    ) -> SdkRunnerStream:
        """The paid request, answered: the 402 is paid before the first byte, so payment is known here.

        Waiting for the runner's response headers is bounded by ``first_byte_timeout_s`` (D35). A timeout
        keeps the sessions signed so far for cost sync; if it hits before the signer's payment is
        validated, ``payment_sent`` is ``False`` and the manifest stays ``unknown``.
        """
        events = PaymentEvents(on_signed)
        paid = self._request(runner, request, credential, events)
        try:
            async with asyncio.timeout(request.first_byte_timeout_s):
                return await paid.open(connect_timeout_s=request.timeout_s)
        except TimeoutError as error:  # the deadline; the call's own timeouts are already RunnerCallError
            message = f"no response from {paid.url} within {request.first_byte_timeout_s:g}s"
            raise events.error("timeout", message) from error


def runners_from_entries(entries: list[dict[str, Any]]) -> list[Runner]:
    runners: list[Runner] = []
    for entry in entries:
        orchestrator_url = _text(entry.get("address"))
        for raw in entry.get("runners") or ():
            runner = _runner_from_raw(raw, orchestrator_url)
            if runner is not None:
                runners.append(runner)
    return runners


def _runner_from_raw(raw: object, orchestrator_url: str | None) -> Runner | None:
    if not isinstance(raw, dict):
        return None
    url, app = _text(raw.get("url")), _text(raw.get("app"))
    if not url or not app:
        return None
    capacity = raw.get("capacity") if isinstance(raw.get("capacity"), int) else None
    sessions = raw.get("session_ids")
    available = capacity - len(sessions) if capacity is not None and isinstance(sessions, list) else None
    reported = raw.get("capacity_available")
    if isinstance(reported, int) and not isinstance(reported, bool):
        available = reported  # the node reports it directly; session_ids is the older form
    rate, rate_error = _rate(raw.get("price_info"))
    return Runner(
        url=url,
        app=app,
        runner_id=_text(raw.get("runner_id")),
        mode=_text(raw.get("mode")),
        orchestrator_url=orchestrator_url,
        capacity=capacity,
        capacity_available=available,
        rate=rate,
        rate_error=rate_error,
        observed_at=_stamp(raw.get("observed_at")),
        version=_text(raw.get("version")),
        metadata=_text(raw.get("metadata")),
        gpu=_gpu(raw.get("gpu")),
    )


def _gpu(value: object) -> RunnerGPU | None:
    if not isinstance(value, dict):
        return None
    vram = value.get("vram_mb")
    gpu = RunnerGPU(
        id=_text(value.get("id")),
        name=_text(value.get("name")),
        vram_mb=vram if isinstance(vram, int) and vram > 0 else None,
    )
    return gpu if (gpu.id or gpu.name or gpu.vram_mb) else None


def _rate(value: object) -> tuple[NetworkRate | None, str | None]:
    """The rate exactly as advertised, or why the advertised ``price_info`` is not one. No defaults."""
    if value is None:
        return None, None  # off-chain: livepeer-node advertises no price
    if not isinstance(value, dict):
        return None, "price_info is not an object"
    amount = _decimal(value.get("price"))
    if amount is None:
        return None, "price is not a nonnegative number"
    currency = _text(value.get("currency"))
    if currency is None:
        return None, "price_info has no currency"
    unit = _text(value.get("unit"))
    if unit is None:
        return None, "price_info has no unit"
    price_usd = _decimal(value.get("price_usd"))
    error = None
    if price_usd is None and value.get("price_usd") is not None:
        error = "price_usd is not a nonnegative number"
    return NetworkRate(
        amount=amount,
        currency=currency.lower(),
        unit=unit.lower(),
        price_usd=price_usd,
    ), error


def _stamp(value: object) -> datetime | None:
    """An RFC 3339 time in UTC (Go's nanoseconds truncate to microseconds), or None. A time with no offset is
    not RFC 3339."""
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        return None
    return stamp.astimezone(UTC) if stamp.tzinfo is not None else None


def _decimal(value: object) -> Decimal | None:
    """A finite, nonnegative decimal below ``PRICE_LIMIT``, or None."""
    try:
        amount = value if isinstance(value, Decimal) else Decimal(str(value))
    except InvalidOperation:
        return None
    return amount if amount.is_finite() and 0 <= amount < PRICE_LIMIT else None


def _text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None
