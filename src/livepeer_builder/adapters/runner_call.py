"""The paid runner call, owned by the engine (D53).

Adapted from ``call_runner`` and its helpers in livepeer-python-gateway ``live_runner.py`` at ``2017a80``
(MIT License, Copyright (c) 2026 Livepeer Foundation). The engine sends the runner request and answers
its 402 itself; signing stays with the SDK's signer client (``get_signer_info``, ``LivePaymentSession``).
Owning the loop lets the transport report each payment session when cost attribution needs it, with no
SDK hooks:

- ``unknown`` before the signer is asked (``on_payment_state``), so a charge whose reply is lost,
  malformed or cancelled still has its manifest on the attempt;
- ``unpaid`` when the signer refused it;
- signed (``on_signed``, ``payment_sent``) once the payment is validated, before the paid request.

tests/unit/test_runner_call.py runs this against a loopback runner and signer, and fails when the pinned
SDK's ``call_runner`` or its helpers change, so every pin bump reviews this copy.
"""

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from decimal import Decimal
from typing import Any, Literal

import aiohttp
from livepeer_gateway.errors import (
    LivepeerGatewayError,
    LivepeerHTTPError,
    PaymentError,
    SignerRefreshRequired,
    SkipPaymentCycle,
)
from livepeer_gateway.remote_signer import (
    GetPaymentResponse,
    LivePaymentChallenge,
    LivePaymentSession,
    RemoteSignerError,
    get_signer_info,
)

from livepeer_builder.contracts import AttemptOutcome, NetworkRate
from livepeer_builder.errors import RunnerCallError
from livepeer_builder.protocols import OnSigned
from livepeer_builder.utils import BODY_LIMIT

PaymentState = Literal["unpaid", "unknown", "paid"]
OnPaymentState = Callable[[str, PaymentState], Awaitable[None]]
"""Called with ``(manifest_id, "unknown")`` before a session is signed, and ``"unpaid"`` when the signer
refused it. Raising from the ``"unknown"`` call stops the signing request (fail closed)."""

PAYMENT_HEADER = "Livepeer-Payment"
SEGMENT_HEADER = "Livepeer-Segment"
PAYER_ADDRESS_HEADER = "Livepeer-Payer-Address"
_PAYMENT_TYPES_BY_UNIT = {"hour": "live", "seconds": "live", "fixed": "fixed"}
# The signer refuses a ceiling below the price it is asked to pay; 1.2% covers most hourly ETH-USD drift.
_PRICE_PADDING = Decimal("1.012")
_PAYMENT_ERRORS = (PaymentError, SignerRefreshRequired, SkipPaymentCycle, RemoteSignerError)
# Signer statuses about the orchestrator's payment params, not the caller: another orchestrator may
# succeed, paid or not (D26, D52). 400 = incomplete or expired params; 481 = price ceiling. Caller- or
# signer-side refusals (402 allowance, 403 forbidden, 502 webhook, 503 outbox) are not here.
_ORCHESTRATOR_SPECIFIC_SIGNER_STATUSES = frozenset({400, 481})
# Signer answers that say it did not sign. A timeout, a lost or malformed reply, or a 5xx does not: the
# signer may have charged, so the manifest stays ``unknown``.
_SIGNER_REFUSALS = frozenset({400, 401, 402, 403, 481})


