"""Payment contracts: allocation modes, explicit provisioning, allowance (docs/contracts/payments.md)."""

from datetime import datetime
from typing import Literal

from pydantic import Field, SecretStr

from livepeer_builder.contracts.common import Contract, Currency, Money

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
    label: str = ""
    grant_id: str = ""  # empty uses the grant of the caller's own allocation


class GrantRecord(Contract):
    """A Batteries grant the operator can fund actors from."""

    id: str
    name: str
    status: str
    currency: str
    total: str


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
