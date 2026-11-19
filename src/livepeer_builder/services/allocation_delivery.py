"""Request-local allocation credential delivery; no durable clearinghouse secret storage."""

from uuid import uuid4

from livepeer_builder.contracts import OPERATION_REPLAYED, ActorAllocation, DeliveredAllocation, SecretRef
from livepeer_builder.errors import NotProvisioned
from livepeer_builder.protocols import ActorRepo


class DeliverySecrets:
    """Keep provider secrets only until the authenticated command response is constructed.

    Stored references are opaque delivery receipts, not resolvable secret-store handles.
    Replays never recover secrets from these references. The work engine owns persistence.
    """

    def __init__(self) -> None:
        self._tokens: dict[str, str] = {}

    async def put(self, name: str, secret: str) -> SecretRef:
        ref = SecretRef(ref=f"delivered:{uuid4()}")
        self._tokens[ref.ref] = secret
        return ref

    async def get(self, ref: SecretRef) -> str:
        if ref.ref not in self._tokens:
            raise NotProvisioned("allocation credential is held by the work engine")
        return self._tokens[ref.ref]

    async def delete(self, ref: SecretRef) -> None:
        self._tokens.pop(ref.ref, None)

    def clear(self) -> None:
        self._tokens.clear()

    def token(self, ref: SecretRef) -> str | None:
        return self._tokens.get(ref.ref)


async def delivered(
    store: ActorRepo, allocation: ActorAllocation, secrets: DeliverySecrets
) -> DeliveredAllocation:
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
    return DeliveredAllocation(**allocation.model_dump(), allocation_api_key=token)