class PaymentEvents:
    """What happened to each payment session of one call.

    A confirmed session means the signer charged the allocation and counts as ``payment_sent``: the
    engine then fails over only to another orchestrator, and only when the failure did not run the work
    (D52). Each one reaches ``on_signed`` before the paid request goes out, so the engine stores the
    evidence first (D42). ``unknown`` is set when the signer was asked and gave no validated answer and
    no refusal: it may have charged, so the job must not pay another orchestrator.
    """

    def __init__(
        self, on_signed: OnSigned | None = None, on_payment_state: OnPaymentState | None = None
    ) -> None:
        self.signed = False
        self.unknown = False
        self.manifest_ids: list[str] = []
        self._on_signed = on_signed
        self._on_payment_state = on_payment_state

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(self.manifest_ids)

    async def starting(self, manifest_id: str) -> None:
        if self._on_payment_state is not None:
            await self._on_payment_state(manifest_id, "unknown")

    async def failed(self, manifest_id: str, error: Exception) -> None:
        refused = isinstance(error, (SignerRefreshRequired, SkipPaymentCycle)) or (
            isinstance(error, LivepeerHTTPError) and error.status_code in _SIGNER_REFUSALS
        )
        if not refused:
            self.unknown = True
        elif self._on_payment_state is not None:
            await self._on_payment_state(manifest_id, "unpaid")

    async def confirmed(self, manifest_id: str) -> None:
        self.signed = True
        self.manifest_ids.append(manifest_id)  # never empty: ``_challenge`` requires one
        if self._on_signed is not None:
            await self._on_signed(manifest_id)

    def error(
        self, kind: AttemptOutcome, message: str, *, status_code: int | None = None, body: str | None = None
    ) -> RunnerCallError:
        """A failed call, carrying what the signer signed for it so far."""
        return RunnerCallError(
            kind,
            message,
            payment_sent=self.signed,
            manifest_ids=self.ids,
            status_code=status_code,
            body=body,
            payment_unknown=self.unknown,
        )


class Signer:
    """The signer and the bearer it is called with. A call without one is unpaid."""

    def __init__(self, url: str, headers: dict[str, str]) -> None:
        self.url = url
        self.headers = headers

    async def payer_address(self) -> str:
        material = await get_signer_info(self.url, frozenset(self.headers.items()))  # cached by the SDK
        return str(material.address)


