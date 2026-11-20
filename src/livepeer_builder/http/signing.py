"""The signer's two paths, as a payment clearinghouse serves them (D87, #166).

``POST /sign-orchestrator-info`` and ``POST /generate-live-payment`` are the two calls the gateway SDK
makes to a signer, and it makes them at the root of the signer's address: it keeps the scheme and the host
of that address and drops any path. So a clearinghouse serves them at the root of its listener, and an
enterprise's engine points the unmodified SDK at the clearinghouse, with an actor's payment key where a
Batteries key goes. They are the clearinghouse half's, typed against ``ClearinghouseApi``, and present
only on an engine that can forward a signer call (``payments.signing``).

These routes do not authenticate as the others do. The credential is a **payment key**, sent as the SDK
sends a signer its credential (``Authorization: Bearer lpp_...``), and nothing else is one here: an
access key is 401, and so is no credential, on both paths.

The body is read as bytes, bounded as the signer bounds it, and handed on untouched; the signer's answer
comes back with its status, its body and its content type, and no other header. What these routes answer
by themselves is in the signer's error shape (``{"error": {"message": ...}}``), where the SDK reads a
signer's words, with a ``code`` for the two failures a signer cannot have:

| The clearinghouse... | Answer |
| --- | --- |
| was given no live payment key | 401, ``WWW-Authenticate: Bearer`` |
| was given a body over 1 MiB | 400, as the signer answers one |
| could not reach its signer: nothing was sent | 503, ``code: signer_not_reached``, ``Retry-After`` |
| sent the call and got no usable answer: it may have signed | 502, ``code: signer_outcome_unknown`` |
"""

from typing import Annotated

from fastapi import FastAPI, Request, Response, Security
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from livepeer_builder.api import ClearinghouseApi, PaymentSigningApi
from livepeer_builder.contracts import SignerCall
from livepeer_builder.errors import NotAuthenticated, SignerNotReached, SignerOutcomeUnknown

_IDENTITY: SignerCall = "sign-orchestrator-info"
_PAYMENT: SignerCall = "generate-live-payment"
IDENTITY_PATH = "/sign-orchestrator-info"  # the path is the call's name: where the gateway SDK posts it
PAYMENT_PATH = "/generate-live-payment"
MAX_SIGNER_BODY_BYTES = 1 << 20  # what livepeer/node's signer reads of a payment request (signer/service.go)
NOT_REACHED_CODE = "signer_not_reached"
OUTCOME_UNKNOWN_CODE = "signer_outcome_unknown"
RETRY_AFTER_S = "10"  # as a job that failed because the signer was unavailable (http/app.py)
_NOT_REACHED = "the clearinghouse could not reach its signer; nothing was sent, and nothing was signed"
_OUTCOME_UNKNOWN = "the clearinghouse got no usable answer from its signer; the payment may have been signed"
_TOO_LARGE = "payment request too large"
_NO_STORE = {"Cache-Control": "no-store"}  # on every answer: a signed payment is nobody's to keep
_NO_PAYMENT_KEY = "a live payment key is required"  # one answer for every refusal: it says nothing of a key
_RESPONSES: dict[int | str, dict[str, str]] = {
    200: {"description": "The signer's own answer: its status, body and content type, whatever the status."},
    401: {"description": "No live payment key. Nothing was sent to the signer."},
    502: {"description": f"`code: {OUTCOME_UNKNOWN_CODE}`. The call left and no usable answer came back."},
    503: {"description": f"`code: {NOT_REACHED_CODE}`. The signer was not reached: nothing was sent."},
}


def mount_signing_routes(app: FastAPI, engine: ClearinghouseApi) -> None:
    """Add the signer's two paths to ``app``, at its root. Nothing is added on an engine that cannot
    forward a signer call: its transport forwards none, or has no signer."""
    signing = engine.payments.signing
    if signing is None:
        return
    key = HTTPBearer(
        auto_error=False,
        scheme_name="PaymentKey",
        bearerFormat="lpp",
        description="An actor's payment key: `lpp_...`, issued when its allocation is allotted",
    )
    Declared = Annotated[
        HTTPAuthorizationCredentials | None, Security(key)
    ]  # the schema's; the header is read raw

    @app.post(IDENTITY_PATH, responses=_RESPONSES)
    async def sign_orchestrator_info(request: Request, _declared: Declared) -> Response:
        """Who the clearinghouse's signer is: the call the gateway SDK begins every payment with. It
        signs no payment. It still takes a payment key: nothing here answers without one."""
        return await _forwarded(signing, _IDENTITY, request)

    @app.post(PAYMENT_PATH, responses=_RESPONSES)
    async def generate_live_payment(request: Request, _declared: Declared) -> Response:
        """Sign one payment against the allocation of the actor whose payment key this is. Fixed work
        calls it once; live work calls it every payment cycle, with the signer's `state` from the cycle
        before. An empty allocation is the signer's 402."""
        return await _forwarded(signing, _PAYMENT, request)


async def _forwarded(signing: PaymentSigningApi, call: SignerCall, request: Request) -> Response:
    try:
        payer = await signing.payer(request.headers.get("authorization"))
        body = await _bounded_body(request)
        if body is None:
            return _own_answer(400, _TOO_LARGE)
        answer = await signing.forward(call, payer, body, request.headers.get("content-type", ""))
    except NotAuthenticated:
        return _own_answer(401, _NO_PAYMENT_KEY, headers={"WWW-Authenticate": "Bearer"})
    except SignerNotReached:
        return _own_answer(503, _NOT_REACHED, NOT_REACHED_CODE, {"Retry-After": RETRY_AFTER_S})
    except SignerOutcomeUnknown:
        return _own_answer(502, _OUTCOME_UNKNOWN, OUTCOME_UNKNOWN_CODE)
    headers = dict(_NO_STORE)
    if answer.content_type:  # as a header: media_type would add a charset to the signer's own
        headers["Content-Type"] = answer.content_type
    return Response(content=answer.content, status_code=answer.status_code, headers=headers)


async def _bounded_body(request: Request) -> bytes | None:
    """The request body, or ``None`` when it is larger than the signer takes: by its declared length, or
    by what arrived, so a body streamed with no length is cut off at the bound."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_SIGNER_BODY_BYTES:
        return None
    received = bytearray()
    async for chunk in request.stream():
        received += chunk
        if len(received) > MAX_SIGNER_BODY_BYTES:
            return None
    return bytes(received)


def _own_answer(
    status: int, message: str, code: str | None = None, headers: dict[str, str] | None = None
) -> JSONResponse:
    """An answer of the clearinghouse's own, in the signer's error shape. The engine's words only: never
    the signer's address, its text or a key."""
    body: dict[str, object] = {"error": {"message": message}}
    if code is not None:
        body["code"] = code
    return JSONResponse(body, status_code=status, headers=_NO_STORE | (headers or {}))
