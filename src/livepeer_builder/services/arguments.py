"""An in-process call's arguments, checked by the rule the REST body applies (#106 review).

A REST body is checked where it is parsed: an amount the provider would not take is a 422 before the
command runs (#112, C11). An in-process caller hands the command the value itself, so the command checks
it by the same rule and refuses it as its other checks do, with ``InvalidRequest`` (400). The message
names the field and the rule, never the value, and pydantic's ``ValidationError``, which holds the value,
is neither raised nor chained.
"""

from collections.abc import Mapping
from typing import Any

from pydantic import TypeAdapter, ValidationError

from livepeer_builder.errors import InvalidRequest


def checked[T](adapter: TypeAdapter[T], value: object, field: str) -> T:
    """``value`` as ``adapter`` reads it, or ``InvalidRequest`` naming ``field`` and each rule it breaks."""
    try:
        return adapter.validate_python(value)
    except ValidationError as refused:
        rules = "; ".join(_rule(detail) for detail in refused.errors(include_url=False))
    raise InvalidRequest(f"{field}: {rules}")  # outside the except block: nothing chains the value


def _rule(detail: Mapping[str, Any]) -> str:
    """A validator's own words, without pydantic's "Value error, " prefix; otherwise pydantic's message,
    which states the constraint and not the input."""
    error = detail.get("ctx", {}).get("error")
    return str(error) if error is not None else str(detail["msg"])
