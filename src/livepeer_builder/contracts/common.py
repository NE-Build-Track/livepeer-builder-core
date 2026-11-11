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
# is after the provider key was funded. So it is refused where the request is parsed.
StoredText = Annotated[str, Field(max_length=200), AfterValidator(_no_nul)]


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
