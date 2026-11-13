"""Read a livepeer/node ``GET /discovery`` document into the runners the engine stores (D63).

A body that is not a JSON array fails the refresh. Each other failure skips that entry or that runner.
There is no size cap, no stamp-age filter and no de-duplication. Numbers stay their JSON tokens, never
binary floats.
"""

import json
import logging
import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlsplit

import yarl

from livepeer_builder.contracts.discovery import PriceInfo, Runner, RunnerGPU

_LOG = logging.getLogger(__name__)
_FRACTION = re.compile(r"(\.\d{6})\d+")
# RFC 3339 date-time: ``T`` and ``Z`` in either case, seconds required, ``Z`` or an offset of 00-23 hours
# and 00-59 minutes. fromisoformat rejects bad clock fields itself, but carries offset minutes past 59.
_RFC3339 = re.compile(
    r"\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-](?:[01]\d|2[0-3]):[0-5]\d)", re.ASCII
)
_INT64 = 1 << 63
_HTTP = frozenset({"http", "https"})
# Text the engine cannot keep or serve (#114, C12): a control character (NUL among them, which PostgreSQL
# refuses), or a lone surrogate, which JSON can carry as ``\udXXX`` and UTF-8 cannot, so the catalog could
# not be written.
_NOT_TEXT = r"\x00-\x1f\x7f-\x9f\ud800-\udfff"
_INVALID_TEXT = re.compile(f"[{_NOT_TEXT}]")
_UNSENDABLE = re.compile(f"[ {_NOT_TEXT}]")  # a URL: a space too, which the HTTP client cannot send
_MAX_APP = 200  # a job stores its app as ``store.schema.ID``, and ``JobRequest.app`` takes no longer one


class JsonNumber:
    """One JSON number token, never a binary float. ``Decimal`` may still be unable to hold it."""

    def __init__(self, token: str) -> None:
        self.token = token


def read_inventory(raw: str | bytes | bytearray) -> tuple[Runner, ...]:
    """Runners the document can be read as. A body that is not a JSON array raises ``ValueError``.

    An entry needs an ``address`` the engine can call (``_http_url``), and a ``runners`` list. A metadata
    entry (``nodes``, and no ``address``) yields nothing. A runner needs an ``app`` that is valid text
    (``_text``) of 1 to 200 characters, a ``url`` the engine can call, and an ``observed_at`` the clock can
    convert to UTC. Any other text field that is not valid text is absent. One bad record does not drop
    the rest. ``[]`` is an empty inventory. A number ``Decimal`` cannot hold is still a number: the
    runner is kept, and ``payable_rate`` reports that its price cannot be paid.
    """
    entries = _loads(raw)
    if not isinstance(entries, list):
        raise ValueError("discovery response is not a JSON list")
    runners: list[Runner] = []
    skipped_entries = 0
    skipped_runners = 0
    for entry in entries:
        if _metadata(entry):
            continue
        parsed = _entry(entry)
        if parsed is None:
            skipped_entries += 1
            continue
        address, records = parsed
        for record in records:
            runner = _runner(record, address)
            if runner is None:
                skipped_runners += 1
            else:
                runners.append(runner)
    if skipped_entries or skipped_runners:
        _LOG.warning(
            "discovery inventory had malformed records",
            extra={
                "event": "discovery.inventory_skipped",
                "entries": skipped_entries,
                "runners": skipped_runners,
            },
        )
    return tuple(runners)


def _loads(raw: str | bytes | bytearray) -> Any:
    """``json.loads`` with every number kept as its token. ``NaN`` and ``Infinity`` are not JSON."""
    return json.loads(raw, parse_int=JsonNumber, parse_float=JsonNumber, parse_constant=_not_json)


def _not_json(token: str) -> Any:
    raise ValueError(f"{token} is not a JSON number")


def _metadata(entry: object) -> bool:
    return isinstance(entry, dict) and "nodes" in entry and "address" not in entry


def _entry(entry: object) -> tuple[str, list[Any]] | None:
    if not isinstance(entry, dict):
        return None
    address = _http_url(entry.get("address"))
    records = entry.get("runners")
    if address is None or not isinstance(records, list):
        return None
    return address, records


