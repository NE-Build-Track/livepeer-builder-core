"""Python gateway SDK adapters: discovery (a `/discover-orchestrators` URL, the signer, or a startup probe
of an orchestrator list) and the paid runner call.

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
import logging
import ssl
from collections.abc import AsyncIterator, Iterable, Sequence
from pathlib import Path
from typing import Any

import aiohttp
from livepeer_gateway.discovery import discover_runners, orchestrator_discovery_urls
from livepeer_gateway.errors import (
    LivepeerGatewayError,
    LivepeerHTTPError,
    PaymentError,
    SignerRefreshRequired,
    SkipPaymentCycle,
)
from livepeer_gateway.live_runner import (
    LiveRunnerCallStream,
    LiveRunnerInstance,
    LiveRunnerPriceInfo,
    call_runner,
)
from livepeer_gateway.remote_signer import LivePaymentSession, RemoteSignerError
from pydantic import ValidationError

from livepeer_builder.contracts import (
    AttemptOutcome,
    DiscoveredOrchestrator,
    DiscoveredPriceInfo,
    DiscoveredRunner,
    JobRequest,
    NetworkRate,
    Runner,
    RunnerGPU,
    RunnerReply,
)
from livepeer_builder.errors import RunnerCallError

_LOG = logging.getLogger(__name__)
_BODY_LIMIT = 4096
# Orchestrators probed at once. Each probe is bounded by the SDK's 5 s timeout, so a 28-entry list
# finishes within two rounds.
_PROBE_CONCURRENCY = 16
_PAYMENT_ERRORS = (PaymentError, SignerRefreshRequired, SkipPaymentCycle, RemoteSignerError)
# Signer statuses that are about the orchestrator's payment params, not the caller: another orchestrator
# may succeed. 400 = incomplete/expired params; 481 = price ceiling.
# Caller- or signer-side refusals (402 allowance, 403 forbidden, 502 webhook, 503 outbox) are not here.
_ORCHESTRATOR_SPECIFIC_SIGNER_STATUSES = frozenset({400, 481})


class SignerDiscoverySource:
    """Reads the signer's discovery endpoint, or a discovery URL that overrides it."""

    def __init__(
        self,
        *,
        signer_url: str | None = None,
        discovery_url: str | None = None,
        signer_headers: dict[str, str] | None = None,
    ) -> None:
        self._signer_url = signer_url
        self._discovery_url = discovery_url
        self._signer_headers = signer_headers

    async def fetch(self) -> tuple[Runner, ...]:
        # The signer bearer goes to the signer only, never to a separate discovery URL.
        entries = await discover_runners(
            signer_url=self._signer_url,
            signer_headers=self._signer_headers,
            discovery_url=self._discovery_url,
        )
        return tuple(runners_from_entries(entries))


class OrchestratorListDiscoverySource:
    """Probes an engine-owned orchestrator list once, on the first fetch (D42). For development and demos.

    ``LPB_ORCHESTRATOR_LIST`` is addresses inline (comma-separated, or one ``http(s)://`` URL) or a file
    of go-livepeer ``-orchWebhookUrl`` JSON (``[{"address": "https://...", "score": n}]``). The first
    fetch calls ``GET /discovery`` on every address with ``discover_runners``, TLS verified against the
    process trust store. A later fetch returns that snapshot. An orchestrator that fails, including one
    whose certificate the process does not trust, is left out and never called. The fetch raises when
    the list cannot be read or names no orchestrator, or when every orchestrator fails; that failure is
    not remembered, so the next discovery interval tries again. Capacity from the probe is stale by the
    next job, so ``capacity_available`` is unknown; price stays, because the call needs its unit. A live
    catalog belongs in ``LPB_DISCOVERY_URL``. No credential is sent to orchestrators.
    """

    def __init__(self, source: str) -> None:
        self._source = source.strip()
        self._probed: tuple[Runner, ...] | None = None

    async def fetch(self) -> tuple[Runner, ...]:
        if self._probed is not None:
            return self._probed
        runners = await self._probe()
        self._probed = runners
        return runners

    async def _probe(self) -> tuple[Runner, ...]:
        urls = list(dict.fromkeys(orchestrator_discovery_urls(await self._addresses())))
        if not urls:
            raise LivepeerGatewayError(f"orchestrator list {self._source} names no orchestrators")
        limit = asyncio.Semaphore(_PROBE_CONCURRENCY)

        async def probe(url: str) -> list[dict[str, Any]]:
            async with limit:
                found: list[dict[str, Any]] = await discover_runners(discovery_url=url)
                return found

        results = await asyncio.gather(*(probe(url) for url in urls), return_exceptions=True)
        entries: list[dict[str, Any]] = []
        first_error: Exception | None = None
        answered = False
        for url, result in zip(urls, results, strict=True):
            if isinstance(result, Exception):
                if first_error is None:
                    first_error = result
                _log_excluded(url, result)
            elif isinstance(result, BaseException):
                raise result
            else:
                answered = True
                entries.extend(result)
        if not answered and first_error is not None:
            raise first_error
        runners = _unique_by_url(runners_from_entries(entries))
        return tuple(runner.model_copy(update={"capacity_available": None}) for runner in runners)

    async def _addresses(self) -> str | list[str]:
        if self._source.startswith(("http://", "https://")) or "," in self._source:
            return self._source  # orchestrator_discovery_urls splits a comma-delimited string
        try:
            text = await asyncio.to_thread(Path(self._source).read_text, encoding="utf-8")
            payload = json.loads(text)
        except (OSError, ValueError) as error:
            raise LivepeerGatewayError(f"orchestrator list {self._source}: {error}") from error
        return _orchestrator_addresses(payload)


