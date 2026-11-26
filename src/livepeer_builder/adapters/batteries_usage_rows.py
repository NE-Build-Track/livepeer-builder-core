"""A Batteries usage item, read as a usage row or set aside (#114, C13).

Split from ``batteries.py``, which holds the HTTP calls: this module holds no client, only what one item
of ``GET /v1/usage`` must look like to be a ``UsageRow``, the record that sets it aside when it does not,
and the reading of a page whose item the JSON decoder refuses. Each value is judged as it was received.
Every reader of Batteries usage goes through it: the cost-sync page and a tenant's page alike. Baseline:
livepeer/clearinghouse-batteries v0.0.3 (docs/references/batteries-api.md).
"""

import json
import re
import secrets
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from livepeer_builder.contracts import SetAsideReason, SetAsideUsage, UsageRow
from livepeer_builder.contracts.costs import set_aside_record, unstorable_field, unstorable_reason
from livepeer_builder.errors import ProviderError

_USAGE_STATUSES = frozenset({"applied", "quarantined", "ignored", "duplicate"})
_NO_FEE = ("", "[error]")  # what Batteries serves, with null, for a fee it has not or could not convert
# A fee as Batteries writes one (v0.0.3, ``internal/units/eth.go``, ``UnitsToDecimal``): digits, then a
# point and digits when it has a fraction, after a minus sign when it is negative. No exponent, whatever
# the size, and ASCII digits only: without ``re.ASCII``, ``\d`` takes every script's digits.
_FEE = re.compile(r"-?\d+(?:\.\d+)?", re.ASCII)
# A usage item's text fields: Batteries serves each as a string or null
_USAGE_TEXT = (
    "event_id", "manifest_id", "allocation_id", "payment_session_id", "request_id", "pipeline", "currency"
)  # fmt: skip
# A JSON string, or one bracket: enough to find where each item of a page starts and ends without decoding it
_TOKEN = re.compile(r'"[^"\\]*(?:\\.[^"\\]*)*"|[\[\]{}]', re.DOTALL)


def page_past_the_decoder(content: bytes) -> tuple[dict[str, Any], list[SetAsideUsage]] | None:
    """A usage page the JSON decoder refused, read without the items its own limits refuse: an integer
    past its digit limit, or nesting past its depth (#158). One such item failed its page on every pass,
    before any row was read. The answer is the page's object with those items left out, and the record
    that sets each aside: ``unreadable_row``, under a digest of the item's text, which is its content.
    Nothing of such an item is read, its id and its fees included.

    None when that is not why the page was refused: it is not UTF-8 or not JSON, or what the decoder
    cannot read is no object in the page's ``items``. Such a page is still no page. So is one whose own
    object names a key twice: the decoder keeps the last, so a first ``items`` list was dropped with
    its rows while the item cut from the second accounted for every marker (#189 review)."""
    try:  # as the decoder reads bytes: UTF-8, 16 or 32, without the byte order mark a page may begin with
        text = content.decode(json.detect_encoding(content))
    except UnicodeDecodeError:
        return None
    marker = secrets.token_hex(16)  # a key no item has: it marks where an item was cut out
    kept, cut = _without_items_past_the_decoder(text, marker)
    try:
        body = loads_naming_each_key_once(kept) if cut else None
    except (ValueError, RecursionError):
        return None
    items = body.get("items") if isinstance(body, dict) else None
    if not isinstance(body, dict) or not isinstance(items, list):
        return None
    marked = [isinstance(item, dict) and marker in item for item in items]
    if sum(marked) != len(cut):  # something cut was no item of this page
        return None
    body["items"] = [item for item, cut_here in zip(items, marked, strict=True) if not cut_here]
    return body, [
        set_aside_record("unreadable_row", item, row_id=None, fee_eth=None, fee_usd=None) for item in cut
    ]


class KeyNamedTwice(ValueError):
    """An answer whose own object names a key twice."""


