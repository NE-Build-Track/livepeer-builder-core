"""The paid runner call, owned by the engine (D53).

Adapted from ``call_runner`` and its helpers in livepeer-python-gateway ``live_runner.py`` at ``2017a80``
(MIT License, Copyright (c) 2026 Livepeer Foundation). The engine sends the runner request and answers
its 402 itself; signing stays with the SDK's signer client (``get_signer_info``, ``LivePaymentSession``).
Owning the loop lets the transport report each payment session when cost attribution needs it, with no
SDK hooks:

- ``remember`` before ``get_payment``, using ``LivePaymentSession.manifest_id``, in memory only:
  a lost reply, a malformed reply or a cancel does not insert a session row;
- ``on_signed`` once ``get_payment`` returns, before the paid request, which writes the session row.

tests/unit/test_runner_call.py runs this against a loopback runner and signer, and fails when the pinned
SDK's ``call_runner`` or its helpers change, so every pin bump reviews this copy.
"""

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator, Iterator
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlsplit

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

_LOG = logging.getLogger(__name__)

PAYMENT_HEADER = "Livepeer-Payment"
SEGMENT_HEADER = "Livepeer-Segment"
PAYER_ADDRESS_HEADER = "Livepeer-Payer-Address"
_PAYMENT_TYPES_BY_UNIT = {"hour": "live", "seconds": "live", "fixed": "fixed"}
# The signer refuses a ceiling below the price it is asked to pay; 1.2% covers most hourly ETH-USD drift.
_PRICE_PADDING = Decimal("1.012")
_PAYMENT_ERRORS = (PaymentError, SkipPaymentCycle, RemoteSignerError)
# Signer statuses about the orchestrator's payment params, not the caller: another orchestrator may
# succeed, paid or not (D26, D52). 400 = incomplete or expired params; 481 = price ceiling. Caller- or
# signer-side refusals (402 allowance, 403 forbidden, 502 webhook, 503 outbox) are not here.
_ORCHESTRATOR_SPECIFIC_SIGNER_STATUSES = frozenset({400, 481})
# Signer answers that say it did not sign. A timeout, a lost or malformed reply, or a 5xx does not: the
# signer may have charged, so the manifest stays ``unknown``.
_SIGNER_REFUSALS = frozenset({400, 401, 402, 403, 481})


@runtime_checkable
class RemembersManifest(Protocol):
    """An ``on_signed`` callback that also keeps a manifest before the signer is asked."""

    async def remember(self, manifest_id: str) -> None: ...


