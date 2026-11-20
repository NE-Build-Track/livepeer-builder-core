"""Payment contracts: explicit allotment, allowance, and whether an advertised price can be paid
(docs/contracts/payments.md)."""

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Literal

from pydantic import Field, SecretStr, computed_field, field_validator

from livepeer_builder.contracts.access import IssuedKey
from livepeer_builder.contracts.common import (
    AllotAmount,
    Contract,
    Currency,
    Money,
    StoredText,
    TopUpAmount,
)
from livepeer_builder.contracts.operations import OperationRef

if TYPE_CHECKING:
    from livepeer_builder.contracts.discovery import PriceInfo

AllocationStatus = Literal["active", "paused", "exhausted", "revoked"]
# How a clearinghouse's payment key begins (D87, #166). A public prefix, not a secret: an access key is
# lpk_ and a Batteries key lpg_.
PAYMENT_KEY_PREFIX = "lpp_"


class PaymentKey(Contract):
    """The key that pays for one actor's call, sent as the signer's bearer, and the allocation it spends."""

    token: str | None
    allocation_id: str | None


class SecretRef(Contract):
    """A handle to a secret held by a SecretStore. Databases, records and logs hold only this."""

    ref: str = Field(min_length=1)  # opaque, and read back from stored rows: no length rule here


class AllotRequest(Contract):
    """Explicit, admin-authorized allotment. There is no automatic allotment. The allocation is allotted
    from the tenant's grant, whose id is the tenant's id (D83): the request names no grant."""

    # ``StoredText``: allotment funds a provider key before it saves the actor, so an id or a label
    # the store could not keep is refused here, before any provider call.
    tenant_id: StoredText
    actor_id: StoredText
    # may be zero: identity without money; top up later with an explicit command. Never negative, and within
    # the provider's format and the engine's bound (#112, C11)
    amount: AllotAmount
    label: StoredText = ""
    operation_ref: OperationRef  # required (D57, Q4): a repeat is answered from the operation ledger


class GrantRecord(Contract):
    """A grant as the payment provider keeps it. Its id is its tenant's id (D83)."""

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


TenantStatus = Literal["active", "paused"]  # what an admin sets; a grant is never closed (D82)


class TenantGrant(Contract):
    """A tenant with its grant, as the payment provider reports it (D83). The tenant's id is the grant's
    id. ``unallocated`` is what is left to allot; the provider, not the engine, refuses an allocation
    larger than it. ``spent`` is the tenant's payment usage: what its allocations have spent, the revoked
    ones included (#166)."""

    tenant_id: str
    name: str
    status: str  # the provider's word: active or paused (draft or closed for a grant made elsewhere)
    total: Money
    unallocated: Money
    spent: Money


class OnboardedTenant(Contract):
    """The answer to onboarding a tenant (D83, #165): the tenant, whose id is the id of the grant the
    provider created for it, and its first tenant admin key. The key's token is in the answer to the
    first successful call only: the engine keeps its hash, so a repeat of the reference answers
    ``token: null`` and ``replayed: true``, as issuing any access key does."""

    tenant_id: str
    name: str
    admin_key: IssuedKey | None  # None when engine access keys are off (LPB_AUTH=jwt)


class TenantState(Contract):
    """The answer to pausing or resuming a tenant: the status its grant now has."""

    tenant_id: str
    status: TenantStatus


class EscrowAccount(Contract):
    """One on-chain escrow account of the clearinghouse's wallet, as the payment provider reports it
    (#167): the TicketBroker deposit and reserve of one sender on one chain. Both are in ETH, exact to
    the wei. They are what the provider's on-chain listener has confirmed through
    ``confirmed_through_block``, not the chain's head: the engine reads no chain itself.

    ``sender`` is the wallet's public address, which the provider already reports. No key material, no
    keystore path and no RPC address is here."""

    chain_id: str
    contract_address: str
    sender: str
    deposit: Money
    reserve: Money
    confirmed_through_block: int  # -1: the provider's first snapshot, from before the chain's first block


class UnspentGrants(Contract):
    """What the tenants whose grants are in one currency may still spend (#167): for every grant the
    provider holds in that currency, whatever its status, the grant's total less what its allocations
    have spent, and never less than zero for one grant. ``grants`` is how many grants were summed.
    Nothing is converted: a deployment with grants in two currencies has two of these."""

    unspent: Money
    grants: int


# Why a signer is not ready. "not_configured": the engine has no signer address. "unavailable": the
# signer could not be reached, timed out or answered a 5xx. "no_identity": it answered, but not with its
# address and a signature.
SignerNotReady = Literal["not_configured", "unavailable", "no_identity"]


class SignerReadiness(Contract):
    """Whether the signer answered its identity call just now (#167): the unauthenticated call every
    payment begins with. ``ready`` says the signer is reachable where the engine calls it and signed
    with the wallet's key. It does not say the wallet is funded, that the signer's price feed or event
    stream is up, or that it can reach the payment provider: those are the signer's own ``/readyz``, on
    a listener the engine is not given."""

    ready: bool
    reason: SignerNotReady | None = None  # None when ready


