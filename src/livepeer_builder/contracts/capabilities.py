"""Public operation descriptions, without runner addresses or workflow details."""

from typing import Literal

from pydantic import Field, JsonValue

from livepeer_builder.contracts.common import Contract


class CapabilityProfile(Contract):
    id: str = Field(min_length=1)
    app: str = Field(min_length=1)
    model: str = Field(min_length=1)
    deployment_revision: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    method: Literal["POST"] = "POST"
    path: str = Field(pattern=r"^/v1/[a-zA-Z0-9_/-]+$")
    content_type: Literal["application/json", "multipart/form-data"]
    input_schema: dict[str, JsonValue]
    defaults: dict[str, JsonValue]
    schema_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
