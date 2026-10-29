"""The REST request bodies: what a route parses before it calls the engine. A 422 here moves no money."""

import json

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from livepeer_builder import contracts
from livepeer_builder.contracts import AllocationStatus, CredentialKind, Money

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
    actor_id: str
    application_id: str = "default"
    permissions: list[str] | None = None
    label: str = ""


class ProvisionBody(BaseModel):
    amount: Money
    label: str = Field(default="", max_length=200)  # ProvisionedActor.label; a 422 before money moves
    grant_id: str = ""


class GrantBody(BaseModel):
    name: str
    amount: Money


class StatusBody(BaseModel):
    status: AllocationStatus


class RotateKeyBody(BaseModel):
    label: str | None = None  # keep the key's label unless given


class RotateBody(BaseModel):
    operation_ref: str = Field(min_length=1, max_length=200)


class UsageBody(BaseModel):
    quantities: dict[str, int]
