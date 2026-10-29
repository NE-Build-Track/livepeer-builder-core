# Portions of this file are adapted from livepeer-python-gateway (src/livepeer_gateway/remote_signer.py,
# ``LivePaymentSession``, at 2017a80), https://github.com/livepeer/livepeer-python-gateway, under this
# license:
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

"""Metered funding on the engine's payment client (D58).

Adapted from livepeer-python-gateway ``LivePaymentSession`` at ``2017a80`` (MIT; the notice above): the
renewal and the loop that sends it (``run_payments``). No request here follows a redirect (#72).
"""

import asyncio
import json
import logging
from dataclasses import replace
from typing import Any, Literal
from urllib.parse import urlsplit

import aiohttp
import livepeer_gateway.remote_signer as remote_signer
from livepeer_gateway.errors import LivepeerHTTPError, PaymentError, SkipPaymentCycle
from livepeer_gateway.http import _raise_http_json_error
from livepeer_gateway.remote_signer import LivePaymentChallenge, LivePaymentSession, get_signer_info

_LOG = logging.getLogger(__name__)

PAYMENT_HEADER = "Livepeer-Payment"
SEGMENT_HEADER = "Livepeer-Segment"
# A renewal answered with one of these is tried again next cycle; any other 4xx will not change (the SDK's).
_RENEWAL_RETRIED_4XX = frozenset({408, 429})
_PAYMENT_TIMEOUT_S = 5.0  # each renewal or refresh, as the SDK's

# Why a metered session stopped being funded: the call ended, the runner released the session (404),
# the orchestrator refused the renewal (any other 4xx but 408 and 429), or it answered with a redirect.
FundingEnd = Literal["cancelled", "released", "refused", "redirected"]


class PaymentRedirected(PaymentError):  # type: ignore[misc]  # the SDK ships no types
    """A renewal or refresh was answered with a 3xx. It is never followed: it would replay the payment."""


class EnginePaymentSession(LivePaymentSession):  # type: ignore[misc]  # the SDK ships no types
    """The SDK's payment session, with renewals and refreshes sent by the engine (D58).

    The SDK's ``send_payment`` and ``_refresh_payment_params`` follow redirects, so a 3xx would carry
    ``Livepeer-Payment`` and ``Livepeer-Segment`` to wherever ``Location`` points, or replace the
    orchestrator's ticket params, recipient included, with another host's. Here both go out on one payment
    client per funding task, never follow a redirect and are never resent by aiohttp. Signing stays the
    SDK's (``get_payment``, which calls the refresh below): it goes to the operator's own signer.
    """

    # Set by the SDK's __init__ (fingerprinted in tests/unit/test_sdk_contract.py).
    _challenge: LivePaymentChallenge
    _signer_url: str | None
    _signer_headers: frozenset[tuple[str, str]] | None

    def __init__(self, signer_url: str | None, *, payment_origins: frozenset[str], **kwargs: Any) -> None:
        super().__init__(signer_url, **kwargs)
        # The origins the 402 challenge was checked against (PaidRequest._challenge): the runner's and its
        # orchestrator's. Every renewal and refresh must stay on them.
        self._payment_origins = payment_origins
        self._client: aiohttp.ClientSession | None = None

    async def send_payment(self) -> None:
        """One renewal: a fresh ticket from the signer, posted to the challenge's ``payment_url``."""
        if not self._signer_url:
            return
        self._check_origin(self._challenge.payment_url)  # before a ticket is minted for it
        payment = await self.get_payment()
        headers = {PAYMENT_HEADER: payment.payment, SEGMENT_HEADER: payment.seg_creds or ""}
        await self._post(self._challenge.payment_url, headers=headers)

    async def _refresh_payment_params(self) -> None:
        """Fresh ticket params from the payment URL's origin, after the signer answered 480."""
        signer = await get_signer_info(self._signer_url or "", self._signer_headers)
        if not signer.address:
            raise PaymentError("Cannot refresh payment without signer address")
        parts = urlsplit(self._challenge.payment_url)
        url = f"{parts.scheme}://{parts.netloc}/refresh-payment"
        payload = {"sender": signer.address, "manifest_id": self._challenge.manifest_id}
        body = await self._post(url, headers={"Content-Type": "application/json"}, data=json.dumps(payload))
        try:
            data = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            data = None
        payment_params = data.get("payment_params") if isinstance(data, dict) else None
        if not isinstance(payment_params, str) or not payment_params:
            raise PaymentError(
                f"RefreshPayment error: missing/invalid 'payment_params' in response (url={url})"
            )
        # The scoped payment_url stays the challenge's; only the ticket params rotate.
        self._challenge = replace(self._challenge, payment_params=payment_params)

    async def aclose(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.close()

    def _check_origin(self, url: str) -> None:
        if origin(url) not in self._payment_origins:
            raise PaymentError(f"{url} is not a payment origin of this challenge")

    async def _post(self, url: str, *, headers: dict[str, str], data: str | None = None) -> bytes:
        self._check_origin(url)
        if self._client is None:
            self._client = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=_PAYMENT_TIMEOUT_S))
            self._client._retry_connection = False  # a renewal carries a ticket: it goes out once
        sent = {"Accept": "application/json", "User-Agent": "livepeer-builder", **headers}
        async with self._client.post(
            url, data=data, headers=sent, allow_redirects=False, skip_auto_headers={"Content-Type"}
        ) as response:
            body = await response.read()
        if 300 <= response.status < 400:
            raise PaymentRedirected(f"HTTP {response.status} from {url}: payment redirects are not followed")
        if response.status >= 400:
            _raise_http_json_error(response.status, url, body.decode(errors="replace"))
        return body


def origin(url: str) -> str | None:
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


async def fund(session: EnginePaymentSession, *, interval_s: float | None = None) -> FundingEnd:
    """Keep a metered session funded until cancelled or the orchestrator stops it (``run_payments``).

    The SDK's cadence and status rules: the first renewal waits one interval, since the paid request
    carried a payment; a 4xx other than 408 and 429 stops, 404 meaning the runner released the session;
    anything else is tried again next cycle. A redirect stops too: it will not change on a retry, and no
    further ticket is minted. ``interval_s`` defaults to the SDK's ``PAYMENT_INTERVAL_S``, read each cycle as
    the SDK does. The payment client is closed however the funding ends.
    """
    try:
        while True:
            interval = remote_signer.PAYMENT_INTERVAL_S if interval_s is None else interval_s
            await asyncio.sleep(interval)
            try:
                await session.send_payment()
            except SkipPaymentCycle:
                _LOG.debug(
                    "renewal skipped",
                    extra={"event": "runner.renewal_skipped", "manifest_id": session.manifest_id},
                )
            except PaymentRedirected:
                return "redirected"
            except Exception as error:
                status = error.status_code if isinstance(error, LivepeerHTTPError) else None
                if status is not None and 400 <= status < 500 and status not in _RENEWAL_RETRIED_4XX:
                    return "released" if status == 404 else "refused"
                _LOG.warning(
                    "renewal failed; retrying next cycle",
                    extra={
                        "event": "runner.renewal_failed",
                        "error_type": type(error).__name__,
                        "status_code": status,
                        "manifest_id": session.manifest_id,
                    },
                )
    except asyncio.CancelledError:
        return "cancelled"
    finally:
        await session.aclose()