class PaidRequest:
    """One runner request. A 402 is paid through the signer and the request sent again, up to
    ``(retries + 1) * 2`` times, as ``call_runner`` does."""

    def __init__(
        self,
        url: str,
        *,
        method: str,
        payload: dict[str, Any],
        app: str,
        rate: NetworkRate | None,
        signer: Signer | None,
        retries: int,
        events: PaymentEvents,
    ) -> None:
        self.url = url
        self._method = method.upper()
        self._body = json.dumps(payload).encode()
        self._app = app
        self._signer = signer
        self._attempts = (max(0, retries) + 1) * 2
        self._events = events
        self._payment_type = payment_type(rate) if signer is not None else ""
        self._max_price = max_price(rate) if signer is not None else None

    async def respond(self, timeout_s: float) -> tuple[int, bytes, str]:
        """The runner's status, body and content type. Each request to the runner (the first, then each
        paid retry) is bounded by ``timeout_s``; signing has the SDK's own limits."""
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout_s)) as session:
            with translated(self._events):
                response, funding = await self._answered(session)
                try:
                    body = await response.read()
                finally:
                    await stop_funding(funding)  # the request ends here, so the funding ends with it
                    response.release()
        return response.status, body, response.content_type

    async def open(self, connect_timeout_s: float) -> "PaidStream":
        """The runner's answered response, unread. Only connecting is bounded; the caller bounds the rest."""
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=connect_timeout_s, sock_read=None)
        session = aiohttp.ClientSession(timeout=timeout)
        try:
            with translated(self._events):
                response, funding = await self._answered(session)
        except BaseException:
            await session.close()
            raise
        return PaidStream(session, response, funding, self._events)

    async def _answered(
        self, session: aiohttp.ClientSession
    ) -> tuple[aiohttp.ClientResponse, asyncio.Task[Any] | None]:
        """Send until the runner answers something other than 402. Metered funding starts with the paid
        request, so it covers the work from its start; the caller stops it."""
        headers = {"Accept": "*/*", "Content-Type": "application/json", "User-Agent": "livepeer-builder"}
        if self._signer is not None:
            headers[PAYER_ADDRESS_HEADER] = await self._signer.payer_address()
        challenge: LivePaymentChallenge | None = None
        for attempt in range(self._attempts):
            sent = dict(headers)
            payment: LivePaymentSession | None = None
            if challenge is not None and self._signer is not None:
                payment = self._payment_session(self._signer, challenge)
                try:
                    signed = await self._sign(payment)
                except SignerRefreshRequired:
                    if attempt + 1 >= self._attempts:
                        raise
                    challenge = None  # the payment params expired: ask the runner for a fresh challenge
                    continue
                sent[PAYMENT_HEADER] = signed.payment
                sent[SEGMENT_HEADER] = signed.seg_creds or ""
            funding = start_funding(payment) if self._payment_type == "live" else None
            try:
                response = await session.request(self._method, self.url, data=self._body, headers=sent)
            except BaseException:
                await stop_funding(funding)
                raise
            if response.status < 400:
                return response, funding
            await stop_funding(funding)
            text = (await response.read()).decode(errors="replace")
            response.release()
            if response.status != 402:
                kind: AttemptOutcome = "refused" if response.status == 503 else "http"
                message = f"HTTP {response.status} from {self.url}"
                raise self._events.error(
                    kind, message, status_code=response.status, body=text[:BODY_LIMIT] or None
                )
            if self._signer is None:
                raise self._events.error(
                    "http", f"{self.url} asks for payment; the call is unpaid", status_code=402
                )
            challenge = self._challenge(text)
        raise self._events.error("http", f"{self.url} kept asking for payment", status_code=402)

    async def _sign(self, payment: LivePaymentSession) -> GetPaymentResponse:
        manifest_id = payment.manifest_id
        try:
            await self._events.starting(manifest_id)  # before the signer request
        except Exception as error:  # fail closed: nothing is signed without the intent on record
            raise self._events.error("payment", f"could not record payment intent: {error}") from error
        try:
            signed = await payment.get_payment()
        except Exception as error:
            with contextlib.suppress(Exception):  # an unrecorded refusal leaves it unknown, which is safe
                await self._events.failed(manifest_id, error)
            raise
        except BaseException:  # cancelled while signing: the signer may have charged
            self._events.unknown = True
            raise
        await self._events.confirmed(manifest_id)
        return signed

    def _payment_session(self, signer: Signer, challenge: LivePaymentChallenge) -> LivePaymentSession:
        return LivePaymentSession(
            signer.url,
            signer_headers=signer.headers,
            type=self._payment_type,
            challenge=challenge,
            app=self._app,
            max_price=self._max_price,
        )

    def _challenge(self, body: str) -> LivePaymentChallenge:
        """The 402's payment contract: ``payment_params``, ``manifest_id`` and ``payment_url``."""
        with contextlib.suppress(json.JSONDecodeError):
            data = json.loads(body)
            if isinstance(data, dict):
                fields = {key: data.get(key) for key in ("payment_params", "manifest_id", "payment_url")}
                if all(isinstance(value, str) and value for value in fields.values()):
                    return LivePaymentChallenge(**fields)
        raise self._events.error("http", f"invalid payment challenge from {self.url}", status_code=402)


class PaidStream:
    """The runner's live response as a ``RunnerStream``. It owns the HTTP session and any metered
    funding; mid-stream transport failures become RunnerCallError."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        response: aiohttp.ClientResponse,
        funding: asyncio.Task[Any] | None,
        events: PaymentEvents,
    ) -> None:
        self._session = session
        self._response = response
        self._funding = funding
        self._events = events

    @property
    def status_code(self) -> int:
        return self._response.status

    @property
    def content_type(self) -> str:
        return self._response.content_type or "application/octet-stream"

    @property
    def manifest_ids(self) -> tuple[str, ...]:
        return self._events.ids

    @property
    def payment_sent(self) -> bool:
        return self._events.signed

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[bytes]:
        with translated(self._events, prefix="stream failed: "):
            async for chunk in self._response.content.iter_any():  # never empty: b"" ends it
                yield chunk

    async def aclose(self) -> None:
        """Stop funding first, so nothing is paid for a stream about to be dropped."""
        try:
            await stop_funding(self._funding)
        finally:
            self._funding = None
            self._response.close()
            await self._session.close()


@contextlib.contextmanager
def translated(events: PaymentEvents, prefix: str = "") -> Iterator[None]:
    """Signer and network failures as RunnerCallError, each with the payment evidence collected so far.
    The runner's own answers are already RunnerCallError. ``TimeoutError`` is an ``OSError``."""
    try:
        yield
    except LivepeerGatewayError as error:
        raise signer_error(error, events) from error
    except (aiohttp.ClientError, OSError) as error:
        raise events.error(network_kind(error), f"{prefix}{error}") from error


