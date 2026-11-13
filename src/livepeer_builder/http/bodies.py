"""The REST request bodies: what a route parses before it calls the engine. A 422 here moves no money."""

import json
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from livepeer_builder import contracts
from livepeer_builder.contracts import AllocationStatus, CredentialKind, OperationRef
from livepeer_builder.contracts.common import FundingAmount, ProvisionAmount, StoredText

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
    application_id: Annotated[StoredText, Field(min_length=1)] = "default"
    permissions: list[str] | None = None
    label: StoredText = ""
    operation_ref: OperationRef  # required (D57, Q4)


class ProvisionBody(BaseModel):
    amount: ProvisionAmount  # as ProvisionRequest: a 422 before money moves (#112, C11)
    label: StoredText = ""  # ProvisionedActor.label; a 422 before money moves
    grant_id: StoredText = ""  # as stored: also a 422 before money moves
    operation_ref: OperationRef  # required (D57, Q4)


class GrantBody(BaseModel):
    name: str
    amount: FundingAmount  # a 422 before the ledger and the provider (#112, C11)
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
