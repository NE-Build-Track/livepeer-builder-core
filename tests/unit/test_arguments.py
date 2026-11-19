"""An in-process call's argument, checked by the REST body's rule, is refused as ``InvalidRequest`` (#106
review): the message names the field and the rule, never the value, and chains nothing that holds it."""

from decimal import Decimal

import pytest
from pydantic import TypeAdapter

from livepeer_builder.contracts import Money
from livepeer_builder.contracts.common import StoredText, TopUpAmount
from livepeer_builder.errors import InvalidRequest
from livepeer_builder.services.arguments import checked

VALUE = "throwaway-value"


def refusal(adapter: TypeAdapter[object], value: object, field: str) -> InvalidRequest:
    with pytest.raises(InvalidRequest) as refused:
        checked(adapter, value, field)
    assert refused.value.__cause__ is None and refused.value.__context__ is None
    return refused.value


def test_a_value_the_rule_takes_is_returned_as_the_rule_reads_it() -> None:
    amount = Money(amount=Decimal("5"), currency="USD")
    assert checked(TypeAdapter(TopUpAmount), amount, "amount") == amount


def test_a_validators_own_words_are_kept_without_pydantics_prefix() -> None:
    amount = Money(amount=Decimal(10**10), currency="USD")
    refused = refusal(TypeAdapter(TopUpAmount), amount, "amount")
    assert str(refused) == "amount: amount must be at most 1000000000"


def test_a_constraint_is_stated_and_the_value_is_not() -> None:
    refused = refusal(TypeAdapter(StoredText), VALUE * 20, "name")
    assert str(refused) == "name: String should have at most 200 characters"
    assert VALUE not in str(refused)
