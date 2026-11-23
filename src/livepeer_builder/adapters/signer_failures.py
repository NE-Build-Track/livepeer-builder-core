"""What a remote-signer failure means for a payment (D58).

The SDK wraps every signer failure in a ``LivepeerGatewayError`` and chains the transport's own error as
its ``__cause__`` (``livepeer_gateway.http._request_body`` at ``2017a80``), so the cause chain says which
phase failed.

Builders read the engine's words only (``signer_message``): the SDK's text carries the signer URL and up
to 2,000 characters of the signer's body, so it goes to the log.
"""

import aiohttp
from livepeer_gateway.errors import (
    LivepeerGatewayError,
    LivepeerHTTPError,
    PaymentError,
    SignerRefreshRequired,
    SkipPaymentCycle,
)
from livepeer_gateway.remote_signer import RemoteSignerError

from livepeer_builder.contracts import AttemptOutcome, PaymentReason

_PAYMENT_ERRORS = (PaymentError, SkipPaymentCycle, RemoteSignerError)
# 400 and 481 are this orchestrator's params (D26). 402, 403, 502 and 503 are the caller's or the signer's.
_ORCHESTRATOR_SPECIFIC_SIGNER_STATUSES = frozenset({400, 481})
# These say the signer did not sign. A timeout, a lost reply or a 5xx may have charged.
_SIGNER_REFUSALS = frozenset({400, 401, 402, 403, 481})
# What a refusal means for the builder (D58): Batteries' decision, passed through by the signer
# (livepeer/node signer/auth.go). A paused allocation is a 403 too: the signer drops Batteries' reason.
_REFUSAL_REASONS: dict[int, PaymentReason] = {
    401: "not_authorized",
    403: "not_authorized",
    402: "allowance_exhausted",
}
_REFUSED = {"not_authorized": "not authorized", "allowance_exhausted": "allowance exhausted"}


def classify(
    error: LivepeerGatewayError, *, unknown: bool
) -> tuple[AttemptOutcome, str, int | None, PaymentReason | None]:
    """A signer failure as ``(kind, message, status_code, reason)``. ``unknown``: the signer was asked
    and gave no validated answer, so it may have charged (``payment_unknown``, D53).

    A refresh past the retry budget is unpaid ``payment_refused``, like a 400.
    """
    status: int | None = None
    reason: PaymentReason | None = None
    if isinstance(error, SignerRefreshRequired):
        kind: AttemptOutcome = "payment_refused"
    elif isinstance(error, LivepeerHTTPError):
        status = error.status_code
        kind = "payment_refused" if status in _ORCHESTRATOR_SPECIFIC_SIGNER_STATUSES else "payment"
        reason = _REFUSAL_REASONS.get(status)
    elif isinstance(error, _PAYMENT_ERRORS):
        kind = "payment"
    elif never_reached_signer(error):  # nothing was signed, and every runner shares the signer (Q3)
        kind, reason = "payment", "signer_unavailable"
    else:
        kind = network_kind(error)
    return kind, signer_message(error, reason, unknown=unknown), status, reason


def refused_by_signer(error: BaseException) -> bool:
    """The signer did not sign: a refusal, an expired challenge, or a request that never reached it."""
    if isinstance(error, (SignerRefreshRequired, SkipPaymentCycle)):
        return True
    if isinstance(error, LivepeerHTTPError):
        return error.status_code in _SIGNER_REFUSALS
    return never_reached_signer(error)


def never_reached_signer(error: BaseException) -> bool:
    """The request never reached the signer, so it cannot have signed (Q3).

    Only a connect-phase failure counts: aiohttp's ``ClientConnectorError`` (connection refused, a DNS
    failure, a TLS handshake that failed) is raised before a byte of the request is written, and the SDK
    opens a new session for each signer call, so no pooled connection is reused. A timeout, a connection
    dropped after sending, a 5xx or a lost reply may follow a signature, and is not this
    (tests/unit/test_payment_failures.py pins what the SDK raises in each case).
    """
    current: BaseException | None = error
    while current is not None:
        if isinstance(current, (aiohttp.ClientConnectorError, ConnectionRefusedError)):
            return True
        current = current.__cause__
    return False


def unavailable_before_signing(error: LivepeerGatewayError) -> bool:
    """At ``/sign-orchestrator-info`` nothing is signed: a 5xx, or any transport failure (refused, DNS, a
    timeout, a dropped connection), means the signer cannot pay for anyone now. A refusal is not this."""
    if isinstance(error, LivepeerHTTPError):
        return int(error.status_code) >= 500
    current: BaseException | None = error.__cause__
    while current is not None:
        if isinstance(current, (aiohttp.ClientError, OSError)):  # TimeoutError is an OSError
            return True
        current = current.__cause__
    return False


def signer_message(error: LivepeerGatewayError, reason: PaymentReason | None, *, unknown: bool) -> str:
    """``Failure.message`` for a signer failure, in the engine's words: never the SDK's text (D58)."""
    status = f" (HTTP {error.status_code})" if isinstance(error, LivepeerHTTPError) else ""
    if unknown:
        return f"the signer gave no answer while signing{status}; it may have charged"
    if reason == "signer_unavailable":
        return f"the signer is unavailable{status or ': it could not be reached'}; nothing was signed"
    if reason in _REFUSED:
        return f"the signer refused payment: {_REFUSED[reason]}{status}"
    if isinstance(error, SignerRefreshRequired):
        return "the signer kept asking for fresh payment parameters"
    if isinstance(error, LivepeerHTTPError):
        return f"the signer refused payment{status}"
    return "the signer's answer was not valid"


def network_kind(error: BaseException) -> AttemptOutcome:
    current: BaseException | None = error
    while current is not None:
        if isinstance(current, TimeoutError):
            return "timeout"
        if isinstance(current, (ConnectionError, OSError, aiohttp.ClientConnectionError)):
            return "unreachable"
        current = current.__cause__
    return "http"
