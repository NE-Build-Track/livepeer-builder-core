"""Python gateway adapters: runner discovery and the Live Runner transport.

Discovery reads livepeer-node's ``/discovery`` inventory through the SDK's ``discover_runners``. The paid
runner call is the engine's own (``adapters/runner_call.py``, D53): it signs through the SDK's signer
client and reports each payment session before the signer is asked, so cost attribution never depends on
a callback that a lost signer reply would skip (docs/references/sdk-gateway.md).
"""

import asyncio
import json
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from livepeer_gateway.discovery import discover_runners

from livepeer_builder.adapters.runner_call import (
    PaidRequest,
    PaymentEvents,
    SdkRunnerStream,
    json_object,
)
from livepeer_builder.adapters.signer_failures import signer_not_reached
from livepeer_builder.contracts import (
    JobRequest,
    NetworkRate,
    Runner,
    RunnerGPU,
    RunnerReply,
)
from livepeer_builder.protocols import OnSigned


class DiscoverRunners(Protocol):
    """The SDK's ``discover_runners``, narrowed to the arguments this adapter passes."""

    async def __call__(
        self,
        *,
        discovery_url: str | None,
    ) -> list[dict[str, Any]]: ...


class HttpDiscoverySource:
    """Reads livepeer-node's ``/discovery`` inventory from ``LPB_DISCOVERY_URL``: the discovery service
    (``lpb discovery``, docs/contracts/discovery.md), livepeer-node's own, or one orchestrator. Sends no
    credentials; the signer is not involved in discovery (#45)."""

    def __init__(self, discovery_url: str, *, discover: DiscoverRunners = discover_runners) -> None:
        self._discovery_url = discovery_url
        self._discover = discover

    async def fetch(self) -> tuple[Runner, ...]:
        entries = await self._discover(discovery_url=self._discovery_url)
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

        Waiting for the runner's response headers is bounded by ``first_byte_timeout_s`` (D35). A deadline
        during signer identity is ``signer_unavailable`` (nothing was signed, D58). A later timeout keeps
        the sessions signed so far for cost sync; if it hits before the signer's payment is validated,
        ``payment_sent`` is ``False`` and the manifest stays ``unknown``.
        """
        events = PaymentEvents(on_signed)
        paid = self._request(runner, request, credential, events)
        try:
            async with asyncio.timeout(request.first_byte_timeout_s):
                return await paid.open(connect_timeout_s=request.timeout_s)
        except TimeoutError as error:  # the deadline; the call's own timeouts are already RunnerCallError
            if not paid.runner_contacted:
                # Still in /sign-orchestrator-info: nothing was signed and no runner was asked (D58).
                # A runner timeout would burn the operation reference (D57).
                raise events.error("payment", signer_not_reached(), reason="signer_unavailable") from error
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
    return Runner(
        url=url,
        app=app,
        runner_id=_text(raw.get("runner_id")),
        mode=_text(raw.get("mode")),
        orchestrator_url=orchestrator_url,
        capacity=capacity,
        capacity_available=available,
        rate=_rate(raw.get("price_info")),
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


def _rate(value: object) -> NetworkRate | None:
    if not isinstance(value, dict):
        return None
    try:
        amount = Decimal(str(value.get("price")))
    except InvalidOperation:
        return None
    if not amount.is_finite() or amount < 0:
        return None
    return NetworkRate(
        amount=amount,
        currency=str(value.get("currency") or "usd").lower(),
        unit=str(value.get("unit") or "hour").lower(),
    )


def _text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None
