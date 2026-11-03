"""The discovery document: each runner object kept as the JSON text that was received.

The envelope is checked here. A runner's numbers stay as the orchestrator spelled them, so ``1e-999999``
is not parsed into a float and written back.
"""

import json
from collections.abc import Callable, Iterable, Sequence
from typing import Any, NewType

from pydantic import BaseModel, ConfigDict

from livepeer_builder.utils import _not_json, decimal_json_number

_WS = " \t\r\n"

RawJson = NewType("RawJson", str)
"""One JSON value, exactly the text the orchestrator sent."""


class InventoryEntry(BaseModel):
    """One orchestrator in ``GET /discovery``: the address we probed and its runner objects as received."""

    model_config = ConfigDict(frozen=True)

    address: str
    runners: tuple[RawJson, ...]


def runner_records(body: str | bytes | bytearray) -> list[RawJson]:
    """The runner objects in an orchestrator's ``GET /discovery`` body, in order, as received.

    A metadata entry, or an entry whose ``runners`` is null, contributes nothing. A ``runners`` value that
    is not a list is refused. A runner that is not an object is dropped. An object key that is not a string
    is refused, so ``[{1:2}]`` is not an empty success. ``NaN`` and ``Infinity`` are not JSON and raise
    ``ValueError``. A number ``Decimal`` cannot construct does too, so it is never re-served for every
    engine's refresh to trip on.
    """
    text = body.decode("utf-8-sig") if isinstance(body, bytes | bytearray) else body.removeprefix("\ufeff")
    # Same parser as the engine. A float parser turns 1e1000000000000000000 into inf and would keep the
    # token; the served document would then fail every consumer's refresh.
    decoder = json.JSONDecoder(parse_float=decimal_json_number, parse_constant=_not_json)
    index = _skip(text, 0)
    if index >= len(text) or text[index] != "[":
        raise ValueError("inventory is not a JSON list")
    index += 1
    found: list[RawJson] = []

    def element(text: str, index: int) -> int:
        if text[index] == "{":
            records, index = _entry(text, index, decoder)
            found.extend(records)
            return index
        _, index = _decode(text, index, decoder)
        return index

    index = _array(text, index, element)
    if _skip(text, index) != len(text):
        raise ValueError("inventory is not a JSON list")
    return found


def discovery_document(entries: Sequence[InventoryEntry]) -> str:
    """The inventory ``GET /discovery`` serves: one entry per orchestrator, runners in the text we stored.
    json.dumps would quote the runner objects, so we build the document ourselves.
    """
    return _json_array(_orchestrator(entry) for entry in entries)


def _orchestrator(entry: InventoryEntry) -> str:
    address = json.dumps(entry.address, ensure_ascii=False)
    return f'{{"address":{address},"runners":{_json_array(entry.runners)}}}'


def _json_array(items: Iterable[str]) -> str:
    return "[" + ",".join(items) + "]"


def _skip(text: str, index: int) -> int:
    length = len(text)
    while index < length and text[index] in _WS:
        index += 1
    return index


def _array(text: str, index: int, element: Callable[[str, int], int]) -> int:
    """One JSON array, ``index`` just after ``[``. ``element`` reads one value and returns the next index."""
    index = _skip(text, index)
    if index < len(text) and text[index] == "]":
        return index + 1
    while index < len(text):
        index = element(text, index)
        index = _skip(text, index)
        if index < len(text) and text[index] == "]":
            return index + 1
        if index >= len(text) or text[index] != ",":
            raise ValueError("inventory is not valid JSON")
        index = _skip(text, index + 1)
    raise ValueError("inventory is not valid JSON")


def _entry(text: str, index: int, decoder: json.JSONDecoder) -> tuple[list[RawJson], int]:
    """The runner objects in this entry's last ``runners`` array, and the index after the object.

    A missing ``runners``, or null, contributes nothing. The last ``runners`` field wins. One that is not
    a list refuses the inventory, so a bad entry does not replace the last good one with an empty success.
    """
    index = _skip(text, index + 1)  # the caller is on '{'
    runners: list[RawJson] = []
    refused = False
    if index < len(text) and text[index] == "}":
        return runners, index + 1
    while index < len(text):
        key, index = _decode(text, index, decoder)
        if not isinstance(key, str):
            raise ValueError("inventory is not valid JSON")
        index = _skip(text, index)
        if index >= len(text) or text[index] != ":":
            raise ValueError("inventory is not valid JSON")
        index = _skip(text, index + 1)
        if key == "runners" and index < len(text) and text[index] == "[":
            taken, index = _runner_objects(text, index + 1, decoder)
            runners = taken
            refused = False
        elif key == "runners":
            value, index = _decode(text, index, decoder)
            runners = []
            refused = value is not None  # null is an empty list; anything else is not a list
        else:
            _, index = _decode(text, index, decoder)
        index = _skip(text, index)
        if index < len(text) and text[index] == "}":
            if refused:
                raise ValueError("runners is not a JSON list")
            return runners, index + 1
        if index >= len(text) or text[index] != ",":
            raise ValueError("inventory is not valid JSON")
        index = _skip(text, index + 1)
    raise ValueError("inventory is not valid JSON")


def _runner_objects(text: str, index: int, decoder: json.JSONDecoder) -> tuple[list[RawJson], int]:
    """The object elements of a ``runners`` array, as received. ``index`` is just after ``[``."""
    found: list[RawJson] = []

    def element(text: str, index: int) -> int:
        start = index
        value, index = _decode(text, index, decoder)
        if isinstance(value, dict):
            found.append(RawJson(text[start:index]))
        return index

    return found, _array(text, index, element)


def _decode(text: str, index: int, decoder: json.JSONDecoder) -> tuple[Any, int]:
    try:
        return decoder.raw_decode(text, index)
    except json.JSONDecodeError as exc:
        raise ValueError("inventory is not valid JSON") from exc
