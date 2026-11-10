"""Read a livepeer/node ``GET /discovery`` document at the vendored schema pin (D63).

The schema is ``docs/discovery.schema.json`` at ``f45d83fd68f73d195dffe8b45f6cac296d023c49``.
Formats and integers are judged as ``discovery/schema_test.go`` judges them there
(``discovery_formats``, held to node's own verdicts). A record that fails is skipped. A body that is
not a JSON array fails the refresh. No other rule is applied: there is no size cap, no stamp-age filter
and no de-duplication.
"""

import json
import logging
import re
from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from importlib.resources import files
from typing import Any, cast

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError
from jsonschema.protocols import Validator
from jsonschema.validators import extend

from livepeer_builder.adapters.discovery_formats import int64_value, is_date_time, is_integer_token, is_uri
from livepeer_builder.contracts.discovery import DISCOVERY_CONTRACT, PriceInfo, Runner, RunnerGPU

_LOG = logging.getLogger(__name__)
_SCHEMA = json.loads(files("livepeer_builder.contracts").joinpath("discovery.schema.json").read_text())
_PIN = json.loads(files("livepeer_builder.contracts").joinpath("discovery.schema.pin.json").read_text())
_DEFS = _SCHEMA["$defs"]
_FRACTION = re.compile(r"(\.\d{6})\d+")


class JsonNumber:
    """One JSON number token, never a binary float. ``Decimal`` may still be unable to hold it."""

    def __init__(self, token: str) -> None:
        self.token = token

    def __repr__(self) -> str:
        return self.token


def discovery_contract() -> str:
    """The pin ``/readyz`` reports, for example ``livepeer/node@f45d83f``."""
    return DISCOVERY_CONTRACT


def schema_pin() -> Mapping[str, str]:
    """The vendored pin: repository, path, commit and sha256."""
    return cast(Mapping[str, str], _PIN)


def schema_document() -> Any:
    """The vendored schema document."""
    return _SCHEMA


def _is_number(checker: object, instance: object) -> bool:
    del checker
    if isinstance(instance, bool):
        return False
    if isinstance(instance, int | float):
        return True
    return isinstance(instance, JsonNumber)


def _is_integer(checker: object, instance: object) -> bool:
    del checker
    if isinstance(instance, (bool, float)):
        return False
    if isinstance(instance, int):
        return True
    return isinstance(instance, JsonNumber) and is_integer_token(instance.token)


_Checker = Draft202012Validator.TYPE_CHECKER.redefine("number", _is_number).redefine("integer", _is_integer)
_Validator = extend(Draft202012Validator, type_checker=_Checker)


_FORMATS = FormatChecker()
_FORMATS.checks("date-time")(is_date_time)
_FORMATS.checks("uri")(is_uri)


def _validator(schema: Mapping[str, Any]) -> Validator:
    document = {"$schema": _SCHEMA["$schema"], "$defs": _DEFS, **schema}
    return _Validator(document, format_checker=_FORMATS)


_DOCUMENT = _Validator(_SCHEMA, format_checker=_FORMATS)
_ENTRY = _validator({"oneOf": _SCHEMA["items"]["oneOf"]})
_ADDRESS = _validator({"$ref": "#/$defs/baseURL"})
_RUNNER = _validator({"$ref": "#/$defs/runner"})


def loads_inventory(raw: str | bytes | bytearray) -> Any:
    """``json.loads`` with every number kept as its token. ``NaN`` and ``Infinity`` are not JSON."""
    return json.loads(raw, parse_int=JsonNumber, parse_float=JsonNumber, parse_constant=_not_json)


def _not_json(token: str) -> Any:
    raise ValueError(f"{token} is not a JSON number")


def document_is_valid(raw: str | bytes | bytearray) -> bool:
    """Whether the schema accepts the whole document. A non-array, or invalid JSON, does not."""
    try:
        value = loads_inventory(raw)
    except ValueError:
        return False
    return bool(_DOCUMENT.is_valid(value))


