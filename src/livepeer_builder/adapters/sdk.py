"""Python gateway SDK adapters: signer discovery and the paid runner call.

The only module that imports ``livepeer_gateway`` (enforced by tests/structure). Payment evidence
comes from the SDK's ``on_payment_session`` callback (livepeer-python-gateway#81, pinned in
pyproject.toml; docs/references/sdk-gateway.md). The SDK calls it once for each new payment session
after the signer returns a valid payment and before the paid request goes out. The adapter derives
``manifest_ids`` and ``payment_sent`` from those sessions: if signing fails or is cancelled before
the callback runs, ``payment_sent`` stays ``False`` and the engine keeps owning failover and cost
attribution.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, Protocol, overload

import aiohttp
from livepeer_gateway.discovery import discover_runners
from livepeer_gateway.errors import (
    LivepeerGatewayError,
    LivepeerHTTPError,
    PaymentError,
    SignerRefreshRequired,
    SkipPaymentCycle,
)
from livepeer_gateway.live_runner import (
    LivePaymentSessionCallback,
    LiveRunnerCallResult,
    LiveRunnerCallStream,
    LiveRunnerInstance,
    LiveRunnerPriceInfo,
    call_runner,
)
from livepeer_gateway.remote_signer import LivePaymentSession, RemoteSignerError

from livepeer_builder.contracts import (
    AttemptOutcome,
    JobRequest,
    NetworkRate,
    Runner,
    RunnerGPU,
    RunnerReply,
)
from livepeer_builder.errors import RunnerCallError
from livepeer_builder.utils import BODY_LIMIT

_PAYMENT_ERRORS = (PaymentError, SignerRefreshRequired, SkipPaymentCycle, RemoteSignerError)
# Signer statuses that are about the orchestrator's payment params, not the caller: another orchestrator
# may succeed. 400 = incomplete/expired params; 481 = price ceiling.
# Caller- or signer-side refusals (402 allowance, 403 forbidden, 502 webhook, 503 outbox) are not here.
_ORCHESTRATOR_SPECIFIC_SIGNER_STATUSES = frozenset({400, 481})


class DiscoverRunners(Protocol):
    """The SDK's ``discover_runners``, narrowed to the arguments this adapter passes."""

    async def __call__(
        self,
        *,
        signer_url: str | None,
        signer_headers: dict[str, str] | None,
        discovery_url: str | None,
    ) -> list[dict[str, Any]]: ...


class CallRunner(Protocol):
    """The SDK's ``call_runner``, narrowed to the arguments this adapter passes.

    The SDK ships no type information, so mypy cannot hold the real function to this; a unit test
    compares the SDK's signature with these keyword names instead.
    """

    @overload
    async def __call__(
        self,
        runner_url: str,
        /,
        *,
        runner: LiveRunnerInstance | None,
        payload: Any,
        method: str,
        signer_url: str | None,
        signer_headers: dict[str, str] | None,
        timeout: float,  # noqa: ASYNC109 - the SDK's own parameter name
        max_payment_challenge_retries: int,
        on_payment_session: LivePaymentSessionCallback | None,
        stream: Literal[False] = False,
    ) -> LiveRunnerCallResult: ...

    @overload
    async def __call__(
        self,
        runner_url: str,
        /,
        *,
        runner: LiveRunnerInstance | None,
        payload: Any,
        method: str,
        signer_url: str | None,
        signer_headers: dict[str, str] | None,
        timeout: float,  # noqa: ASYNC109 - the SDK's own parameter name
        max_payment_challenge_retries: int,
        on_payment_session: LivePaymentSessionCallback | None,
        stream: Literal[True],
    ) -> LiveRunnerCallStream: ...


