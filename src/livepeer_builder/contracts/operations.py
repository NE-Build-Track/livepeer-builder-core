"""The operation ledger's record: one repeat-safe admin command (D57).

See docs/contracts/payments.md#repeat-safe-commands.

An admin command that moves money or creates a credential is identified by a caller-chosen
``operation_ref``, scoped to the caller ``(application_id, actor_id)``. The ledger stores it with the
command's fingerprint before the first provider call (``started``) and the public result after the
last step (``done``). A repeat is answered from this record. It never holds a secret: ``context`` holds
what a resume needs (key ids, secret references), ``result`` what the caller was answered, without a token.
"""

from contextvars import ContextVar
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import Field

from livepeer_builder.contracts.common import Contract

OperationCommand = Literal[
    "provision",
    "fund",
    "fund_grant",
    "create_grant",
    "rotate_payment_key",
    "issue_key",
    "rotate_access_key",
]
OperationState = Literal["started", "done"]
OperationRef = Annotated[str, Field(min_length=1, max_length=200)]  # required on every admin command (Q4)


class OperationRecord(Contract):
    application_id: str  # the caller's scope, with actor_id and operation_ref: unique together
    actor_id: str
    operation_ref: OperationRef
    command: OperationCommand
    fingerprint: str  # sha256 of the command, its target and the validated request, the reference excluded
    state: OperationState
    fencing_token: int = Field(default=1, ge=1)  # +1 each time a stale run is resumed; guards every write
    context: dict[str, str] = Field(default_factory=dict)  # what a resume needs; never a secret
    result: Any = None  # the public result as JSON once done; never a token
    started_at: datetime
    completed_at: datetime | None = None


OPERATION_REPLAYED: ContextVar[bool] = ContextVar("lpb_operation_replayed", default=False)
"""Whether the last admin command run in this context was answered from the ledger (a replay). Read it
right after the call: the REST service turns it into ``Idempotent-Replayed: true``. Each command resets it."""
