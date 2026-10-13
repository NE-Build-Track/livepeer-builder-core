"""Base model and shared value types for every contract that crosses the engine boundary."""

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator


class Contract(BaseModel):
    """Frozen, strict base: the same class is used in-process and as the REST/MCP schema."""

    model_config = ConfigDict(frozen=True, extra="forbid")


Currency = Literal["USD", "ETH"]


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
