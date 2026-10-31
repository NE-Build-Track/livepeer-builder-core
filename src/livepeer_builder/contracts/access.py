"""Access contracts: the validated actor, operations, and engine-issued keys (docs/contracts/access.md)."""

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import Field

from livepeer_builder.contracts.common import Contract


class Operation(StrEnum):
    """Every operation the AccessPolicy can be asked about. Permission strings equal these values."""

    DISCOVER = "discover"
    JOBS_RUN = "jobs.run"
    JOBS_READ = "jobs.read"
    COSTS_READ = "costs.read"
    EVENTS_READ = "events.read"
    ADMIN_KEYS = "admin.keys"
    ADMIN_ALLOCATIONS = "admin.allocations"
    ADMIN_COSTS = "admin.costs"
    ADMIN_READ = "admin.read"  # admin reads of one application: keys, actors, reconciliation (D46)
    ACTORS_ASSERT = "actors.assert"


DEFAULT_ACTOR_PERMISSIONS: frozenset[str] = frozenset(
    {Operation.DISCOVER, Operation.JOBS_RUN, Operation.JOBS_READ, Operation.COSTS_READ}
)

CredentialKind = Literal["actor", "admin", "auditor", "service"]  # auditor: a read-only admin (D46)


class AuthProvenance(Contract):
    """How the actor was established. Audit only: never a secret."""

    method: str  # "api_key", "jwt", "in_process", ...
    issuer: str | None = None
    credential_id: str | None = None


class ActorContext(Contract):
    """The only identity the core sees. Built by an Authenticator, or in-process by an importing app."""

    actor_id: str = Field(min_length=1)
    application_id: str = Field(default="default", min_length=1)
    permissions: frozenset[str] = frozenset()
    credential_kind: CredentialKind = "actor"
    attributes: dict[str, str] = Field(default_factory=dict)  # copied onto records; never secrets
    auth: AuthProvenance | None = None
    # The name the identity provider gives this person (LPB_JWT_NAME_CLAIM, D48), for the operator's actor
    # list only: never copied onto records, never an identity (the actor is actor_id).
    display_name: str | None = None


class AuthRequest(Contract):
    """What an Authenticator sees: the whole request, so any scheme fits without core changes."""

    method: str
    path: str
    headers: dict[str, str]  # lower-cased names
    client_address: str | None = None

    def header(self, name: str) -> str | None:
        return self.headers.get(name.lower())


class ResourceRef(Contract):
    """What an operation touches, so a policy can check ownership."""

    kind: Literal["job", "actor", "allocation", "key", "costs"]
    application_id: str
    owner_actor_id: str | None = None
    id: str | None = None


class AccessKey(Contract):
    """An engine-issued key, as inspected. The token itself is never stored or returned again."""

    key_id: str
    kind: CredentialKind
    actor_id: str
    application_id: str
    permissions: tuple[str, ...]
    label: str
    created_at: datetime
    revoked_at: datetime | None


class IssuedKey(Contract):
    key: AccessKey
    token: str | None  # shown once; None when a repeat of the operation_ref replays the issue (D57)
    replayed: bool = False  # a repeat of the operation_ref: the issue is replayed, without its token (D57)
