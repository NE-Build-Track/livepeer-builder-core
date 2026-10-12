"""Authenticators: the engine API-key adapter and the ordered chain (docs/contracts/access.md).

JWT bearer (M4) and the trusted-backend service credential are defined in the contract and are
not implemented here yet.
"""

from collections.abc import Sequence

from livepeer_builder.contracts import ActorContext, AuthProvenance, AuthRequest
from livepeer_builder.errors import NotAuthenticated
from livepeer_builder.protocols import Authenticator, KeyRepo
from livepeer_builder.utils import token_hash

API_KEY_PREFIX = "lpk_"


class ApiKeyAuthenticator:
    """``Authorization: Bearer lpk_...``. Any other credential is "not mine" (returns None)."""

    def __init__(self, store: KeyRepo, realm: str = "livepeer-builder") -> None:
        self._store = store
        self._realm = realm

    async def authenticate(self, request: AuthRequest) -> ActorContext | None:
        token = _bearer(request)
        if token is None or not token.startswith(API_KEY_PREFIX):
            return None
        key = await self._store.access_key_by_hash(token_hash(token))
        if key is None or key.revoked_at is not None:
            raise NotAuthenticated("unknown or revoked key")
        return ActorContext(
            actor_id=key.actor_id,
            application_id=key.application_id,
            permissions=frozenset(key.permissions),
            credential_kind=key.kind,
            auth=AuthProvenance(method="api_key", credential_id=key.key_id),
        )

    def challenge(self) -> str | None:
        return f'Bearer realm="{self._realm}"'


class AuthenticatorChain:
    """Runs authenticators in order. The first ActorContext wins; NotAuthenticated stops the chain."""

    def __init__(self, authenticators: Sequence[Authenticator]) -> None:
        self._authenticators = tuple(authenticators)

    async def authenticate(self, request: AuthRequest) -> ActorContext | None:
        for authenticator in self._authenticators:
            actor = await authenticator.authenticate(request)
            if actor is not None:
                return actor
        return None

    def challenge(self) -> str | None:
        values = [value for a in self._authenticators if (value := a.challenge())]
        return ", ".join(values) or None


def _bearer(request: AuthRequest) -> str | None:
    header = request.header("authorization")
    if header is None:
        return None
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return None
    return value.strip()
