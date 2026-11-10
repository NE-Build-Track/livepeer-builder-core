"""Python gateway adapters: runner discovery and the Live Runner transport.

Discovery reads a ``/discovery`` inventory itself, not through the SDK's ``discover_runners``, which
parses ``price_usd`` as a float (livepeer/livepeer-python-gateway#88). The paid runner call is the
engine's own (``adapters/runner_call.py``, D53): it signs through the SDK's signer client and reports
each payment session before the signer is asked, so cost attribution never depends on a callback that
a lost signer reply would skip (docs/references/sdk-gateway.md)."""

import asyncio
import contextlib
import json

import httpx

from livepeer_builder.adapters.discovery_inventory import read_inventory
from livepeer_builder.adapters.runner_call import (
    PaidRequest,
    PaymentEvents,
    SdkRunnerStream,
    json_object,
)
from livepeer_builder.contracts import JobRequest, Runner, RunnerReply
from livepeer_builder.contracts.discovery import payable_rate
from livepeer_builder.protocols import OnSigned


def memory_discovery_client(body: bytes) -> httpx.AsyncClient:
    """An in-memory client that answers every GET with ``body``. Used by ``FakeDiscoveryService``."""

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, content=body)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class HttpDiscoverySource:
    """Reads ``GET LPB_DISCOVERY_URL`` as livepeer/node's ``/discovery`` contract (D63). Sends no
    credentials; the signer is not involved in discovery (#45).

    The whole request, body included, gets ``timeout_s`` (5 s). ``DiscoveryService`` refreshes serially,
    so a slow source would otherwise freeze the snapshot. A redirect is not followed, even by an injected
    client; it fails the refresh. There is no size cap (D63).
    """

    def __init__(
        self,
        discovery_url: str,
        *,
        client: httpx.AsyncClient | None = None,
        timeout_s: float = 5.0,
    ) -> None:
        self._discovery_url = discovery_url
        self._client = client or httpx.AsyncClient(
            timeout=timeout_s
        )  # one per source: polls reuse its connection
        self._owns_client = client is None
        self._timeout_s = timeout_s

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def fetch(self) -> tuple[Runner, ...]:
        try:
            # httpx's timeout bounds each phase; this bounds the request, so a slow body cannot hold a refresh
            async with asyncio.timeout(self._timeout_s):
                body = await self._body(self._client)
        except (TimeoutError, httpx.TimeoutException) as error:  # httpx's is no TimeoutError, and says ""
            message = f"no inventory from {self._discovery_url} within {self._timeout_s:g}s"
            raise TimeoutError(message) from error
        return read_inventory(body)

    async def _body(self, client: httpx.AsyncClient) -> bytes:
        request = client.build_request("GET", self._discovery_url, headers={"Accept": "application/json"})
        response = await client.send(request, follow_redirects=False)
        async with contextlib.aclosing(response):
            response.raise_for_status()
            return response.content


class SdkRunnerTransport:
    """The Live Runner call with the actor's credential as the signer bearer. Unpaid when there is none.

    ``on_signed`` gets each session the signer signed, before the paid request goes out (D42). A recorder
    that implements ``remember`` is told the manifest, from ``LivePaymentSession.manifest_id``, before
    ``get_payment`` (D53).
    """

    def __init__(self, signer_url: str | None, max_payment_challenge_retries: int = 3) -> None:
        self._signer_url = signer_url
        self._retries = max_payment_challenge_retries

    @property
    def pays(self) -> bool:
        """A credential pays the runner only through a signer; with none, every call is unpaid."""
        return self._signer_url is not None

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
            rate=payable_rate(runner.price_info),
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
            timings=events.timer.timings,
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
            if paid.identifying:  # the signer never answered: nothing signed, no runner asked (D58 (4))
                message = f"the signer did not identify itself within {request.first_byte_timeout_s:g}s"
                raise events.error("payment", message, reason="signer_unavailable") from error
            if events.unknown:  # cut off while signing: the signer is the one that did not answer (D58 (5))
                raise events.error(
                    "timeout", "the signer gave no answer while signing; it may have charged"
                ) from error
            message = f"no response from {paid.url} within {request.first_byte_timeout_s:g}s"
            raise events.error("timeout", message) from error
