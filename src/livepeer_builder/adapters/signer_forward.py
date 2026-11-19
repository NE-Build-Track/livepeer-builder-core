"""One signer call, forwarded by a clearinghouse to its own signer (D87, #166).

A payment clearinghouse serves the signer's two paths (``http/signing.py``). Each call it accepts is
handed to its private signer here, with the actor's Batteries key as the bearer. This module is the wire
of that and nothing else: it does not know a payment key, an actor or an allocation.

- **The address** is the signer's, read as the gateway SDK reads it: the scheme and the host, any path
  dropped, ``https`` for a bare ``host:port``. One setting names the signer for the engine's own payments
  and for the calls it forwards, so both reach the same place.
- **The request** carries the body as it arrived, and four headers of the engine's own. Nothing of the
  caller's travels: the signer hands every header it receives to the Batteries webhook.
- **The answer** is the signer's status, body and content type. No other header comes back.
- **Once.** A call is sent one time: no retry, no redirect followed. Each call opens its own connection,
  as the SDK's does, so a failure to connect is known to be one: no request was written. It goes to the
  signer directly: no proxy is read from the environment, as the SDK's client reads none.
- **Inside the SDK's budget.** The SDK gives a signer call 5 seconds and does not retry. The whole call
  ends after ``DEADLINE_S``, and connecting after ``CONNECT_S``, which is shorter, so a signer that
  cannot be reached is told apart from one that took the request and did not answer.

Two failures, and the difference is money (``errors.py``): ``SignerNotReached`` signed nothing, and
``SignerOutcomeUnknown`` may have signed. When in doubt it is the second. Neither names the key.
"""

import asyncio
import ssl
from urllib.parse import urlsplit

import httpx

from livepeer_builder.contracts import SignerAnswer, SignerCall
from livepeer_builder.errors import SignerNotReached, SignerOutcomeUnknown

DEADLINE_S = 4.0  # the whole call: under the 5 s the gateway SDK gives a signer call
CONNECT_S = 2.0  # under the deadline, so a connection that was never made is known to be that
_JSON = "application/json"
# A request that was never written: nothing reached the signer, so nothing was signed.
_NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.UnsupportedProtocol)
_REDIRECTS = range(300, 400)


def signer_origin(signer_url: str) -> str:
    """The signer's address as ``livepeer_gateway.http._http_origin`` reads it at the pin: scheme and
    host, with a path, a query or a fragment dropped, and ``https`` for a bare ``host:port``
    (tests/unit/test_signer_forward.py holds the two to each other)."""
    given = signer_url.strip()
    parts = urlsplit(given if "://" in given else f"https://{given}")
    return f"{parts.scheme}://{parts.netloc}"


class HttpSignerForwarder:
    """``SignerForwarder`` over HTTP. ``transport`` replaces the network, for a test."""

    def __init__(
        self,
        signer_url: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        deadline_s: float = DEADLINE_S,
    ) -> None:
        self._origin = signer_origin(signer_url)
        self._transport = transport
        self._tls: ssl.SSLContext | None = None  # built at the first call, then kept: it reads the CA store
        self.deadline_s = deadline_s

    def __repr__(self) -> str:
        return f"HttpSignerForwarder({self._origin!r})"

    async def forward_signer_call(
        self, call: SignerCall, credential: str, body: bytes, content_type: str
    ) -> SignerAnswer:
        headers = {
            "Authorization": f"Bearer {credential}",
            "Content-Type": content_type or _JSON,
            "Accept": _JSON,
            "User-Agent": "livepeer-builder",
        }
        try:
            async with asyncio.timeout(self.deadline_s):
                response = await self._post(f"{self._origin}/{call}", body, headers)
        except _NEVER_SENT as error:
            # The error's own text is not repeated: it is the transport's, and may quote what was sent.
            raise SignerNotReached(f"no connection to the signer at {self._origin}") from error
        except (TimeoutError, httpx.HTTPError) as error:
            reason = type(error).__name__
            raise SignerOutcomeUnknown(
                f"no usable answer from the signer at {self._origin} ({reason})"
            ) from error
        if response.status_code in _REDIRECTS:
            raise SignerOutcomeUnknown(
                f"the signer at {self._origin} answered with a redirect ({response.status_code}), which is "
                "not followed"
            )
        return SignerAnswer(
            status_code=response.status_code,
            content=response.content,
            content_type=response.headers.get("content-type", ""),
        )

    async def _post(self, url: str, body: bytes, headers: dict[str, str]) -> httpx.Response:
        """One request on a connection of its own, closed when the answer is read."""
        timeout = httpx.Timeout(self.deadline_s, connect=min(CONNECT_S, self.deadline_s))
        if self._transport is None and self._tls is None:
            self._tls = httpx.create_ssl_context()
        # ``trust_env=False``: no proxy and no netrc from the environment, as the SDK's own client has none.
        async with httpx.AsyncClient(
            transport=self._transport,
            verify=self._tls or True,
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            return await client.post(url, content=body, headers=headers)
