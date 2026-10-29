"""Payment contracts: allocation modes, explicit provisioning, allowance (docs/contracts/payments.md)."""

from datetime import datetime
from typing import Literal

from pydantic import Field, SecretStr, computed_field

from livepeer_builder.contracts.common import Contract, Currency, Money
from livepeer_builder.contracts.operations import OperationRef

AllocationMode = Literal["per_application", "per_actor"]
AllocationStatus = Literal["active", "paused", "exhausted", "revoked"]


class PaymentCredential(Contract):
    """The signer bearer for one call, and the allocation that bearer spends."""

    token: str | None
    allocation_id: str | None


class SecretRef(Contract):
    """A handle to a secret held by a SecretStore. Databases, records and logs hold only this."""

    ref: str = Field(min_length=1)


class ProvisionRequest(Contract):
    """Explicit, admin-authorized provisioning. There is no automatic provisioning."""

    application_id: str
    actor_id: str
    amount: Money  # may be zero: identity without money; fund later with an explicit command
    label: str = Field(default="", max_length=200)  # as stored: refused before any provider call
    grant_id: str = ""  # empty uses the grant of the caller's own allocation
    operation_ref: OperationRef  # required (D57, Q4): a repeat is answered from the operation ledger


class GrantRecord(Contract):
    """A Batteries grant the operator can fund actors from."""

    id: str
    name: str
    status: str
    currency: str
    total: str
    # Batteries' grant metadata, when it is a JSON object of strings. The engine tags each grant it creates
    # with ``lpb_operation`` (D57, Q5), so a resumed create finds it instead of creating a second one.
    metadata: dict[str, str] = Field(default_factory=dict)


class GrantPage(Contract):
    """One page of grants, oldest first; ``next_cursor`` is ``None`` on the last page (D57)."""

    grants: tuple[GrantRecord, ...]
    next_cursor: str | None = None


class ProvisionedActor(Contract):
    application_id: str
    actor_id: str
    grant_id: str
    allocation_id: str
    api_key_id: str
    secret_ref: SecretRef
    currency: Currency
    status: AllocationStatus
    created_at: datetime
    updated_at: datetime
    label: str = Field(default="", max_length=200)  # a readable name beside an opaque actor_id (D46)


class SeenActor(Contract):
    """A person the identity provider signed in as an actor (#52, D48), known before any allocation.

    ``name`` is the provider's latest display name (``LPB_JWT_NAME_CLAIM``); the actor itself is the
    immutable ``sub``, so a renamed person keeps their actor and a reused name never inherits one.
    """

    application_id: str
    actor_id: str
    name: str = Field(default="", max_length=200)
    first_seen_at: datetime
    last_seen_at: datetime


class KnownActor(Contract):
    """One row of the operator's actor list (D46, D48): provisioned, signed in, or both."""

    application_id: str
    actor_id: str
    label: str  # the operator's label, else the name the provider gave, else ""
    name: str | None  # the provider's latest name; None until the person signs in with a token
    allocation: ProvisionedActor | None  # None until an operator provisions the actor
    first_seen_at: datetime | None
    last_seen_at: datetime | None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def provisioned(self) -> bool:
        return self.allocation is not None


class FundRequest(Contract):
    amount: Money
    operation_ref: str = Field(min_length=1, max_length=200)  # required: makes a retried fund safe
    grant_id: str = ""  # grant funding only; allocation funding ignores it


class Allowance(Contract):
    """The allocation as the provider reports it. A cap that can overshoot: funds are not reserved."""

    allocation_id: str
    status: str
    granted: Money | None
    spent: Money | None
    available: Money | None


class NewAllocationKey(Contract):
    """What the provider returns when it mints a key. The secret goes straight to a SecretStore."""

    allocation_id: str
    api_key_id: str
    secret: SecretStr
