"""Engine-issued access keys (``lpk_``): issue, inspect without disclosure, rotate, revoke, and bootstrap.

Every change is logged (``keys.bootstrapped``, ``keys.issued``, ``keys.rotated``, ``keys.revoked``) with the
acting admin, never with a token or its hash (D44). The authenticator that checks these keys lives in
livepeer_builder.adapters.auth.
"""

import logging
import secrets
from collections.abc import Iterable
from typing import Any

from pydantic import TypeAdapter

from livepeer_builder.contracts import (
    DEFAULT_ACTOR_PERMISSIONS,
    OPERATION_REPLAYED,
    AccessKey,
    ActorContext,
    CredentialKind,
    IssuedKey,
    Operation,
    OperationRecord,
    ResourceRef,
)
from livepeer_builder.errors import AccessDenied, InvalidRequest, NotFound
from livepeer_builder.protocols import AccessPolicy, Clock, KeyRepo
from livepeer_builder.services import ledger
from livepeer_builder.services.ownership import check_owner
from livepeer_builder.utils import token_hash

_LOG = logging.getLogger(__name__)
_KEY = TypeAdapter(AccessKey)
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
        operation_ref: str,
    ) -> IssuedKey:
        """Issue a key once per ``operation_ref`` (D57, B4): the key and its ledger record commit together, so
        a repeat gets the key back, without its token, which is never stored."""
        self._policy.check(admin, Operation.ADMIN_KEYS)
        OPERATION_REPLAYED.set(False)
        granted = _validated_permissions(kind, permissions)
        request: dict[str, Any] = {"kind": kind, "actor_id": actor_id, "application_id": application_id}
        request |= {"permissions": sorted(granted), "label": label}
        replay = await self._replayed(admin, operation_ref, ledger.fingerprint("issue_key", request))
        if replay is not None:
            return replay
        token, key = self._new_key(kind, actor_id, application_id, granted, label)
        record = ledger.done(admin, "issue_key", operation_ref, request, _public(key), key.created_at)
        if await self._store.insert_access_key(key, token_hash(token), record):
            _audit("keys.issued", key, by=admin.actor_id)
            return IssuedKey(key=key, token=token)
        return await self._taken(admin, record)

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
        self._policy.check(admin, Operation.ADMIN_READ)
        key = await self._store.get_access_key(key_id)
        if key is None:
            raise NotFound(f"key {key_id!r} not found")
        resource = ResourceRef(kind="key", application_id=key.application_id, id=key.key_id)
        check_owner(self._policy, admin, Operation.ADMIN_READ, resource, f"key {key_id!r} not found")
        return key

    async def rotate(
        self, admin: ActorContext, key_id: str, *, label: str | None = None, operation_ref: str
    ) -> IssuedKey:
        """Replace an active key at once (D44): the old token stops working in the same transaction that
        stores the new one and its ledger record (D57). Same kind, actor, application and permissions;
        ``label`` renames it. A repeat of ``operation_ref`` gets the new key back, without its token."""
        self._policy.check(admin, Operation.ADMIN_KEYS)
        OPERATION_REPLAYED.set(False)
        request = {"key_id": key_id, "label": label}
        replay = await self._replayed(admin, operation_ref, ledger.fingerprint("rotate_access_key", request))
        if replay is not None:
            return replay
        old = await self._store.get_access_key(key_id)
        if old is None or old.revoked_at is not None:
            raise InvalidRequest(f"no active key {key_id!r}")
        token, key = self._new_key(
            old.kind, old.actor_id, old.application_id, old.permissions, old.label if label is None else label
        )
        record = ledger.done(admin, "rotate_access_key", operation_ref, request, _public(key), key.created_at)
        if not await self._store.rotate_access_key(key_id, key, token_hash(token), key.created_at, record):
            replay = await self._replayed(admin, operation_ref, record.fingerprint)
            if replay is None:
                raise InvalidRequest(f"no active key {key_id!r}")  # revoked or rotated meanwhile
            return replay  # another request took the reference just now
        _audit("keys.rotated", key, by=admin.actor_id, replaced_key_id=key_id)
        return IssuedKey(key=key, token=token)

    async def revoke(self, admin: ActorContext, key_id: str) -> None:
        self._policy.check(admin, Operation.ADMIN_KEYS)
        key = await self._store.get_access_key(key_id)  # whose key it was, for the audit line (#48 review)
        if key is None:
            raise InvalidRequest(f"no active key {key_id!r}")
        if not await self._store.revoke_access_key(key_id, self._clock.now()):
            return  # already revoked: a retry after a lost answer succeeds, and logs nothing again (D61)
        _audit("keys.revoked", key, by=admin.actor_id)

    async def list_keys(self, admin: ActorContext, application_id: str | None = None) -> list[AccessKey]:
        """Every application's keys for an admin unless it names one; an auditor's own application's (D46)."""
        scope = application_id or (None if admin.credential_kind == "admin" else admin.application_id)
        resource = None if scope is None else ResourceRef(kind="key", application_id=scope)
        self._policy.check(admin, Operation.ADMIN_READ, resource)
        return await self._store.list_access_keys(scope)

    async def _replayed(self, admin: ActorContext, operation_ref: str, fingerprint: str) -> IssuedKey | None:
        """The answer to a held reference: the recorded key without its token (never stored, so never
        replayed), a conflict, or in progress. ``None`` when the reference is free."""
        held = await self._store.get_operation(admin.application_id, admin.actor_id, operation_ref)
        if held is None:
            return None
        return IssuedKey(key=ledger.repeat(held, fingerprint, _KEY), token=None, replayed=True)

    async def _taken(self, admin: ActorContext, record: OperationRecord) -> IssuedKey:
        """The store refused the key because another request took the reference just now: answer from it."""
        replay = await self._replayed(admin, record.operation_ref, record.fingerprint)
        assert replay is not None  # the store refuses a key only while the reference is held, and keeps it
        return replay

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


def _public(key: AccessKey) -> Any:
    return _KEY.dump_python(key, mode="json")  # the key as inspected: never its token or hash


def _validated_permissions(kind: CredentialKind, permissions: Iterable[str] | None) -> set[str]:
    if kind in ("admin", "auditor"):
        return set()  # a credential kind, not permission strings, decides what these may do
    granted = set(DEFAULT_ACTOR_PERMISSIONS if permissions is None else permissions)
    known = {operation.value for operation in Operation}
    unknown = granted - known
    if unknown:
        raise InvalidRequest(f"unknown permissions: {sorted(unknown)}")
    if Operation.ACTORS_ASSERT in granted and kind != "service":
        raise InvalidRequest("actors.assert is only valid on a service credential")
    return granted
