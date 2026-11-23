"""Request-local allocation credential delivery; no durable clearinghouse secret storage."""

from uuid import uuid4

from pydantic import SecretStr

from livepeer_builder.contracts import (
    OPERATION_REPLAYED,
    OPERATION_RESUMED,
    ActorAllocation,
    DeliveredAllocation,
    SecretRef,
)
from livepeer_builder.errors import NotProvisioned
from livepeer_builder.protocols import ActorRepo


class DeliverySecrets:
    """Keep provider secrets only until the authenticated command response is constructed.

    Stored references are opaque delivery receipts, not resolvable secret-store handles. Whether an
    actor's reference is one is its ``credential_custody``, never the text of the reference. Replays never
    recover secrets from these references. The work engine owns persistence.
    """

    def __init__(self) -> None:
        self._tokens: dict[str, str] = {}

    def put(self, secret: str) -> SecretRef:
        """Hold ``secret`` under a new receipt. Unlike a ``SecretStore``, it takes no name."""
        ref = SecretRef(ref=f"delivered:{uuid4()}")
        self._tokens[ref.ref] = secret
        return ref

    def hold(self, ref: SecretRef, secret: str) -> None:
        """Hold ``secret`` under a receipt an earlier run issued, which the actor already points at: a
        resume, or an allotment another request wrote first, has the key the provider replayed in hand,
        and this request's answer carries it."""
        self._tokens[ref.ref] = secret

    def get(self, ref: SecretRef) -> str:
        if ref.ref not in self._tokens:  # a backstop: credential() never asks for a work engine's key
            raise NotProvisioned("allocation credential is held by the work engine")
        return self._tokens[ref.ref]

    def delete(self, ref: SecretRef) -> None:
        self._tokens.pop(ref.ref, None)

    def clear(self) -> None:
        self._tokens.clear()

    def token(self, ref: SecretRef) -> str | None:
        return self._tokens.get(ref.ref)


async def delivered(
    store: ActorRepo, allocation: ActorAllocation, secrets: DeliverySecrets
) -> DeliveredAllocation:
    """The answer to a delivering command: the key, while the actor as stored still holds it, else none
    and ``replayed``. A repeat of a finished command never carries a key, and neither does an answer the
    actor has moved past (revoked, or rotated or allotted again since). A resumed command's answer is
    marked ``resumed``, with or without a key."""
    token = None
    if not OPERATION_REPLAYED.get():
        current = await store.get_actor(allocation.tenant_id, allocation.actor_id)
        if (
            current is not None
            and current.status != "revoked"
            and current.allocation_id == allocation.allocation_id
            and current.api_key_id == allocation.api_key_id
            and current.secret_ref == allocation.secret_ref
        ):
            token = secrets.token(allocation.secret_ref)
    return DeliveredAllocation(
        **allocation.model_dump(),
        allocation_api_key=SecretStr(token) if token is not None else None,
        replayed=token is None,
        resumed=OPERATION_RESUMED.get(),
    )