def read_inventory(raw: str | bytes | bytearray) -> tuple[Runner, ...]:
    """Runners the schema accepts. A body that is not a JSON array raises ``ValueError``.

    Each other failure skips that entry or that runner and is counted by reason. ``[]`` is an empty
    inventory. A number ``Decimal`` cannot hold is still a number: the runner is kept, and
    ``payable_rate`` reports that its price cannot be paid.
    """
    entries = loads_inventory(raw)
    if not isinstance(entries, list):
        raise ValueError("discovery response is not a JSON list")
    runners: list[Runner] = []
    reasons: Counter[str] = Counter()
    for entry in entries:
        if _ENTRY.is_valid(entry) and _metadata(entry):
            continue
        if (
            isinstance(entry, dict)
            and _ADDRESS.is_valid(entry.get("address"))
            and isinstance(entry.get("runners"), list)
        ):
            for record in entry["runners"]:
                if _RUNNER.is_valid(record):
                    runners.append(_runner(record, entry["address"]))
                else:
                    reasons[_reason(_RUNNER, record)] += 1
            continue
        reasons[_reason(_ENTRY, entry)] += 1
    if reasons:
        _LOG.warning(
            "discovery inventory had malformed records",
            extra={"event": "discovery.inventory_skipped", "reasons": dict(reasons)},
        )
    return tuple(runners)


def _metadata(entry: object) -> bool:
    return isinstance(entry, dict) and "nodes" in entry and "address" not in entry


def _reason(validator: Validator, instance: object) -> str:
    error = next(validator.iter_errors(instance), None)
    if error is None:
        return "invalid"
    keyword = error.validator if isinstance(error, ValidationError) else "invalid"
    return str(keyword)


def _runner(raw: Mapping[str, Any], address: str) -> Runner:
    gpu = raw.get("gpu")
    return Runner(
        url=raw["url"],
        app=raw["app"],
        mode=_string(raw.get("mode")),
        orchestrator_url=address,
        capacity=_integer(raw.get("capacity")),
        capacity_used=_integer(raw.get("capacity_used")),
        capacity_available=_integer(raw.get("capacity_available")),
        price_info=_price_info(raw.get("price_info")),
        observed_at=_stamp(raw.get("observed_at")),
        version=_string(raw.get("version")),
        metadata=_string(raw.get("metadata")),
        gpu=_gpu(gpu) if isinstance(gpu, dict) else None,
    )


def _price_info(value: object) -> PriceInfo | None:
    if not isinstance(value, dict):
        return None
    return PriceInfo(
        price=_decimal(value.get("price")),
        price_usd=_decimal(value.get("price_usd")),
        currency=_string(value.get("currency")),
        unit=_string(value.get("unit")),
    )


def _decimal(value: object) -> Decimal | None:
    """The exact token, or None when ``Decimal`` cannot hold it. Never a binary float."""
    if isinstance(value, JsonNumber):
        try:
            amount = Decimal(value.token)
        except InvalidOperation:
            return None
        return amount if amount.is_finite() else None
    if isinstance(value, int) and not isinstance(value, bool):
        return Decimal(value)
    return None


def _integer(value: object) -> int | None:
    """A schema integer as a signed 64-bit value. A larger one leaves the field absent; the runner stays."""
    if isinstance(value, JsonNumber):
        return int64_value(value.token)
    if isinstance(value, int) and not isinstance(value, bool) and -(1 << 63) <= value < 1 << 63:
        return value
    return None


def _string(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _stamp(value: object) -> datetime | None:
    """The RFC 3339 stamp in UTC. Nanoseconds truncate to microseconds. A stamp the schema allows and
    Python cannot hold (a leap second, year 0) is None, and the catalog uses the poll time (D62 (3))."""
    if not isinstance(value, str):
        return None
    text = value[:10] + "T" + value[11:] if value[10:11] in ("t", "T") else value
    text = text[:-1] + "+00:00" if text[-1:] in ("z", "Z") else text
    text = _FRACTION.sub(r"\1", text)
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        return None
    if stamp.utcoffset() is None:
        return None
    try:
        return stamp.astimezone(UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _gpu(value: Mapping[str, Any]) -> RunnerGPU:
    return RunnerGPU(
        id=_string(value.get("id")),
        name=_string(value.get("name")),
        vram_mb=_integer(value.get("vram_mb")),
    )
