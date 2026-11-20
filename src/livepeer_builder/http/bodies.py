"""The REST request bodies: what a route parses before it calls the engine. A 422 here moves no money."""

import json
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from livepeer_builder import contracts
from livepeer_builder.contracts import AllocationStatus, CredentialKind, OperationRef
from livepeer_builder.contracts.common import AllotAmount, GrantId, StoredText, TopUpAmount

_BASE64 = TypeAdapter(bytes, config=ConfigDict(val_json_bytes="base64"))


class Body(BaseModel):
    """A request body that refuses a field it does not know (#163 review): a client that still sends an
    old name, or misspells one, gets a 422 naming it, never an answer as if it had not sent it.
    ``tests/structure/test_request_names.py`` keeps every route's body so."""

    model_config = ConfigDict(extra="forbid")


# The contract's JobRequest as a REST body, under the same schema name (no docstring: it would become
# the schema's description). FastAPI validates the parsed JSON as Python, where ``raw_body`` takes only
# bytes, so its URL-safe base64 is decoded here, exactly as ``JobRequest.model_validate_json`` would (#55).
class JobRequest(contracts.JobRequest):
    @field_validator("raw_body", mode="before")
    @classmethod
    def _decoded(cls, value: object) -> object:
        return _BASE64.validate_json(json.dumps(value)) if isinstance(value, str) else value


class IssueKeyBody(Body):
    kind: CredentialKind = "actor"
    actor_id: Annotated[StoredText, Field(min_length=1)]  # a key for nobody could never authenticate
    tenant_id: Annotated[StoredText, Field(min_length=1)]  # required: no key lands in a tenant unnamed
    permissions: list[str] | None = None
    label: StoredText = ""
    operation_ref: OperationRef  # required (D57, Q4)


class AllotBody(Body):
    amount: AllotAmount  # as AllotRequest: a 422 before money moves (#112, C11)
    label: StoredText = ""  # ActorAllocation.label; a 422 before money moves
    grant_id: GrantId = ""  # as stored, and one provider path segment: a 422 before money moves (#113, C10)
    operation_ref: OperationRef  # required (D57, Q4)


class GrantBody(Body):
    name: str
    amount: TopUpAmount  # a 422 before the ledger and the provider (#112, C11)
    operation_ref: OperationRef  # required (D57, Q4)


class StatusBody(Body):
    status: AllocationStatus


class RotateKeyBody(Body):
    label: StoredText | None = None  # keep the key's label unless given
    operation_ref: OperationRef  # required (D57, Q4)


class RotateBody(Body):
    operation_ref: OperationRef  # required (D57, Q4)


class UsageBody(Body):
    quantities: dict[str, int]
