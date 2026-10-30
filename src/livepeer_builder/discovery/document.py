"""The discovery document: each runner object kept as the JSON text that was received.

The envelope is checked here. A runner's numbers stay as the orchestrator spelled them, so ``1e-999999``
is not parsed into a float and written back.
"""

import json
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from livepeer_builder.utils import decimal_json_number

_WS = " \t\r\n"


def runner_records(body: str | bytes | bytearray) -> list[str]:
    """The runner objects in an orchestrator's ``GET /discovery`` body, in order, as received.

    A metadata entry, or an entry whose ``runners`` is null, contributes nothing. A ``runners`` value that
    is not a list is refused. A runner that is not an object is dropped. ``NaN`` and ``Infinity`` are not
    JSON and raise ``ValueError``. A number ``Decimal`` cannot construct does too, so it is never re-served
    for every engine's refresh to trip on.
    """
    text = body.decode() if isinstance(body, bytes | bytearray) else body
    # Same parser as the engine. A float parser turns 1e1000000000000000000 into inf and would keep the
    # token; the served document would then fail every consumer's refresh.
    decoder = json.JSONDecoder(parse_float=decimal_json_number, parse_constant=_not_json)
    index = _skip(text, 0)
    if index >= len(text) or text[index] != "[":
        raise ValueError("inventory is not a JSON list")
    entries, index = _array(text, index, decoder)
    if _skip(text, index) != len(text):
        raise ValueError("inventory is not a JSON list")
    runners: list[str] = []
    for value, raw in entries:
        if not isinstance(value, dict):
            continue
        found = _field(raw, "runners", decoder)
        if found is None or found[0] is None:  # missing, or null: a Go nil slice
            continue
        runners_value, runners_raw = found
        if not isinstance(runners_value, list):
            raise ValueError("runners is not a JSON list")
        items, _ = _array(runners_raw, 0, decoder)
        runners += [raw_item for item, raw_item in items if isinstance(item, dict)]
    return runners


def discovery_document(entries: Sequence[Mapping[str, Any]]) -> str:
    """The inventory ``GET /discovery`` serves: one entry per orchestrator, runners in the text we stored.
    json.dumps would quote the runner objects, so we build the document ourselves.
    """
    return _json_array(_orchestrator(entry) for entry in entries)


def _orchestrator(entry: Mapping[str, Any]) -> str:
    address = json.dumps(entry["address"], ensure_ascii=False)
    return f'{{"address":{address},"runners":{_json_array(entry["runners"])}}}'


def _json_array(items: Iterable[str]) -> str:
    return "[" + ",".join(items) + "]"


def _not_json(token: str) -> Any:
    raise ValueError(f"{token} is not a JSON number")


def _skip(text: str, index: int) -> int:
    length = len(text)
    while index < length and text[index] in _WS:
        index += 1
    return index


def _array(text: str, index: int, decoder: json.JSONDecoder) -> tuple[list[tuple[Any, str]], int]:
    index = _skip(text, index)
    if index >= len(text) or text[index] != "[":
        raise ValueError("runners is not a JSON list")
    index += 1
    items: list[tuple[Any, str]] = []
    index = _skip(text, index)
    if index < len(text) and text[index] == "]":
        return items, index + 1
    while index < len(text):
        start = index
        value, index = _decode(text, index, decoder)
        items.append((value, text[start:index]))
        index = _skip(text, index)
        if index < len(text) and text[index] == "]":
            return items, index + 1
        if index >= len(text) or text[index] != ",":
            raise ValueError("inventory is not valid JSON")
        index = _skip(text, index + 1)
    raise ValueError("inventory is not valid JSON")


def _field(text: str, name: str, decoder: json.JSONDecoder) -> tuple[Any, str] | None:
    """The last value of ``name`` in a JSON object, with the text it occupies. Absent is ``None``."""
    index = _skip(text, 0)
    if index >= len(text) or text[index] != "{":
        raise ValueError("inventory is not valid JSON")
    index = _skip(text, index + 1)
    found: tuple[Any, str] | None = None
    if index < len(text) and text[index] == "}":
        return None
    while index < len(text):
        key, index = _decode(text, index, decoder)
        index = _skip(text, index)
        if index >= len(text) or text[index] != ":":
            raise ValueError("inventory is not valid JSON")
        index = _skip(text, index + 1)
        start = index
        value, index = _decode(text, index, decoder)
        if key == name:
            found = (value, text[start:index])
        index = _skip(text, index)
        if index < len(text) and text[index] == "}":
            return found
        if index >= len(text) or text[index] != ",":
            raise ValueError("inventory is not valid JSON")
        index = _skip(text, index + 1)
    raise ValueError("inventory is not valid JSON")


def _decode(text: str, index: int, decoder: json.JSONDecoder) -> tuple[Any, int]:
    try:
        return decoder.raw_decode(text, index)
    except json.JSONDecodeError as exc:
        raise ValueError("inventory is not valid JSON") from exc
