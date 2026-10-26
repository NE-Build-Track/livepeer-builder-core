"""Engine-issued access keys (``lpk_``): issue, inspect without disclosure, rotate, revoke, and bootstrap.

Every change is logged (``keys.bootstrapped``, ``keys.issued``, ``keys.rotated``, ``keys.revoked``) with the
acting admin, never with a token or its hash (D44). The authenticator that checks these keys lives in
livepeer_builder.adapters.auth.
"""

import logging
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
from livepeer_builder.errors import AccessDenied, InvalidRequest, NotFound
from livepeer_builder.protocols import AccessPolicy, Clock, KeyRepo
from livepeer_builder.utils import token_hash

_LOG = logging.getLogger(__name__)
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
        issued = await self._issue(kind, actor_id, application_id, permissions, label)
        _audit("keys.issued", issued.key, by=admin.actor_id)
        return issued

    async def bootstrap_admin(self, label: str = "bootstrap") -> IssuedKey:
        """Issue the first admin key. Refused once any active admin key exists (one-time bootstrap).

        Called only from the CLI (``lpb bootstrap-admin``); there is no network route and no static
        admin token.
        """
        if await self._store.count_active_admin_keys() > 0:
            raise AccessDenied("an admin key already exists; issue further keys with it")
        issued = await self._issue("admin", "operator", "default", (), label)
        _audit("keys.bootstrapped", issued.key, by="cli")
        return issued

    async def get(self, admin: ActorContext, key_id: str) -> AccessKey:
        """One key as stored, without its secret: the token is never kept, only its hash, never shown."""
        self._policy.check(admin, Operation.ADMIN_KEYS)
        key = await self._store.get_access_key(key_id)
        if key is None:
            raise NotFound(f"key {key_id!r} not found")
        return key

    async def rotate(self, admin: ActorContext, key_id: str, *, label: str | None = None) -> IssuedKey:
        """Replace an active key at once (D44): the old token stops working in the same transaction that
        stores the new one. Same kind, actor, application and permissions; ``label`` renames it."""
        self._policy.check(admin, Operation.ADMIN_KEYS)
        old = await self._store.get_access_key(key_id)
        if old is None or old.revoked_at is not None:
            raise InvalidRequest(f"no active key {key_id!r}")
        token, key = self._new_key(
            old.kind, old.actor_id, old.application_id, old.permissions, old.label if label is None else label
        )
        if not await self._store.rotate_access_key(key_id, key, token_hash(token), self._clock.now()):
            raise InvalidRequest(f"no active key {key_id!r}")  # revoked or rotated meanwhile
        _audit("keys.rotated", key, by=admin.actor_id, replaced_key_id=key_id)
        return IssuedKey(key=key, token=token)

    async def revoke(self, admin: ActorContext, key_id: str) -> None:
        self._policy.check(admin, Operation.ADMIN_KEYS)
        key = await self._store.get_access_key(key_id)  # whose key it was, for the audit line (#48 review)
        if key is None or not await self._store.revoke_access_key(key_id, self._clock.now()):
            raise InvalidRequest(f"no active key {key_id!r}")
        _audit("keys.revoked", key, by=admin.actor_id)

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
        token, key = self._new_key(kind, actor_id, application_id, granted, label)
        await self._store.insert_access_key(key, token_hash(token))
        return IssuedKey(key=key, token=token)

    def _new_key(
        self, kind: CredentialKind, actor_id: str, application_id: str, permissions: Iterable[str], label: str
    ) -> tuple[str, AccessKey]:
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        key = AccessKey(
            key_id="key_" + secrets.token_urlsafe(12),
            kind=kind,
            actor_id=actor_id,
            application_id=application_id,
            permissions=tuple(sorted(permissions)),
            label=label,
            created_at=self._clock.now(),
            revoked_at=None,
        )
        return token, key


def _audit(event: str, key: AccessKey, *, by: str, replaced_key_id: str | None = None) -> None:
    """One key change, with who made it. Never the token or its hash."""
    replaced = {} if replaced_key_id is None else {"replaced_key_id": replaced_key_id}
    _LOG.info(
        event.replace("keys.", "key "),
        extra={
            "event": event,
            "key_id": key.key_id,
            "kind": key.kind,
            "actor_id": key.actor_id,
            "application_id": key.application_id,
            "by": by,
            **replaced,
        },
    )


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
