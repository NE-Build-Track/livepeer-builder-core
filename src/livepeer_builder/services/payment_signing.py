"""Signing a payment for an enterprise's engine: the key swap of a payment clearinghouse (D87, #166).

A clearinghouse serves the signer's two paths, and an enterprise's engine calls them with an actor's
payment key where a signer takes a Batteries key. Here the clearinghouse:

1. finds the actor the payment key was issued for (``PaymentsService.resolve_payment_key``). Anything
   that is no live payment key is ``NotAuthenticated``, on both paths, and nothing is forwarded for it;
2. reads that actor's Batteries key from its own secret store, where allotment put it;
3. hands the call to its signer with that key (``SignerForwarder``), the body as it arrived;
4. returns the signer's answer as it is.

It decides nothing about money. Whether the allocation can pay is Batteries' answer, which the signer
asks for before it signs: an empty allocation is the signer's 402 and a paused one its 403, and both come
back as they are. The Batteries key never leaves: it is in no answer and no log line, and an answer that
held it would be withheld. The payment key goes no further than this module.

Every call is logged (``payments.signer_call``) with the tenant, the actor, the allocation and what came
of it, never with a key.
"""

import logging
from collections.abc import Awaitable, Callable

from livepeer_builder.contracts import ActorAllocation, SignerAnswer, SignerCall
from livepeer_builder.errors import (
    EngineError,
    NotAuthenticated,
    NotFound,
    SignerNotReached,
    SignerOutcomeUnknown,
)
from livepeer_builder.protocols import SecretStore, SignerForwarder
from livepeer_builder.services.issued_payment_keys import PAYMENT_KEY_PREFIX

_LOG = logging.getLogger(__name__)
_EVENT = "payments.signer_call"
_BEARER = "bearer"
MAX_PAYMENT_KEY_LENGTH = 200  # a payment key is 47 characters: nothing longer is looked up
NO_PAYMENT_KEY = "a live payment key is required"  # one answer for every refusal: it says nothing of a key


class PaymentSigning:
    """``resolve`` finds the actor of a live payment key and raises ``NotAuthenticated`` for any other."""

    def __init__(
        self,
        resolve: Callable[[str], Awaitable[ActorAllocation]],
        secrets: SecretStore,
        forwarder: SignerForwarder,
    ) -> None:
        self._resolve = resolve
        self._secrets = secrets
        self._forwarder = forwarder

    async def payer(self, authorization: str | None) -> ActorAllocation:
        """The actor whose payment key the ``Authorization`` header carries, as ``Bearer lpp_...``: how
        the gateway SDK sends a signer its credential. ``NotAuthenticated`` for no header, another
        scheme, an access key, a Batteries key, and a payment key that was never issued, was replaced by
        a rotation or belongs to a revoked allocation."""
        scheme, _, value = (authorization or "").partition(" ")
        token = value.strip()
        named = scheme.lower() == _BEARER and token.startswith(PAYMENT_KEY_PREFIX)
        if not named or len(token) > MAX_PAYMENT_KEY_LENGTH:
            raise NotAuthenticated(NO_PAYMENT_KEY)
        return await self._resolve(token)

    async def forward(
        self, call: SignerCall, payer: ActorAllocation, body: bytes, content_type: str
    ) -> SignerAnswer:
        """Hand one signer call to the signer with ``payer``'s Batteries key, once, and return the
        signer's answer as it is. ``SignerNotReached`` when nothing was sent; ``SignerOutcomeUnknown``
        when the call left and no usable answer came back."""
        try:
            credential = await self._batteries_key(payer)
            answer = await self._forwarder.forward_signer_call(call, credential, body, content_type)
            if credential.encode() in answer.content:
                raise SignerOutcomeUnknown("the signer's answer held the key it was called with")
        except SignerNotReached as error:
            _log(logging.WARNING, call, payer, "not_reached", detail=str(error))
            raise
        except SignerOutcomeUnknown as error:
            _log(logging.WARNING, call, payer, "unknown", detail=str(error))
            raise
        refused = answer.status_code >= 400
        _log(logging.INFO if refused else logging.DEBUG, call, payer, "answered", status=answer.status_code)
        return answer

    async def _batteries_key(self, payer: ActorAllocation) -> str:
        """The key the allocation pays with, from the clearinghouse's own store. One that is gone was
        revoked since the payment key was read: the key stopped. One that cannot be read is the
        clearinghouse's own fault, and nothing was sent for it."""
        try:
            return await self._secrets.get(payer.secret_ref)
        except NotFound as gone:
            raise NotAuthenticated(NO_PAYMENT_KEY) from gone
        except EngineError as unreadable:
            raise SignerNotReached(f"the actor's key could not be read: {unreadable}") from unreadable


def _log(
    level: int,
    call: SignerCall,
    payer: ActorAllocation,
    outcome: str,
    *,
    status: int | None = None,
    detail: str | None = None,
) -> None:
    """One forwarded signer call, with whose allocation it was for. Never a key."""
    _LOG.log(
        level,
        "signer call forwarded",
        extra={
            "event": _EVENT,
            "call": call,
            "tenant_id": payer.tenant_id,
            "actor_id": payer.actor_id,
            "allocation_id": payer.allocation_id,
            "outcome": outcome,
            "status_code": status,
            "detail": detail,
        },
    )
