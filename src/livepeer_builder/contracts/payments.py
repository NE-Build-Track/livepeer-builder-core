"""Payment contracts: allocation modes, explicit provisioning, allowance, and whether an advertised price can
be paid (docs/contracts/payments.md)."""

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Literal

from pydantic import Field, SecretStr, computed_field, field_validator

from livepeer_builder.contracts.common import Contract, Currency, Money
from livepeer_builder.contracts.operations import OperationRef

if TYPE_CHECKING:
    from livepeer_builder.contracts.discovery import PriceInfo

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


# Whether an advertised price can be paid (D63 (4)). The discovery reader keeps price_info as advertised;
# the catalog and the transport ask here, and max_price (adapters/runner_price.py) builds the signer's
# ceiling from the result.

# max_price pads a rate into a float ceiling for the signer; a larger price overflows or is no float.
PRICE_LIMIT = Decimal("1e300")
# The signer refuses a ceiling that is not positive. Below the normal float range the padded float rounds
# to 0.0, or below the price itself (7e-324 pads to 5e-324). 0 itself is a price (free): it needs no
# ceiling, and the engine never signs for it.
PRICE_FLOOR = Decimal("1e-300")
PRICE_RULE = "price must be 0, or from 1e-300 to below 1e300"
# The ceilings livepeer-node's signer accepts (signer/service.go, checkMaxPrice): wei, per second for a live
# payment or per request for a fixed one. Each unit the engine pays, and the payment type it is signed as.
PAYMENT_CURRENCY = "wei"
PAYMENT_TYPES_BY_UNIT = {"seconds": "live", "fixed": "fixed"}


def is_price(amount: Decimal) -> bool:
    """0 (free), or a positive price that pads into a float ceiling above it."""
    return amount.is_finite() and (amount == 0 or PRICE_FLOOR <= amount < PRICE_LIMIT)


def is_payable(currency: str, unit: str) -> bool:
    """The signer takes a ceiling in this currency and unit; it refuses any other (HTTP 400)."""
    return currency == PAYMENT_CURRENCY and unit in PAYMENT_TYPES_BY_UNIT  # NetworkRate normalizes both


def signable(amount: Decimal, currency: str, unit: str) -> bool:
    """The signer takes a ceiling for this price: a positive one (a free price needs none, and a 0.0 ceiling
    is a 400), in a currency and unit it takes. The engine signs for nothing else."""
    return amount > 0 and is_payable(currency, unit)


class NetworkRate(Contract):
    """A rate exactly as advertised: a wholesale network price per unit, not a job quote or a retail price."""

    amount: Decimal  # livepeer-node's price: wei on-chain. PRICE_RULE, from any source
    currency: str  # "wei" on-chain
    unit: str  # as advertised; the engine pays only "seconds" and "fixed"
    # livepeer-node's price_usd: the runner's registered USD per the same unit. Not an engine conversion;
    # absent from go-livepeer orchestrators. The wei amount is what is paid.
    price_usd: Decimal | None = None
    # who stated the rate; a field, so a later price oracle or operator override would be explicit
    source: Literal["orchestrator"] = "orchestrator"

    @field_validator("amount")
    @classmethod
    def _a_price(cls, amount: Decimal) -> Decimal:
        if not is_price(amount):
            raise ValueError(PRICE_RULE)
        return Decimal(0) if amount == 0 else amount  # -0 and 0E-10 are 0

    @field_validator("currency", "unit")
    @classmethod
    def _normalized(cls, value: str) -> str:
        return (
            value.strip().lower()
        )  # once, here: grouping, is_payable and the signer's ceiling all compare it


def payable_rate(info: "PriceInfo | None") -> NetworkRate | None:
    """The advertised price as a rate the catalog and the signer can use, or None when it cannot be paid."""
    if info is None or info.price is None or not info.currency or not info.unit:
        return None
    try:
        return NetworkRate(
            amount=info.price,
            currency=info.currency,
            unit=info.unit,
            price_usd=info.price_usd,
        )
    except ValueError:
        return None