class SignerDiscoverySource:
    """Reads the signer's discovery endpoint, or a discovery URL that overrides it."""

    def __init__(
        self,
        *,
        signer_url: str | None = None,
        discovery_url: str | None = None,
        signer_headers: dict[str, str] | None = None,
        discover: DiscoverRunners = discover_runners,
    ) -> None:
        self._signer_url = signer_url
        self._discovery_url = discovery_url
        self._signer_headers = signer_headers
        self._discover = discover

    async def fetch(self) -> tuple[Runner, ...]:
        # The signer bearer goes to the signer only, never to a separate discovery URL.
        entries = await self._discover(
            signer_url=self._signer_url,
            signer_headers=self._signer_headers,
            discovery_url=self._discovery_url,
        )
        return tuple(runners_from_entries(entries))


class _SignedSessions:
    """Collects what the signer signed during one call (``on_payment_session``).

    ``on_payment_session`` runs only after ``LivePaymentSession.get_payment()`` validates a signed
    payment and before the paid request is sent to the runner. A reported session means the signer
    charged the allocation and counts as ``payment_sent``, so the engine never fails over (or
    retries) work that was paid for. If signing fails or is cancelled before the callback runs,
    ``signed`` stays ``False`` (no payment sent).
    """

    def __init__(self) -> None:
        self.signed = False
        self.manifest_ids: list[str] = []

    def __call__(self, session: LivePaymentSession) -> None:
        self.signed = True
        if session.manifest_id:
            self.manifest_ids.append(session.manifest_id)


class SdkRunnerTransport:
    """``call_runner`` with the actor's credential as the signer bearer. Unpaid when there is none."""

    def __init__(
        self,
        signer_url: str | None,
        max_payment_challenge_retries: int = 3,
        *,
        call: CallRunner = call_runner,
    ) -> None:
        self._signer_url = signer_url
        self._retries = max_payment_challenge_retries
        self._call = call

    def _call_kwargs(
        self, runner: Runner, request: JobRequest, credential: str | None, sessions: "_SignedSessions"
    ) -> tuple[str, dict[str, Any]]:
        paid = credential is not None and self._signer_url is not None
        instance = LiveRunnerInstance(
            url=runner.url,
            app=runner.app,
            runner_id=runner.runner_id or "",
            mode=runner.mode or "",
            orchestrator_url=runner.orchestrator_url or "",
            raw={},
            price_info=_price_info(runner.rate),
        )
        url = runner.url.rstrip("/") + request.path
        kwargs: dict[str, Any] = {
            "runner": instance,
            # The SDK sends an empty body for None; runners expect the JSON object earlier pins sent.
            "payload": {} if request.payload is None else request.payload,
            "method": request.method,
            "signer_url": self._signer_url if paid else None,
            "signer_headers": {"Authorization": f"Bearer {credential}"} if paid else None,
            "timeout": request.timeout_s,
            "max_payment_challenge_retries": self._retries,
            "on_payment_session": sessions if paid else None,
        }
        return url, kwargs

    async def call(self, runner: Runner, request: JobRequest, credential: str | None) -> RunnerReply:
        sessions = _SignedSessions()
        url, kwargs = self._call_kwargs(runner, request, credential, sessions)
        try:
            result = await self._call(url, **kwargs)
        except LivepeerGatewayError as error:
            raise call_error(
                error, url, payment_sent=sessions.signed, manifest_ids=tuple(sessions.manifest_ids)
            ) from error
        except (aiohttp.ClientError, OSError) as error:
            raise RunnerCallError(
                network_kind(error),
                str(error),
                payment_sent=sessions.signed,
                manifest_ids=tuple(sessions.manifest_ids),
            ) from error
        content = result.content if result.content is not None else json.dumps(result.data).encode()
        return RunnerReply(
            status_code=200,
            content=content,
            content_type=result.content_type or "application/json",
            data=result.data if result.content is None else None,
            manifest_ids=tuple(sessions.manifest_ids),
            payment_sent=sessions.signed,
        )

    async def open(self, runner: Runner, request: JobRequest, credential: str | None) -> "SdkRunnerStream":
        """``call_runner(stream=True)``: the 402 is paid before the first byte, so payment is known here.

        The SDK does not bound the wait for the runner's response headers, so this does
        (``first_byte_timeout_s``, D35). A timeout keeps the sessions signed so far for cost sync;
        if it hits before ``on_payment_session`` runs, ``payment_sent`` is ``False``.
        """
        sessions = _SignedSessions()
        url, kwargs = self._call_kwargs(runner, request, credential, sessions)
        try:
            async with asyncio.timeout(request.first_byte_timeout_s):
                stream = await self._call(url, stream=True, **kwargs)
        except TimeoutError as error:
            raise RunnerCallError(
                "timeout",
                f"no response from {url} within {request.first_byte_timeout_s:g}s",
                payment_sent=sessions.signed,
                manifest_ids=tuple(sessions.manifest_ids),
            ) from error
        except LivepeerGatewayError as error:
            raise call_error(
                error, url, payment_sent=sessions.signed, manifest_ids=tuple(sessions.manifest_ids)
            ) from error
        except (aiohttp.ClientError, OSError) as error:
            raise RunnerCallError(
                network_kind(error),
                str(error),
                payment_sent=sessions.signed,
                manifest_ids=tuple(sessions.manifest_ids),
            ) from error
        return SdkRunnerStream(stream, sessions)


