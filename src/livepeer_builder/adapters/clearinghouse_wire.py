"""The wire to a payment clearinghouse: one request with the tenant admin key, and what its failure means
(D84, D87, #168).

An enterprise's engine that uses a payment clearinghouse reaches it here and nowhere else: the remote
adapters of the payment ports (``adapters/clearinghouse.py``) send every request through
``ClearinghouseWire``. It knows the clearinghouse's error answers (``http/app.py``) and nothing of the
routes.

A clearinghouse failure is classified like a signer failure (D87):

- **Not reached, or not ready** is ``ProviderUnavailable``: the connection was never made, or the
  clearinghouse turned the request away (429), or a read got no usable answer (a timeout, a dropped
  connection, a 503). Nothing was applied.
- **An unknown outcome** is ``ProviderOutcomeUnknown``: a write left and no answer says whether it was
  applied (a timeout, a dropped connection, a 5xx, a 408, or a 409 saying the same reference is still
  running there). The operation ledger keeps the reference, and the repeat carries the same
  ``operation_ref``, which the clearinghouse answers from its own ledger.
- **A refusal** is ``ProviderError`` with the clearinghouse's status and its own words: 400, 401, 403,
  404, 409, 422.
- **An answer the engine cannot read** (a 2xx that is not the expected shape, a redirect, a 5xx to a
  read) is a ``ProviderError`` with that status, which is no refusal: its body is not quoted.

No request is ever sent twice here, and no redirect is followed: the tenant admin key goes to the
clearinghouse's address only. The key is in no error text, log line or ``repr``.
"""

import asyncio
from collections.abc import Mapping
from typing import Any, Literal

import httpx
from pydantic import BaseModel, SecretStr, ValidationError

from livepeer_builder.errors import CursorMismatch, ProviderError, ProviderOutcomeUnknown, ProviderUnavailable
from livepeer_builder.utils import BODY_LIMIT

# A request that was never written: nothing reached the clearinghouse, so nothing was applied.
_NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.UnsupportedProtocol)
_REFUSALS = frozenset({400, 401, 403, 404, 409, 422})
_STILL_RUNNING = "operation_in_progress"  # a 409 of the clearinghouse's ledger: no refusal
_CURSOR_MISMATCH = "cursor_mismatch"  # a 400 of its usage route
_APPLIED_OR_NOT = (
    "it may have been applied. Repeat the same request, with the same operation_ref, to resume it"
)
Method = Literal["GET", "POST"]


