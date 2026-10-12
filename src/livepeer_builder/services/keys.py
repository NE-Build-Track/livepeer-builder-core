"""Engine-issued access keys (``lpk_``): issue, inspect without disclosure, revoke, and bootstrap.

The authenticator that checks these keys lives in livepeer_builder.adapters.auth.
"""

import secrets
from collections.abc import Iterable

from livepeer_builder.contracts import (
    DEFAULT_ACTOR_PERMISSIONS,
    AccessKey,
    ActorContext,
    CredentialKind,
    IssuedKey,
    Operation,
)
from livepeer_builder.errors import AccessDenied, InvalidRequest
from livepeer_builder.protocols import AccessPolicy, Clock, KeyRepo
from livepeer_builder.utils import token_hash

TOKEN_PREFIX = "lpk_"  # noqa: S105 - a public prefix, not a secret


class KeyService:
    def __init__(self, store: KeyRepo, policy: AccessPolicy, clock: Clock) -> None:
        self._store = store
        self._policy = policy
        self._clock = clock

    async def issue(
        self,
        admin: ActorContext,
        *,
        kind: CredentialKind,
        actor_id: str,
        application_id: str = "default",
        permissions: Iterable[str] | None = None,
        label: str = "",
    ) -> IssuedKey:
        self._policy.check(admin, Operation.ADMIN_KEYS)
        return await self._issue(kind, actor_id, application_id, permissions, label)

    async def bootstrap_admin(self, label: str = "bootstrap") -> IssuedKey:
        """Issue the first admin key. Refused once any active admin key exists (one-time bootstrap).

        Called only from the CLI (``lpb bootstrap-admin``); there is no network route and no static
        admin token.
        """
        if await self._store.count_active_admin_keys() > 0:
            raise AccessDenied("an admin key already exists; issue further keys with it")
        return await self._issue("admin", "operator", "default", (), label)

    async def revoke(self, admin: ActorContext, key_id: str) -> None:
        self._policy.check(admin, Operation.ADMIN_KEYS)
        if not await self._store.revoke_access_key(key_id, self._clock.now()):
            raise InvalidRequest(f"no active key {key_id!r}")

    async def list_keys(self, admin: ActorContext, application_id: str | None = None) -> list[AccessKey]:
        self._policy.check(admin, Operation.ADMIN_KEYS)
        return await self._store.list_access_keys(application_id)

    async def _issue(
        self,
        kind: CredentialKind,
        actor_id: str,
        application_id: str,
        permissions: Iterable[str] | None,
        label: str,
    ) -> IssuedKey:
        granted = _validated_permissions(kind, permissions)
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        key = AccessKey(
            key_id="key_" + secrets.token_urlsafe(12),
            kind=kind,
            actor_id=actor_id,
            application_id=application_id,
            permissions=tuple(sorted(granted)),
            label=label,
            created_at=self._clock.now(),
            revoked_at=None,
        )
        await self._store.insert_access_key(key, token_hash(token))
        return IssuedKey(key=key, token=token)


def _validated_permissions(kind: CredentialKind, permissions: Iterable[str] | None) -> set[str]:
    if kind == "admin":
        return set()  # admin is a credential kind; it needs no permission strings
    granted = set(DEFAULT_ACTOR_PERMISSIONS if permissions is None else permissions)
    known = {operation.value for operation in Operation}
    unknown = granted - known
    if unknown:
        raise InvalidRequest(f"unknown permissions: {sorted(unknown)}")
    if Operation.ACTORS_ASSERT in granted and kind != "service":
        raise InvalidRequest("actors.assert is only valid on a service credential")
    return granted