class SdkRunnerStream:
    """The SDK's live response as a ``RunnerStream``. Mid-stream transport failures become RunnerCallError."""

    def __init__(self, stream: LiveRunnerCallStream, sessions: "_SignedSessions") -> None:
        self._stream = stream
        self._sessions = sessions

    @property
    def status_code(self) -> int:
        return int(self._stream.status)

    @property
    def content_type(self) -> str:
        return str(self._stream.content_type or "application/octet-stream")

    @property
    def manifest_ids(self) -> tuple[str, ...]:
        return tuple(self._sessions.manifest_ids)

    @property
    def payment_sent(self) -> bool:
        return self._sessions.signed

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[bytes]:
        try:
            async for chunk in self._stream.aiter_bytes():
                if chunk:
                    yield chunk
        except (aiohttp.ClientError, OSError, TimeoutError) as error:
            raise RunnerCallError(
                network_kind(error),
                f"stream failed: {error}",
                payment_sent=self.payment_sent,
                manifest_ids=self.manifest_ids,
            ) from error

    async def aclose(self) -> None:
        await self._stream.aclose()


def call_error(
    error: LivepeerGatewayError,
    runner_url: str,
    *,
    payment_sent: bool,
    manifest_ids: tuple[str, ...] = (),
) -> RunnerCallError:
    """Map an SDK error to an attempt outcome, with the payment evidence the callback collected."""
    kind: AttemptOutcome
    status: int | None = None
    body: str | None = None
    if isinstance(error, LivepeerHTTPError) and error.url == runner_url:
        status = error.status_code
        body = error.body[:BODY_LIMIT] if error.body else None
        kind = "refused" if status == 503 else "http"
    elif isinstance(error, LivepeerHTTPError):
        # An HTTP error from any other URL came from the signer.
        orchestrator_specific = error.status_code in _ORCHESTRATOR_SPECIFIC_SIGNER_STATUSES
        kind = "payment_refused" if orchestrator_specific and not payment_sent else "payment"
        status = error.status_code
    elif isinstance(error, _PAYMENT_ERRORS):
        kind = "payment"
    else:
        kind = network_kind(error)
    return RunnerCallError(
        kind,
        str(error),
        payment_sent=payment_sent,
        manifest_ids=manifest_ids,
        status_code=status,
        body=body,
    )


def network_kind(error: BaseException) -> AttemptOutcome:
    current: BaseException | None = error
    while current is not None:
        if isinstance(current, TimeoutError):
            return "timeout"
        if isinstance(current, (ConnectionError, OSError, aiohttp.ClientConnectionError)):
            return "unreachable"
        current = current.__cause__
    return "http"


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


def _price_info(rate: NetworkRate | None) -> LiveRunnerPriceInfo | None:
    if rate is None:
        return None
    return LiveRunnerPriceInfo(price=str(rate.amount), currency=rate.currency, unit=rate.unit)


def _text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None
