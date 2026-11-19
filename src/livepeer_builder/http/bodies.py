"""The REST request bodies: what a route parses before it calls the engine. A 422 here moves no money."""

import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from livepeer_builder import contracts
from livepeer_builder.contracts import AllocationStatus, CredentialKind, OperationRef, TenantStatus
from livepeer_builder.contracts.common import AllotAmount, StoredText

_BASE64 = TypeAdapter(bytes, config=ConfigDict(val_json_bytes="base64"))


# The contract's JobRequest as a REST body, under the same schema name (no docstring: it would become
# the schema's description). FastAPI validates the parsed JSON as Python, where ``raw_body`` takes only
# bytes, so its URL-safe base64 is decoded here, exactly as ``JobRequest.model_validate_json`` would (#55).
class JobRequest(contracts.JobRequest):
    @field_validator("raw_body", mode="before")
    @classmethod
    def _decoded(cls, value: object) -> object:
        return _BASE64.validate_json(json.dumps(value)) if isinstance(value, str) else value


class IssueKeyBody(BaseModel):
    kind: CredentialKind = "actor"
    actor_id: Annotated[StoredText, Field(min_length=1)]  # a key for nobody could never authenticate
    tenant_id: Annotated[StoredText, Field(min_length=1)] = "default"
    permissions: list[str] | None = None
    label: StoredText = ""
    operation_ref: OperationRef  # required (D57, Q4)


# The path names the tenant, and a tenant's id is its grant's id (D83): a body that still names a grant
# or a tenant is a 422, never a field the engine ignores while money moves.
_NAMES_NO_TENANT = ConfigDict(extra="forbid")


class AllotBody(BaseModel):
    model_config = _NAMES_NO_TENANT
    amount: AllotAmount  # as AllotRequest: a 422 before money moves (#112, C11)
    label: StoredText = ""  # ActorAllocation.label; a 422 before money moves
    operation_ref: OperationRef  # required (D57, Q4)


class OnboardBody(BaseModel):
    model_config = _NAMES_NO_TENANT
    name: Annotated[StoredText, Field(min_length=1)]  # the tenant's name, and its grant's
    amount: AllotAmount  # the grant's first total; zero onboards a tenant to top up later
    admin_actor_id: Annotated[StoredText, Field(min_length=1)] = "tenant-admin"  # whose key the first is
    operation_ref: OperationRef  # required (D57, Q4)


class TenantStatusBody(BaseModel):
    status: TenantStatus  # active or paused: a grant is never closed (D82)
    operation_ref: OperationRef  # required: a late repeat never pauses a tenant resumed since


class TenantKeyBody(BaseModel):
    model_config = _NAMES_NO_TENANT
    kind: Literal["auditor", "tenant_admin"] = "auditor"  # tenant_admin: a clearinghouse admin's to issue
    actor_id: Annotated[StoredText, Field(min_length=1)]
    label: StoredText = ""
    operation_ref: OperationRef  # required (D57, Q4)


class StatusBody(BaseModel):
    status: AllocationStatus


class RotateKeyBody(BaseModel):
    label: StoredText | None = None  # keep the key's label unless given
    operation_ref: OperationRef  # required (D57, Q4)


class RotateBody(BaseModel):
    operation_ref: OperationRef  # required (D57, Q4)


class UsageBody(BaseModel):
    quantities: dict[str, int]