def _runner(raw: object, address: str) -> Runner | None:
    if not isinstance(raw, dict):
        return None
    url = _http_url(raw.get("url"))
    app = _text(raw.get("app"))
    if url is None or not app or len(app) > _MAX_APP:
        return None
    observed_at = _stamp(raw.get("observed_at"))
    if observed_at is None:
        return None
    gpu = raw.get("gpu")
    # livepeer-discovery at the pin republishes the claimed address. The removed prober stored the URL
    # it had fetched, so one orchestrator could not name another's payment host. Keep the address only
    # when its origin is the runner URL's: PaidRequest will otherwise sign a 402 whose payment_url is
    # that host, and orchestrator_of will use it as the failover group.
    return Runner(
        url=url,
        app=app,
        mode=_text(raw.get("mode")),
        orchestrator_url=address if _origin(address) == _origin(url) else None,
        capacity=_integer(raw.get("capacity")),
        capacity_used=_integer(raw.get("capacity_used")),
        capacity_available=_available(raw),
        price_info=_price_info(raw.get("price_info")),
        observed_at=observed_at,
        version=_text(raw.get("version")),
        metadata=_text(raw.get("metadata")),
        gpu=_gpu(gpu) if isinstance(gpu, dict) else None,
    )


def _http_url(value: object) -> str | None:
    """An ``http`` or ``https`` URL the engine can call, kept as written; otherwise None.

    aiohttp, which makes every runner call, reads the URL with yarl. yarl must connect to the host
    ``urlsplit`` names, the one the payment-origin check compares, on a port from 1 to 65535. yarl folds
    a non-ASCII host, so ``①27.0.0.1`` would reach ``127.0.0.1``, and it refuses a backslash in the host.
    The URL holds no whitespace, control character or lone surrogate, which aiohttp cannot send. It has no
    userinfo, query or fragment: the engine appends each request's path to a runner URL, an ``address``
    whose origin matches that URL becomes ``orchestrator_url`` for the payment-origin check, and error
    text quotes both.
    """
    if not isinstance(value, str) or not value or _UNSENDABLE.search(value):
        return None
    try:
        parts = urlsplit(value)
        host, _ = parts.hostname, parts.port  # .port raises for a port that is not a number
        dialed = yarl.URL(value)
    except ValueError:
        return None
    if parts.scheme not in _HTTP or not host or "@" in parts.netloc or "?" in value or "#" in value:
        return None
    if dialed.raw_host != host or not 1 <= (dialed.port or 0) <= 65535:
        return None
    return value


def _origin(url: str) -> str | None:
    """``scheme://host:port`` as ``PaidRequest`` compares payment origins, or ``None`` when unusable."""
    try:
        parts = urlsplit(url)
        scheme, host, port = parts.scheme.lower(), parts.hostname, parts.port
    except ValueError:
        return None
    if port is None:
        port = 443 if scheme == "https" else 80
    if scheme not in _HTTP or not host or not 1 <= port <= 65535:
        return None
    return f"{scheme}://{host.lower()}:{port}"


def _price_info(value: object) -> PriceInfo | None:
    if not isinstance(value, dict):
        return None
    return PriceInfo(
        price=_decimal(value.get("price")),
        price_usd=_decimal(value.get("price_usd")),
        currency=_text(value.get("currency")),
        unit=_text(value.get("unit")),
    )


def _decimal(value: object) -> Decimal | None:
    """The exact token, or None when ``Decimal`` cannot hold it. Never a binary float."""
    if not isinstance(value, JsonNumber):
        return None
    try:
        return Decimal(value.token)
    except InvalidOperation:
        return None


def _integer(value: object) -> int | None:
    """A JSON number with a whole value that fits 64 bits, as node accepts it (``3``, ``3.0``, ``3e0``)."""
    if not isinstance(value, JsonNumber):
        return None
    try:
        number = Decimal(value.token)
    except InvalidOperation:
        return None
    if not number.is_finite() or number != number.to_integral_value() or not -_INT64 <= number < _INT64:
        return None
    return int(number)


def _available(raw: dict[str, Any]) -> int | None:
    """Free slots. Missing means unknown, which selection treats as one free slot. A value that is present
    and not an integer is 0, not unknown: a runner that reports its capacity badly is not chosen."""
    if "capacity_available" not in raw:
        return None
    available = _integer(raw["capacity_available"])
    return 0 if available is None else available


def _text(value: object) -> str | None:
    """A string of valid Unicode text with no control character, exactly as written; otherwise None.

    Nothing is repaired: text with a NUL, another control character or a lone surrogate is not read."""
    if not isinstance(value, str) or _INVALID_TEXT.search(value):
        return None
    return value


def _stamp(value: object) -> datetime | None:
    """The RFC 3339 stamp in UTC, or None. Nanoseconds truncate to microseconds. A stamp RFC 3339 allows
    and the clock cannot hold, such as a leap second, is None too."""
    if not isinstance(value, str) or _RFC3339.fullmatch(value) is None:
        return None
    text = value[:10] + "T" + value[11:]
    text = _FRACTION.sub(r"\1", text[:-1] + "+00:00" if text[-1] in "zZ" else text)
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


def _gpu(value: dict[str, Any]) -> RunnerGPU:
    return RunnerGPU(
        id=_text(value.get("id")),
        name=_text(value.get("name")),
        vram_mb=_integer(value.get("vram_mb")),
    )