class PaymentEvents:
    """What happened to each payment session of one call.

    A session ``get_payment`` validated means the signer charged the allocation and counts as
    ``payment_sent``: the engine then fails over only to another orchestrator, and only when the failure
    did not run the work (D52). Each one reaches ``on_signed`` before the paid request goes out, so the
    engine stores the evidence first (D42). ``unknown`` is set when the signer was asked and gave no
    validated answer and no refusal: it may have charged, so the job must not pay another orchestrator.
    That charge is not a session row, because ``on_signed`` did not run.
    """

    def __init__(self, on_signed: OnSigned | None = None) -> None:
        self.signed = False
        self.unknown = False
        self.manifest_ids: list[str] = []
        self._on_signed = on_signed

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(self.manifest_ids)

    async def remember(self, manifest_id: str) -> None:
        """Before ``get_payment``. Only a recorder that implements ``remember`` is told."""
        callback = self._on_signed
        if isinstance(callback, RemembersManifest):
            await callback.remember(manifest_id)

    async def confirmed(self, manifest_id: str) -> None:
        """``get_payment`` returned: this manifest is a validated payment."""
        self.signed = True
        self.manifest_ids.append(manifest_id)
        if self._on_signed is not None:
            await self._on_signed(manifest_id)

    def note_failure(self, error: BaseException) -> None:
        """A non-refusal may have charged. A signer refusal, including a refresh, did not validate."""
        refused = isinstance(error, (SignerRefreshRequired, SkipPaymentCycle)) or (
            isinstance(error, LivepeerHTTPError) and error.status_code in _SIGNER_REFUSALS
        )
        if not refused:
            self.unknown = True

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
        signer_url: str | None,
        signer_headers: dict[str, str] | None,
        retries: int,
        events: PaymentEvents,
        orchestrator_url: str | None = None,
    ) -> None:
        self.url = url
        # A challenge may only send renewals and refreshes to the runner's own host or its orchestrator's.
        origins = {_origin(url), _origin(orchestrator_url) if orchestrator_url else None} - {None}
        if _origin(url) is None:  # refused before any request, unpaid
            raise RunnerCallError("http", f"invalid runner URL {url!r}", payment_sent=False)
        self._payment_origins = origins
        self._method = method.upper()
        self._body = json.dumps(payload).encode()
        self._app = app
        self._signer_url = signer_url
        self._signer_headers = signer_headers or {}
        self._attempts = (max(0, retries) + 1) * 2
        self._events = events
        self._payment_type = payment_type(rate) if signer_url is not None else ""
        self._max_price = max_price(rate) if signer_url is not None else None

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
        if self._signer_url is not None:
            material = await get_signer_info(self._signer_url, frozenset(self._signer_headers.items()))
            headers[PAYER_ADDRESS_HEADER] = str(material.address)
        challenge: LivePaymentChallenge | None = None
        for attempt in range(self._attempts):
            sent = dict(headers)
            payment: LivePaymentSession | None = None
            if challenge is not None and self._signer_url is not None:
                payment = self._payment_session(challenge)
                try:
                    signed = await self._sign(payment)
                except SignerRefreshRequired as error:
                    _LOG.info(
                        "payment challenge needs refresh",
                        extra={
                            "event": "runner.signer_refresh",
                            "error_type": type(error).__name__,
                            "manifest_id": payment.manifest_id,
                            "paid": self._events.signed,
                        },
                    )
                    if attempt + 1 >= self._attempts:
                        raise
                    challenge = None  # the payment params expired: ask the runner for a fresh challenge
                    continue
                sent[PAYMENT_HEADER] = signed.payment
                sent[SEGMENT_HEADER] = signed.seg_creds or ""
            funding = start_funding(payment) if self._payment_type == "live" else None
            try:
                # A redirect would replay the payment headers to another host, and its answer is not
                # the runner's: a 3xx is a runner error like any other.
                response = await session.request(
                    self._method, self.url, data=self._body, headers=sent, allow_redirects=False
                )
            except BaseException:
                await stop_funding(funding)
                raise
            if response.status < 300:
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
            if self._signer_url is None:
                raise self._events.error(
                    "http", f"{self.url} asks for payment; the call is unpaid", status_code=402
                )
            challenge = self._challenge(text)
        raise self._events.error("http", f"{self.url} kept asking for payment", status_code=402)

    async def _sign(self, session: LivePaymentSession) -> GetPaymentResponse:
        """``session.manifest_id`` is the challenge id, known before ``get_payment`` returns."""
        manifest_id = session.manifest_id
        await self._events.remember(manifest_id)
        try:
            signed = await session.get_payment()
        except SignerRefreshRequired:
            raise
        except Exception as error:
            self._events.note_failure(error)
            self._log_signer_error(manifest_id, error)
            raise
        except BaseException as error:  # cancelled while signing: the signer may have charged
            self._events.unknown = True
            self._log_signer_error(manifest_id, error)
            raise
        await self._events.confirmed(manifest_id)
        return signed

    def _log_signer_error(self, manifest_id: str, error: BaseException) -> None:
        """The signer did not return a validated payment. ``paid`` is this call's validated payments."""
        _LOG.warning(
            "signer error",
            extra={
                "event": "runner.signer_error",
                "error_type": type(error).__name__,
                "manifest_id": manifest_id,
                "paid": self._events.signed,
            },
        )

    def _payment_session(self, challenge: LivePaymentChallenge) -> LivePaymentSession:
        return LivePaymentSession(
            self._signer_url,
            signer_headers=self._signer_headers,
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
                valid = all(isinstance(value, str) and value for value in fields.values())
                if valid and _origin(str(fields["payment_url"])) in self._payment_origins:  # None never is
                    return LivePaymentChallenge(**fields)
        # Malformed, or its payment_url names another host: refused before anything is signed.
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


def _origin(url: str) -> str | None:
    """``scheme://host:port`` of an absolute ``http``/``https`` URL, lower-cased, with the default port
    filled in; ``None`` when the URL is relative, has another scheme, or does not parse (a bad port, a
    broken IPv6 host)."""
    try:
        parts = urlsplit(url)
        scheme, host, port = parts.scheme.lower(), parts.hostname, parts.port
    except (ValueError, UnicodeError):
        return None
    if scheme not in ("http", "https") or not host:
        return None
    return f"{scheme}://{host.lower()}:{port or (443 if scheme == 'https' else 80)}"


def signer_error(error: LivepeerGatewayError, events: PaymentEvents) -> RunnerCallError:
    """The SDK's signer client failed: an outcome for the attempt, with the evidence so far.

    A refresh that escapes the retry budget is this orchestrator's expired params, like a 400: unpaid
    ``payment_refused``, so the job fails over. The in-loop refresh never reaches here.
    """
    if isinstance(error, SignerRefreshRequired):
        return events.error("payment_refused", str(error))
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
