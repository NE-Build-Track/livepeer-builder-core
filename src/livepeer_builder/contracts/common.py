"""Base model and shared value types for every contract that crosses the engine boundary."""

from decimal import Decimal
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator


class Contract(BaseModel):
    """Frozen, strict base: the same class is used in-process and as the REST/MCP schema."""

    model_config = ConfigDict(frozen=True, extra="forbid")


Currency = Literal["USD", "ETH"]


def _no_nul(value: str) -> str:
    if "\x00" in value:
        raise ValueError("must not contain a NUL character")
    return value


# Text a record keeps (an id, a label, a reference): at most 200 characters (``store.schema.ID``) and no
# NUL. SQLite takes anything; PostgreSQL refuses either when the row is written, which for provisioning
# is after the provider key was funded. So it is refused where the request is parsed. A lone surrogate
# (JSON's ``"\ud800"``) is refused there too, by pydantic itself: text with a length limit must be valid
# Unicode (``string_unicode``). ``tests/unit/test_stored_text.py`` pins that.
StoredText = Annotated[str, Field(max_length=200), AfterValidator(_no_nul)]


def _one_path_segment(value: str) -> str:
    if value in (".", "..") or any(c in "/\\?#%" or ord(c) < 32 or 127 <= ord(c) < 160 for c in value):
        raise ValueError(
            "must not contain '/', '\\', '?', '#', '%' or a control character, or be '.' or '..'"
        )
    return value


# A grant id: stored text that the payment provider's URL path reads as one segment, whatever a client or
# server normalizes (#113, C10). The engine places it in ``/v1/grants/{grant_id}/fund``, where ``..`` or a
# ``/`` would name another resource. It is held to no pattern such as a UUID: the provider chooses its ids.
GrantId = Annotated[StoredText, AfterValidator(_one_path_segment)]


class Money(Contract):
    """An exact amount in one currency. Serialized as a decimal string, never a float."""

    amount: Decimal
    currency: Currency

    @field_validator("amount")
    @classmethod
    def _finite(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("amount must be finite")
        return value

    @classmethod
    def zero(cls, currency: Currency) -> "Money":
        return cls(amount=Decimal(0), currency=currency)


# The payment provider's amount (Batteries v0.0.3, internal/units/eth.go and API.md): an unsigned decimal
# string with no exponent and at most 18 fractional digits. The engine sends ``format(amount, "f")``
# (adapters/batteries.py), so an amount refused here is one the provider would refuse after the ledger
# recorded the command, or one it would take as written (#112, C11).
AMOUNT_DECIMALS = 18
# The provider's own bound (256 digits once scaled) is effectively none, so the engine sets one: a single
# command never moves more than 10^9 USD or ETH (more than the ether in existence). It stops an amount
# typed in wei or with its digits run together, and with 18 places it keeps every amount within 28
# significant digits, exact in Python's default decimal context. A constant, not a setting.
MAX_AMOUNT = Decimal(10**9)


def _provider_amount(money: Money) -> Money:
    amount = money.amount
    if amount > MAX_AMOUNT:
        raise ValueError(f"amount must be at most {MAX_AMOUNT}")
    exponent = amount.as_tuple().exponent  # an int: Money is finite
    if isinstance(exponent, int) and exponent < -AMOUNT_DECIMALS:
        raise ValueError(f"amount must have at most {AMOUNT_DECIMALS} decimal places")
    return money


def _unsigned(money: Money) -> Money:
    if money.amount.is_signed():  # -0 too: the provider takes no sign
        raise ValueError("amount must be zero or more, with no sign")
    return money


# Funding and grants: within the provider's format and the engine's bound. That they are positive stays
# their command's rule (InvalidRequest, 400), as before.
FundingAmount = Annotated[Money, AfterValidator(_provider_amount)]
# Provisioning: zero or more (zero sets up an identity with no money), within the same format and bound.
ProvisionAmount = Annotated[Money, AfterValidator(_unsigned), AfterValidator(_provider_amount)]
