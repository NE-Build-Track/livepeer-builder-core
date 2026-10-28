# Portions of this file are adapted from livepeer-python-gateway (src/livepeer_gateway/live_runner.py,
# ``call_runner``, at 2017a80), https://github.com/livepeer/livepeer-python-gateway, under this license:
#
# MIT License
#
# Copyright (c) 2026 Livepeer Foundation
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

"""The engine's paid runner call (D53).

Adapted from livepeer-python-gateway ``call_runner`` at ``2017a80`` (MIT; the notice above).
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
_PRICE_PADDING = Decimal("1.012")  # the signer refuses a ceiling below the price it is asked to pay
_PAYMENT_ERRORS = (PaymentError, SkipPaymentCycle, RemoteSignerError)
# 400 and 481 are this orchestrator's params (D26). 402, 403, 502 and 503 are the caller's or the signer's.
_ORCHESTRATOR_SPECIFIC_SIGNER_STATUSES = frozenset({400, 481})
# These say the signer did not sign. A timeout, a lost reply or a 5xx may have charged.
_SIGNER_REFUSALS = frozenset({400, 401, 402, 403, 481})


@runtime_checkable
class RemembersManifest(Protocol):
    async def remember(self, manifest_id: str) -> None: ...


@runtime_checkable
class RefusesManifest(Protocol):
    async def refused(self, manifest_id: str) -> None: ...


class PaymentEvents:
    """Sessions of one call. ``unknown`` is ``payment_unknown``: the signer was asked and may have charged."""

    def __init__(self, on_signed: OnSigned | None = None) -> None:
        self.signed = False
        self.unknown = False
        self.manifest_ids: list[str] = []
        self._on_signed = on_signed

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(self.manifest_ids)

    async def remember(self, manifest_id: str) -> None:
        """Before the signer is asked: the recorder stores the manifest as ``unknown`` (D54). Its failure
        propagates, and nothing is signed."""
        callback = self._on_signed
        if isinstance(callback, RemembersManifest):
            await callback.remember(manifest_id)

    async def confirmed(self, manifest_id: str) -> None:
        self.signed = True
        self.manifest_ids.append(manifest_id)
        if self._on_signed is not None:
            await self._on_signed(manifest_id)

    async def note_failure(self, manifest_id: str, error: Exception) -> None:
        """A refusal makes the stored manifest ``unpaid``. Anything else leaves it ``unknown``: the
        signer may have charged."""
        refused = isinstance(error, (SignerRefreshRequired, SkipPaymentCycle)) or (
            isinstance(error, LivepeerHTTPError) and error.status_code in _SIGNER_REFUSALS
        )
        if not refused:
            self.unknown = True
            return
        callback = self._on_signed
        if isinstance(callback, RefusesManifest):
            await callback.refused(manifest_id)

    def error(
        self, kind: AttemptOutcome, message: str, *, status_code: int | None = None, body: str | None = None
    ) -> RunnerCallError:
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
        if _origin(url) is None:  # unpaid, before any request
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
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout_s)) as session:
            with translated(self._events):
                response, funding = await self._answered(session)
                try:
                    body = await response.read()
                finally:
                    await stop_funding(funding)
                    response.release()
        return response.status, body, response.content_type

    async def open(self, connect_timeout_s: float) -> "SdkRunnerStream":
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=connect_timeout_s, sock_read=None)
        session = aiohttp.ClientSession(timeout=timeout)
        try:
            with translated(self._events):
                response, funding = await self._answered(session)
        except BaseException:
            await session.close()
            raise
        return SdkRunnerStream(session, response, funding, self._events)

    async def _answered(
        self, session: aiohttp.ClientSession
    ) -> tuple[aiohttp.ClientResponse, asyncio.Task[Any] | None]:
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
                    challenge = None  # expired params: ask the runner for a fresh challenge
                    continue
                sent[PAYMENT_HEADER] = signed.payment
                sent[SEGMENT_HEADER] = signed.seg_creds or ""
            funding = start_funding(payment) if self._payment_type == "live" else None
            try:
                # A redirect would replay the payment headers to another host.
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
        """``manifest_id`` is known before ``get_payment`` returns."""
        manifest_id = session.manifest_id
        try:
            await self._events.remember(manifest_id)
        except Exception as error:  # fail closed: nothing is signed without the intent on record (D54)
            raise self._events.error("payment", f"could not record payment intent: {error}") from error
        try:
            signed = await session.get_payment()
        except SignerRefreshRequired as error:
            await self._events.note_failure(manifest_id, error)
            raise
        except Exception as error:
            self._log_signer_error(manifest_id, error)  # before the write: a cancel there skips the rest
            await self._events.note_failure(manifest_id, error)
            raise
        except BaseException as error:  # cancelled while signing: the signer may have charged
            self._events.unknown = True
            self._log_signer_error(manifest_id, error)
            raise
        await self._events.confirmed(manifest_id)
        return signed

    def _log_signer_error(self, manifest_id: str, error: BaseException) -> None:
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
        with contextlib.suppress(json.JSONDecodeError):
            data = json.loads(body)
            if isinstance(data, dict):
                fields = {key: data.get(key) for key in ("payment_params", "manifest_id", "payment_url")}
                valid = all(isinstance(value, str) and value for value in fields.values())
                if valid and _origin(str(fields["payment_url"])) in self._payment_origins:
                    return LivePaymentChallenge(**fields)
        # Malformed, or payment_url names another host: nothing is signed.
        raise self._events.error("http", f"invalid payment challenge from {self.url}", status_code=402)


class SdkRunnerStream:
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
            async for chunk in self._response.content.iter_any():  # b"" ends the stream
                yield chunk

    async def aclose(self) -> None:
        try:
            await stop_funding(self._funding)
        finally:
            self._funding = None
            self._response.close()
            await self._session.close()


@contextlib.contextmanager
def translated(events: PaymentEvents, prefix: str = "") -> Iterator[None]:
    """Signer and network failures become ``RunnerCallError``. ``TimeoutError`` is an ``OSError``."""
    try:
        yield
    except LivepeerGatewayError as error:
        raise signer_error(error, events) from error
    except (aiohttp.ClientError, OSError) as error:
        raise events.error(network_kind(error), f"{prefix}{error}") from error


def _origin(url: str) -> str | None:
    """``scheme://host:port``, or ``None`` when the URL or its port is not one aiohttp would request.

    An empty port is the scheme default. A stated port counts only in ``1..65535``: ``0`` is not ``443``.
    """
    try:
        parts = urlsplit(url)
        scheme, host, port = parts.scheme.lower(), parts.hostname, parts.port
    except (ValueError, UnicodeError):
        return None
    if port is None:
        port = 443 if scheme == "https" else 80
    if scheme not in ("http", "https") or not host or not 1 <= port <= 65535:
        return None
    return f"{scheme}://{host.lower()}:{port}"


def signer_error(error: LivepeerGatewayError, events: PaymentEvents) -> RunnerCallError:
    """A refresh past the retry budget is unpaid ``payment_refused``, like a 400."""
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
    unit = rate.unit.strip().lower() if rate is not None else ""
    if not unit:
        return "live"
    if unit not in _PAYMENT_TYPES_BY_UNIT:
        expected = ", ".join(sorted(_PAYMENT_TYPES_BY_UNIT))
        message = f"unsupported runner price unit {unit!r}; expected {expected}"
        raise RunnerCallError("payment_refused", message, payment_sent=False)
    return _PAYMENT_TYPES_BY_UNIT[unit]


def max_price(rate: NetworkRate | None) -> dict[str, Any] | None:
    if rate is None:
        return None
    return {
        "price": float(rate.amount * _PRICE_PADDING),
        "currency": (rate.currency or "usd").strip().lower(),
        "unit": (rate.unit or "hour").strip().lower(),
    }


def start_funding(payment: LivePaymentSession | None) -> asyncio.Task[Any] | None:
    return None if payment is None else asyncio.create_task(payment.run_payments())


async def stop_funding(task: asyncio.Task[Any] | None) -> None:
    """The funding's own cancellation is absorbed. A cancel of the caller still propagates."""
    if task is None:
        return
    task.cancel()
    await asyncio.wait([task])
    if not task.cancelled():
        task.exception()  # retrieved, so asyncio does not warn about it


def json_object(body: bytes, content_type: str) -> dict[str, Any] | None:
    mime = content_type.split(";", 1)[0].strip().lower()
    if not (mime.endswith("/json") or mime.endswith("+json")):
        return None
    try:
        data = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None