def signer_error(error: LivepeerGatewayError, events: PaymentEvents) -> RunnerCallError:
    """The SDK's signer client failed: an outcome for the attempt, with the evidence so far."""
    if isinstance(error, LivepeerHTTPError):
        orchestrator_specific = error.status_code in _ORCHESTRATOR_SPECIFIC_SIGNER_STATUSES
        kind: AttemptOutcome = "payment_refused" if orchestrator_specific else "payment"
        return events.error(kind, str(error), status_code=error.status_code)
    if isinstance(error, _PAYMENT_ERRORS):
        return events.error("payment", str(error))
    return events.error(network_kind(error), str(error))


def network_kind(error: BaseException) -> AttemptOutcome:
    current: BaseException | None = error
    while current is not None:
        if isinstance(current, TimeoutError):
            return "timeout"
        if isinstance(current, (ConnectionError, OSError, aiohttp.ClientConnectionError)):
            return "unreachable"
        current = current.__cause__
    return "http"


def payment_type(rate: NetworkRate | None) -> str:
    """``fixed`` pays once per request; ``live`` (metered) keeps paying while the work runs.

    A unit the engine cannot pay is about this runner's terms, not the job: ``payment_refused`` fails
    over to the next runner before anything is signed.
    """
    unit = rate.unit.strip().lower() if rate is not None else ""
    if not unit:
        return "live"
    if unit not in _PAYMENT_TYPES_BY_UNIT:
        expected = ", ".join(sorted(_PAYMENT_TYPES_BY_UNIT))
        message = f"unsupported runner price unit {unit!r}; expected {expected}"
        raise RunnerCallError("payment_refused", message, payment_sent=False)
    return _PAYMENT_TYPES_BY_UNIT[unit]


def max_price(rate: NetworkRate | None) -> dict[str, Any] | None:
    """The price ceiling the signer is given: the advertised price, padded for rate drift."""
    if rate is None:
        return None
    return {
        "price": float(rate.amount * _PRICE_PADDING),
        "currency": (rate.currency or "usd").strip().lower(),
        "unit": (rate.unit or "hour").strip().lower(),
    }


def start_funding(payment: LivePaymentSession | None) -> asyncio.Task[Any] | None:
    """Keep a metered session paid in the background (``run_payments``) until ``stop_funding``."""
    return None if payment is None else asyncio.create_task(payment.run_payments())


async def stop_funding(task: asyncio.Task[Any] | None) -> None:
    """Cancel the funding and wait for it to end. Only the funding's own cancellation is absorbed: a
    cancel of the caller while it waits still propagates."""
    if task is None:
        return
    task.cancel()
    await asyncio.wait([task])
    if not task.cancelled():
        task.exception()  # retrieved, so asyncio does not warn about it


def json_object(body: bytes, content_type: str) -> dict[str, Any] | None:
    """The reply parsed when it is a JSON object (``application/json`` or ``+json``); ``None`` otherwise.

    The runner's bytes are the result either way: a delivered reply is never failed for its shape.
    """
    mime = content_type.split(";", 1)[0].strip().lower()
    if not (mime.endswith("/json") or mime.endswith("+json")):
        return None
    try:
        data = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None