class ClearinghouseWire:
    """Requests to one clearinghouse as one tenant's admin. ``tenant_id`` is that tenant, whose id is its
    grant's id (D83). A ``client`` passed in is borrowed and never closed."""

    def __init__(
        self,
        base_url: str,
        token: str,
        tenant_id: str,
        client: httpx.AsyncClient | None = None,
        *,
        timeout_s: float = 30.0,
    ) -> None:
        self.tenant_id = tenant_id
        self._base_url = base_url.rstrip("/")
        self._token = SecretStr(token)  # never a plain attribute: ``vars`` and ``repr`` show no key
        self._client = client or httpx.AsyncClient(timeout=timeout_s)
        self._owns_client = client is None
        self._timeout_s = timeout_s

    def __repr__(self) -> str:
        return f"ClearinghouseWire({self._base_url!r}, tenant_id={self.tenant_id!r})"

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def read(self, path: str, params: Mapping[str, str] | None = None) -> httpx.Response:
        """One ``GET``. It applies nothing, so its failure is never an unknown outcome."""
        return await self._send("GET", path, params=params)

    async def write(self, path: str, body: Mapping[str, Any] | None = None) -> httpx.Response:
        """One ``POST``, sent once. A failure after it left is ``ProviderOutcomeUnknown``."""
        return await self._send("POST", path, body=dict(body or {}))

    async def read_as[T: BaseModel](
        self, model: type[T], path: str, params: Mapping[str, str] | None = None
    ) -> T:
        return _parsed(model, await self.read(path, params))

    async def write_as[T: BaseModel](
        self, model: type[T], path: str, body: Mapping[str, Any] | None = None
    ) -> T:
        return _parsed(model, await self.write(path, body))

    async def _send(
        self,
        method: Method,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        body: dict[str, Any] | None = None,
    ) -> httpx.Response:
        request = self._client.build_request(
            method,
            self._base_url + path,
            params=params,
            json=body,
            headers={
                "Authorization": f"Bearer {self._token.get_secret_value()}",
                "Accept": "application/json",
            },
        )
        try:
            # One deadline for the whole call, as for Batteries: the ledger's stale window counts whole calls
            async with asyncio.timeout(self._timeout_s):
                response = await self._client.send(request, follow_redirects=False)
        except _NEVER_SENT as error:
            raise ProviderUnavailable("the clearinghouse could not be reached; nothing was sent") from _named(
                error
            )
        except (TimeoutError, httpx.TransportError) as error:
            raise _no_answer(method, f"the clearinghouse gave no answer within {self._timeout_s:g} s") from (
                _named(error)
            )
        if response.status_code >= 400:
            raise self._failed(method, response)
        if not response.is_success:  # a redirect is not followed: the key goes to this address only
            raise ProviderError(
                response.status_code, "the clearinghouse answered with a redirect, which is not followed"
            )
        return response

    def _failed(self, method: Method, response: httpx.Response) -> Exception:
        status = response.status_code
        message, code = _problem(response)
        message = message.replace(self._token.get_secret_value(), "[the tenant admin key]")[:BODY_LIMIT]
        if status == 400 and code == _CURSOR_MISMATCH:
            return CursorMismatch(message)
        if status == 409 and code == _STILL_RUNNING:
            return ProviderOutcomeUnknown(
                f"the clearinghouse is still running this command; {_APPLIED_OR_NOT}"
            )
        if status in _REFUSALS:
            return ProviderError(status, message)
        if status == 429:  # turned away before it was handled
            return ProviderUnavailable("the clearinghouse is not taking requests now (HTTP 429)")
        if method == "POST":  # a 5xx or a 408 to a command: it may have been applied on the way
            return ProviderOutcomeUnknown(f"the clearinghouse answered HTTP {status}; {_APPLIED_OR_NOT}")
        if status in (408, 503):
            return ProviderUnavailable(f"the clearinghouse is not ready (HTTP {status})")
        return ProviderError(status, message)


def _no_answer(method: Method, what: str) -> ProviderUnavailable:
    if method == "POST":
        return ProviderOutcomeUnknown(f"{what}; {_APPLIED_OR_NOT}")
    return ProviderUnavailable(what)


def _named(error: BaseException) -> Exception:
    """The cause to chain, as its type alone: httpx's own text and request can hold the address with its
    headers, and the chain is logged."""
    return (
        type(error)("clearinghouse request failed") if isinstance(error, httpx.HTTPError) else TimeoutError()
    )


def _problem(response: httpx.Response) -> tuple[str, str | None]:
    """An error answer's ``error`` text and its ``code``, as ``http/app.py`` writes them; the status
    alone when the body is something else (a proxy's page)."""
    try:
        body = response.json()
    except (ValueError, RecursionError):
        body = None
    if isinstance(body, dict) and isinstance(body.get("error"), str):
        code = body.get("code")
        return body["error"], code if isinstance(code, str) else None
    return f"the clearinghouse answered HTTP {response.status_code}", None


def _parsed[T: BaseModel](model: type[T], response: httpx.Response) -> T:
    """A 2xx answer as ``model``, or a ``ProviderError`` with the answer's own status: an unknown outcome
    to the ledger, never a refusal. The body is not quoted: it may hold a payment key."""
    try:
        return model.model_validate_json(response.content)
    except ValidationError:
        raise ProviderError(
            response.status_code,
            f"the clearinghouse answered with a body that is not {model.__name__}",
        ) from None