def _log_excluded(url: str, error: Exception) -> None:
    reason = "untrusted_certificate" if _untrusted_certificate(error) else "failed"
    _LOG.warning(
        "orchestrator excluded from discovery",
        extra={
            "event": "discovery.orchestrator_excluded",
            "orchestrator": url,
            "reason": reason,
            "error": str(error),
        },
    )


def _orchestrator_addresses(payload: object) -> list[str]:
    """go-livepeer ``deserializeWebhookJSON``: a JSON list; entries without an ``address`` are skipped."""
    if not isinstance(payload, list):
        raise LivepeerGatewayError(f"orchestrator list must be a JSON list, got {type(payload).__name__}")
    return [
        item["address"]
        for item in payload
        if isinstance(item, dict) and isinstance(item.get("address"), str) and item["address"].strip()
    ]


def _untrusted_certificate(error: BaseException) -> bool:
    """True when a TLS certificate check caused ``error`` (the SDK wraps it, keeping ``cause``)."""
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        if isinstance(current, (aiohttp.ClientConnectorCertificateError, ssl.SSLCertVerificationError)):
            return True
        seen.add(id(current))
        current = getattr(current, "cause", None) or current.__cause__ or current.__context__
    return False


def _unique_by_url(runners: Iterable[Runner]) -> list[Runner]:
    """One runner per URL, the first in list order: two orchestrators may advertise the same runner."""
    unique: dict[str, Runner] = {}
    for runner in runners:
        unique.setdefault(runner.url, runner)
    return list(unique.values())


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

    def __init__(self, signer_url: str | None, max_payment_challenge_retries: int = 3) -> None:
        self._signer_url = signer_url
        self._retries = max_payment_challenge_retries

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
            result = await call_runner(url, **kwargs)
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
                stream = await call_runner(url, stream=True, **kwargs)
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
        body = error.body[:_BODY_LIMIT] if error.body else None
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


def runners_from_entries(entries: Sequence[object]) -> list[Runner]:
    """Map a `/discover-orchestrators` list onto `Runner`, one orchestrator at a time.

    Entries without an address are skipped. An orchestrator that does not match the contract is
    skipped and logged, and the rest are kept. Raises only when entries name orchestrators and none of
    them parses, so the caller keeps its last good snapshot.
    """
    runners: list[Runner] = []
    named = parsed = 0
    first_error: ValidationError | None = None
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        address = entry.get("address")
        if not isinstance(address, str) or not address.strip():
            continue
        named += 1
        try:
            discovered = DiscoveredOrchestrator.model_validate(entry)
        except ValidationError as error:
            first_error = first_error or error
            _LOG.warning(
                "orchestrator entry does not match the discovery contract",
                extra={
                    "event": "discovery.orchestrator_invalid",
                    "orchestrator": address.strip(),
                    "error": str(error),
                },
            )
            continue
        parsed += 1
        for raw in discovered.runners:
            runner = _runner_from_discovered(raw, address.strip())
            if runner is not None:
                runners.append(runner)
    if named and not parsed:
        raise LivepeerGatewayError(f"no orchestrator entry matches the discovery contract: {first_error}")
    return runners


def _runner_from_discovered(raw: DiscoveredRunner, orchestrator_url: str) -> Runner | None:
    url, app = _text(raw.url), _text(raw.app)
    if not url or not app:
        return None
    available = raw.capacity_available
    if available is None and raw.capacity is not None and raw.session_ids is not None:
        available = raw.capacity - len(raw.session_ids)  # session_ids is the older form
    return Runner(
        url=url,
        app=app,
        runner_id=_text(raw.runner_id),
        mode=_text(raw.mode),
        orchestrator_url=orchestrator_url,
        capacity=raw.capacity,
        capacity_available=available,
        rate=_rate(raw.price_info),
        version=_text(raw.version),
        metadata=_text(raw.metadata),
        gpu=_gpu(raw.gpu),
    )


def _gpu(value: RunnerGPU | None) -> RunnerGPU | None:
    if value is None:
        return None
    gpu = RunnerGPU(
        id=_text(value.id),
        name=_text(value.name),
        vram_mb=value.vram_mb if isinstance(value.vram_mb, int) and value.vram_mb > 0 else None,
    )
    return gpu if (gpu.id or gpu.name or gpu.vram_mb) else None


def _rate(value: DiscoveredPriceInfo | None) -> NetworkRate | None:
    if value is None:
        return None
    return NetworkRate(amount=value.price, currency=value.currency, unit=value.unit)


def _price_info(rate: NetworkRate | None) -> LiveRunnerPriceInfo | None:
    if rate is None:
        return None
    return LiveRunnerPriceInfo(price=str(rate.amount), currency=rate.currency, unit=rate.unit)


def _text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None