class WalletReport(Contract):
    """What a clearinghouse admin watches the wallet by (D82, #167): the deposit and reserve, the total
    of unspent grants, and the signer's readiness. Each amount is in its own currency. Nothing is
    converted between ETH and a grant's currency, and no margin is computed: the report states, and a
    person judges."""

    escrow: tuple[EscrowAccount, ...]  # empty: the provider follows no escrow account (its listener is off)
    unspent_grants: tuple[UnspentGrants, ...]  # one for each currency a grant is in, by currency
    signer: SignerReadiness


class ActorAllocation(Contract):
    tenant_id: str
    actor_id: str
    # The provider's grant the allocation was allotted from: the tenant's id, for every allocation
    # allotted since a tenant and its grant share one id (D83, #165). A record written before that
    # keeps the grant it names.
    grant_id: str
    allocation_id: str
    api_key_id: str
    secret_ref: SecretRef
    currency: Currency
    status: AllocationStatus
    created_at: datetime
    updated_at: datetime
    label: str = Field(default="", max_length=200)  # a readable name beside an opaque actor_id (D46)


class IssuedPaymentKey(Contract):
    """An actor's payment key, as the command that issued it answers (D87, #166). The clearinghouse keeps
    the key's hash and never the key, so the token is in the answer of the run that issued the key and
    nowhere else. A repeat of the command's reference, or a run that found the key already issued,
    answers ``token: null`` and ``replayed: true``, as issuing an access key does."""

    token: str | None  # ``lpp_...``, shown once
    replayed: bool = False


class KeyedAllocation(ActorAllocation):
    """The answer to allotting an allocation and to rotating its payment key: the allocation, and the
    payment key the command issued for its actor. An enterprise's engine presents that key when it asks
    the clearinghouse to sign a payment for the actor, and stores it where it stores a Batteries key
    today. ``payment_key`` is ``None`` when the store keeps no payment keys (a supplied store without
    ``PaymentKeyRepo``)."""

    payment_key: IssuedPaymentKey | None = None

    @property
    def allocation(self) -> ActorAllocation:
        """The allocation as the store keeps it and every read returns it: without the payment key."""
        return ActorAllocation.model_validate(self.model_dump(exclude={"payment_key"}))


SignerCall = Literal["sign-orchestrator-info", "generate-live-payment"]
"""The two calls the gateway SDK makes to a signer, by the path of each: who the signer is, which signs
no payment, and one payment."""


class SignerAnswer(Contract):
    """What a signer answered one call with, as it answered (#166): the status, the body as bytes and its
    content type. Nothing of it is parsed: a clearinghouse hands it back to the engine that asked."""

    status_code: int
    content: bytes
    content_type: str = ""


class SeenActor(Contract):
    """A person the identity provider signed in as an actor (#52, D48), known before any allocation.

    ``name`` is the provider's latest display name (``LPB_JWT_NAME_CLAIM``); the actor itself is the
    immutable ``sub``, so a renamed person keeps their actor and a reused name never inherits one.
    """

    tenant_id: str
    actor_id: str
    name: str = Field(default="", max_length=200)
    first_seen_at: datetime
    last_seen_at: datetime


class KnownActor(Contract):
    """One row of the operator's actor list (D46, D48): allotted, signed in, or both."""

    tenant_id: str
    actor_id: str
    label: str  # the operator's label, else the name the provider gave, else ""
    name: str | None  # the provider's latest name; None until the person signs in with a token
    allocation: ActorAllocation | None  # None until an operator allots one
    first_seen_at: datetime | None
    last_seen_at: datetime | None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def allotted(self) -> bool:
        return self.allocation is not None


class TopUpRequest(Contract):
    """A top-up of an allocation or of a tenant's grant. The path names which: the body names no grant."""

    amount: TopUpAmount  # the provider's format and the engine's bound (#112, C11)
    operation_ref: OperationRef  # required: makes a retried top-up safe


class Allowance(Contract):
    """The allocation as the provider reports it. A cap that can overshoot: funds are not reserved."""

    allocation_id: str
    status: str
    granted: Money | None
    spent: Money | None
    available: Money | None


class AllocationRead(ActorAllocation):
    """The answer to reading one actor's allocation (#168): the allocation's record, and its amounts as
    the payment provider reports them now. ``allowance`` is what the allowance read answers for the
    same actor, by the same read of the provider: what was allotted (``granted``), what is spent, what
    is available, and the provider's status. An enterprise's engine reads an actor's allowance here.

    ``allowance`` is ``None`` when the provider could not be read: unknown, never zero. The record is
    the engine's own and is answered either way."""

    allowance: Allowance | None = None

    @property
    def allocation(self) -> ActorAllocation:
        """The allocation as the store keeps it: without the amounts."""
        return ActorAllocation.model_validate(self.model_dump(exclude={"allowance"}))


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