def loads_naming_each_key_once(text: str | bytes) -> Any:
    """``json.loads``, refusing an answer whose own object names a key twice (``KeyNamedTwice``). The
    decoder keeps the last of the two and says nothing: of a page that names ``items`` twice, the rows
    of the first list are gone and its cursor is followed past them (#189 review). Only the answer's own
    object is judged. An item that repeats a key is read as the decoder reads it, and never fails its page."""
    last: list[list[tuple[str, Any]]] = [[]]  # the keys of the object decoded last, which is the outermost

    def keep(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        last[0] = pairs
        return dict(pairs)

    value = json.loads(text, object_pairs_hook=keep)
    if isinstance(value, dict) and len(value) != len(last[0]):
        raise KeyNamedTwice("an object that names a key twice")
    return value


def _without_items_past_the_decoder(text: str, marker: str) -> tuple[str, list[str]]:
    """``text`` with each such item replaced by ``{marker: n}``, and the items' texts."""
    parts: list[str] = []
    cut: list[str] = []
    copied = 0
    for start, end in _item_spans(text):
        if _past_the_decoder(text[start:end]):
            parts += [text[copied:start], json.dumps({marker: len(cut)})]
            cut.append(text[start:end])
            copied = end
    return "".join([*parts, text[copied:]]), cut


def _item_spans(text: str) -> Iterator[tuple[int, int]]:
    """Where each item of a page starts and ends. An item is an object directly in a list that is
    directly in the page's object, found by counting brackets outside strings: no recursion, whatever
    the nesting."""
    open_brackets: list[str] = []
    start = -1
    for token in _TOKEN.finditer(text):
        bracket = token.group()
        if bracket in ("[", "{"):
            if bracket == "{" and open_brackets == ["{", "["]:
                start = token.start()
            open_brackets.append(bracket)
        elif bracket in ("]", "}") and open_brackets:
            open_brackets.pop()
            if start >= 0 and len(open_brackets) == 2:
                yield start, token.end()
                start = -1


def _past_the_decoder(item: str) -> bool:
    """Whether ``item`` is refused by the decoder's limits and not for being no JSON. The decoder stops
    at the first limit it meets and reads no further, so what it stopped in is read here to its end: an
    item that is no JSON after its long integer, or inside its deep nesting, is no item to leave out, and
    its page is still no page (#189 review)."""
    try:
        json.loads(item)
    except json.JSONDecodeError:
        return False
    except (ValueError, RecursionError):  # the digit limit of an integer, the depth of the nesting
        return is_json(item)
    return False


# The decoder's grammar, as it reads by default: no control character in a string, no leading zero, ASCII
# digits, and the names it takes for numbers that are none. Each pattern is matched where a token starts.
_SPACE = re.compile(r"[ \t\n\r]*")
_PLAIN = re.compile(r'[^"\\\x00-\x1f]*')  # of a string: up to its end, an escape or a control character
_ESCAPE = re.compile(r'\\(?:["\\/bfnrt]|u[0-9a-fA-F]{4})')
_NUMBER = re.compile(r"-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][-+]?\d+)?", re.ASCII)
_NAMES = ("true", "false", "null", "NaN", "Infinity", "-Infinity")
# the kinds of token
_SCALAR, _TEXT, _OPEN, _CLOSE, _COMMA, _COLON, _END = (
    "scalar",
    "string",
    "open",
    "close",
    "comma",
    "colon",
    "end",
)
_PUNCTUATION = {"[": _OPEN, "{": _OPEN, "]": _CLOSE, "}": _CLOSE, ",": _COMMA, ":": _COLON}
_CLOSES = {"]": "[", "}": "{"}
# what may come next
_A_VALUE, _A_FIRST_VALUE, _A_KEY, _A_FIRST_KEY = "a value", "a first value", "a key", "a first key"
_A_COLON, _A_COMMA_OR_CLOSE, _THE_END = "a colon", "a comma or a close", "the end"
_NOTHING_MORE, _NO_JSON = "nothing more", "no JSON"


def is_json(text: str) -> bool:
    """Whether ``text`` is one JSON value by the decoder's grammar, read token by token with a list of
    the brackets that are open: no recursion and no number is built, so neither of the decoder's limits
    is met, whatever the nesting and however long an integer."""
    opened: list[str] = []
    expects, position = _A_VALUE, 0
    while expects not in (_NOTHING_MORE, _NO_JSON):
        token = _token(text, position)
        if token is None:
            return False
        kind, value, position = token
        expects = _NEXT[expects](kind, value, opened)
    return expects == _NOTHING_MORE


def _token(text: str, start: int) -> tuple[str, str, int] | None:
    """The token at ``start``, after any space: its kind, its text and where it ends. None for none."""
    at = _SPACE.match(text, start).end()  # type: ignore[union-attr]  # it matches no space too
    if at == len(text):
        return _END, "", at
    if text[at] in _PUNCTUATION:
        return _PUNCTUATION[text[at]], text[at], at + 1
    for name in _NAMES:
        if text.startswith(name, at):
            return _SCALAR, name, at + len(name)
    if text[at] == '"':
        end = _string_end(text, at)
        return None if end is None else (_TEXT, text[at:end], end)
    number = _NUMBER.match(text, at)
    return None if number is None else (_SCALAR, number.group(), number.end())


def _string_end(text: str, opening: int) -> int | None:
    """Where the string that opens at ``opening`` ends, after its closing quote. None when it does not
    close, holds a control character, or has an escape the decoder does not read. Read run by run, with
    no pattern that repeats a choice: a long string costs its length."""
    position = _PLAIN.match(text, opening + 1).end()  # type: ignore[union-attr]  # it matches no text too
    while position < len(text) and text[position] == "\\":
        escape = _ESCAPE.match(text, position)
        if escape is None:
            return None
        position = _PLAIN.match(text, escape.end()).end()  # type: ignore[union-attr]
    return position + 1 if position < len(text) and text[position] == '"' else None


def _closed(opened: list[str]) -> str:
    """What follows a value that has just ended."""
    return _A_COMMA_OR_CLOSE if opened else _THE_END


def _value(kind: str, text: str, opened: list[str], *, may_close: bool = False) -> str:
    if kind in (_TEXT, _SCALAR):
        return _closed(opened)
    if kind == _OPEN:
        opened.append(text)
        return _A_FIRST_VALUE if text == "[" else _A_FIRST_KEY
    if kind == _CLOSE and may_close and text == "]":  # an empty list
        opened.pop()
        return _closed(opened)
    return _NO_JSON


def _first_value(kind: str, text: str, opened: list[str]) -> str:
    return _value(kind, text, opened, may_close=True)


def _key(kind: str, text: str, opened: list[str], *, may_close: bool = False) -> str:
    if kind == _TEXT:
        return _A_COLON
    if kind == _CLOSE and may_close and text == "}":  # an empty object
        opened.pop()
        return _closed(opened)
    return _NO_JSON


def _first_key(kind: str, text: str, opened: list[str]) -> str:
    return _key(kind, text, opened, may_close=True)


def _colon(kind: str, text: str, opened: list[str]) -> str:
    return _A_VALUE if kind == _COLON else _NO_JSON


def _comma_or_close(kind: str, text: str, opened: list[str]) -> str:
    if kind == _COMMA:
        return _A_VALUE if opened[-1] == "[" else _A_KEY
    if kind == _CLOSE and _CLOSES[text] == opened[-1]:
        opened.pop()
        return _closed(opened)
    return _NO_JSON


def _end(kind: str, text: str, opened: list[str]) -> str:
    return _NOTHING_MORE if kind == _END else _NO_JSON


_NEXT = {
    _A_VALUE: _value,
    _A_FIRST_VALUE: _first_value,
    _A_KEY: _key,
    _A_FIRST_KEY: _first_key,
    _A_COLON: _colon,
    _A_COMMA_OR_CLOSE: _comma_or_close,
    _THE_END: _end,
}


def read_usage_item(item: dict[str, Any]) -> UsageRow | SetAsideUsage:
    """The item as a usage row, or the record that sets it aside when it cannot be read or stored (#114,
    C13): one such item no longer fails its page, which the sync would read again on every pass. The
    record's content is the item as received."""
    reason = _unreadable(item)
    if reason is None:
        try:
            row = usage_row_from_item(item)
        except ValueError:
            # The backstop: whatever the checks miss, the row never fails its page. The row type's
            # ``ValidationError`` is a ``ValueError``, and so is a fee ``_fee`` does not read.
            reason = "unreadable_row"
        else:
            field = unstorable_field(row)
            if field is None:
                return row
            reason = unstorable_reason(field)
    return set_aside_record(
        reason,
        item,
        row_id=_row_id(item),
        fee_eth=_kept_fee(item.get("computed_fee_eth")),
        fee_usd=_kept_fee(item.get("computed_fee_usd")),
    )


def _unreadable(item: dict[str, Any]) -> SetAsideReason | None:
    """Why the item is no usage row, as a short code; None when it is one. Each value is judged as it was
    received, so the value checked is the value stored: nothing is turned into text first."""
    status = item.get("status")
    if _row_id(item) is None:
        return "missing_id"
    if not isinstance(status, str) or status not in _USAGE_STATUSES:
        return "unknown_status"
    if _created_at(item) is None:
        return "unreadable_created_at"
    for name in _USAGE_TEXT:
        if not isinstance(item.get(name), str | None):
            return unstorable_reason(name)  # no text: the column would keep its Python form
    fees = (item.get("computed_fee_eth"), item.get("computed_fee_usd"))
    return "unreadable_fee" if any(_unreadable_fee(fee) for fee in fees) else None


def _unreadable_fee(value: Any) -> bool:
    """Whether a fee is stated and is not one the engine reads (``_fee``). Such a fee is never read as no
    fee, which an applied row would then cost."""
    try:
        _fee(value)
    except ValueError:
        return True
    return False


def _fee(value: Any) -> Decimal | None:
    """The fee an item states, exactly: None when it states none (``null``, ``""``, ``"[error]"``), and a
    ``ValueError`` for one that is not as Batteries writes a fee, which is never guessed at (#158).
    Python's ``Decimal`` reads far more than that: an exponent (``"1e999999"`` was stored, and two such
    fees could not be added), ``"1_000"``, digits of other scripts, padded text. And a JSON number is no
    text: it went through ``str()``, as a text field's once did (#114, C13)."""
    if value is None or value in _NO_FEE:
        return None
    if not isinstance(value, str) or not _FEE.fullmatch(value):
        raise ValueError("not a fee as Batteries writes one")
    return Decimal(value)


def _kept_fee(value: Any) -> Decimal | None:
    """The fee for a set-aside row's record: the one the engine reads, else none."""
    return None if _unreadable_fee(value) else _fee(value)


def _row_id(item: dict[str, Any]) -> str | None:
    """The row's id as text: a string that is not blank (empty, or white space alone), as received, or a
    whole number as its digits."""
    value = item.get("id")
    if isinstance(value, str):
        return value if value.strip() else None
    return str(value) if isinstance(value, int) and not isinstance(value, bool) else None


def _created_at(item: dict[str, Any]) -> datetime | None:
    """``created_at_ms`` as a time, or None when it is absent, not a JSON number (a string of digits and
    ``true`` are not), or past what a clock holds."""
    value = item.get("created_at_ms")
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=UTC)
    except (ValueError, OverflowError, OSError):
        return None


def usage_row_from_item(item: dict[str, Any]) -> UsageRow:
    status = item.get("status")
    if status not in _USAGE_STATUSES:
        raise ProviderError(200, f"unknown usage status {status!r}")
    return UsageRow(
        id=str(item["id"]),
        event_id=_text(item.get("event_id")),
        status=status,
        manifest_id=_text(item.get("manifest_id")),
        allocation_id=_text(item.get("allocation_id")),
        payment_session_id=_text(item.get("payment_session_id")),
        request_id=_text(item.get("request_id")),
        pipeline=_text(item.get("pipeline")),
        computed_fee_eth=_fee(item.get("computed_fee_eth")),
        computed_fee_usd=_fee(item.get("computed_fee_usd")),
        currency=_text(item.get("currency")),
        created_at=_created_at(item),
    )


def _text(value: str | None) -> str | None:
    """A usage item's text field: Batteries serves ``""`` where it has none."""
    return value or None
