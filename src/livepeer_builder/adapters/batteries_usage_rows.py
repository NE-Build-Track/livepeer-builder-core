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
    try:
        text = content.decode()
    except UnicodeDecodeError:
        return None
    marker = secrets.token_hex(16)  # a key no item has: it marks where an item was cut out
    kept, cut = _without_items_past_the_decoder(text, marker)
    objects: list[list[tuple[str, Any]]] = [[]]  # the keys of the object decoded last: the page's own

    def keep_last(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        objects[0] = pairs
        return dict(pairs)

    try:
        body = json.loads(kept, object_pairs_hook=keep_last) if cut else None
    except (ValueError, RecursionError):
        return None
    if isinstance(body, dict) and len(body) != len(objects[0]):  # a key named twice
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
    """Whether ``item`` is refused by the decoder's limits and not for being no JSON."""
    try:
        json.loads(item)
    except json.JSONDecodeError:
        return False
    except (ValueError, RecursionError):  # the digit limit of an integer, the depth of the nesting
        return True
    return False


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
