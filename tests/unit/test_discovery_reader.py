"""The discovery reader against livepeer/node's own output and its own test cases at the pin (D63).

The reader takes the fields the engine stores; it does not validate node's schema. These tests anchor it
to the node commit ``docs/references/upstream-baseline.md`` records: ``node_discovery_f45d83f.json`` is
what that commit's ``livepeer-discovery --dump`` served for ai1 and xodeapp. When the pin moves, capture
that file again and keep these tests green.
"""

import json
from decimal import Decimal
from pathlib import Path

import pytest

from livepeer_builder.adapters.discovery_inventory import read_inventory

_NODE_DOCUMENT = Path(__file__).with_name("node_discovery_f45d83f.json").read_text()
_RUNNER = '{"url":"https://orch.example/app","app":"example","observed_at":"2026-10-22T18:05:00.123+01:00"}'
_METADATA = '[{"nodes":[{"url":"https://orch.example","queried_at":"2026-10-22T18:05:00Z"}]}]'


def _envelope(record: str, address: str = "https://orch.example/deployment") -> str:
    return '[{"address":"' + address + '","runners":[' + record + "]}]"


def test_every_runner_node_serves_is_read_with_its_exact_price() -> None:
    document = json.loads(_NODE_DOCUMENT, parse_float=Decimal, parse_int=Decimal)
    served = [
        (entry["address"], runner) for entry in document if "address" in entry for runner in entry["runners"]
    ]
    read = read_inventory(_NODE_DOCUMENT)
    assert len(read) == len(served) == 26
    for runner, (address, raw) in zip(read, served, strict=True):
        assert (runner.url, runner.app, runner.orchestrator_url) == (raw["url"], raw["app"], address)
        advertised = raw.get("price_info") or {}
        assert runner.price_info is not None
        assert runner.price_info.price == advertised.get("price")
        assert runner.price_info.price_usd == advertised.get("price_usd")
        assert (runner.price_info.currency, runner.price_info.unit) == (
            advertised["currency"],
            advertised["unit"],
        )


# Cases from livepeer/node's discovery/schema_test.go at f45d83f, by its own names. The reader keeps a
# runner exactly when node's schema accepts the document, with one deliberate difference noted below.
_KEPT = {
    "minimal runner": _envelope(_RUNNER),
    "IPv6 with port": _envelope(_RUNNER).replace("orch.example", "[::1]:8935"),
    "mixed-case schemes": _envelope(_RUNNER).replace("https://", "hTtPs://"),
    "unknown properties": _envelope(_RUNNER.removesuffix("}") + ',"extension":{"count":9007199254740993}}'),
    "extensible price": _envelope(
        _RUNNER.removesuffix("}")
        + ',"mode":"future","price_info":{"price":123456789012345678901234567890,'
        + '"price_usd":0.123456789012345678901234567890123456,"currency":"credits","unit":"tokens","extension":true}}'
    ),
    "USD price omitted": _envelope(
        _RUNNER.removesuffix("}") + ',"price_info":{"price":1,"currency":"wei","unit":"fixed"}}'
    ),
}
_SKIPPED = {
    "missing URL": _envelope(_RUNNER.replace('"url":"https://orch.example/app",', "")),
    "missing app": _envelope(_RUNNER.replace('"app":"example",', "")),
    "missing observation": _envelope('{"url":"https://orch.example/app","app":"example"}'),
    "empty app": _envelope(_RUNNER.replace('"app":"example"', '"app":""')),
    "invalid observation": _envelope(_RUNNER.replace("2026-10-22T18:05:00.123+01:00", "yesterday")),
    "missing runners": '[{"address":"https://orch.example"}]',
    "non-HTTP address": _envelope(_RUNNER, "ftp://orch.example"),
    "non-HTTP runner URL": _envelope(_RUNNER.replace("https://orch.example/app", "ftp://orch.example/app")),
    "address credentials": _envelope(_RUNNER, "https://user:pass@orch.example/deployment"),
    "address query": _envelope(_RUNNER, "https://orch.example/deployment?x=1"),
    "address empty query": _envelope(_RUNNER, "https://orch.example/deployment?"),
    "address fragment": _envelope(_RUNNER, "https://orch.example/deployment#part"),
    "hostless address": _envelope(_RUNNER, "https://:8935/deployment"),
    # Node's schema accepts this one. The engine appends each request's path to a runner URL and quotes
    # it in error text, so a URL with userinfo, a query or a fragment is not one it can call.
    "runner URL components": _envelope(
        _RUNNER.replace("https://orch.example/app", "https://user:pass@orch.example/app?x=1#part")
    ),
}


@pytest.mark.parametrize("body", _KEPT.values(), ids=_KEPT.keys())
def test_a_document_node_accepts_is_read(body: str) -> None:
    assert len(read_inventory(body)) == 1


@pytest.mark.parametrize("body", _SKIPPED.values(), ids=_SKIPPED.keys())
def test_a_runner_the_engine_cannot_use_is_skipped(body: str) -> None:
    assert read_inventory(body) == ()


@pytest.mark.parametrize("body", ["[]", _METADATA, '[{"nodes":[],"runners":[]}]'])
def test_an_empty_or_metadata_only_document_has_no_runners(body: str) -> None:
    assert read_inventory(body) == ()


@pytest.mark.parametrize(
    "url",
    [
        "https://orch.example/app\n",
        "https://orch.example/app\t",
        "https://orch.example/app\x7f",
        "https://orch example/app",
        "https://orch.example:abc/app",
        "https://orch.example:1:2/app",
        "https://orch.example:99999/app",
        "https://orch.example:0/app",
        "https://orch.example:00/app",
        "https://orch.example/app?",
        # aiohttp refuses these, or connects to a host other than the one the payment-origin check names.
        "https://orch.example\\evil/app",
        "https://[::1]]/app",
        "https://\u246027.0.0.1/app",
        "https://\uff11\uff12\uff17\uff0e\uff10\uff0e\uff10\uff0e\uff11/app",
        "https://127\u30020\u30020\u30021/app",
        "https://ex\xe4mple.com/app",
    ],
)
def test_a_runner_url_the_engine_cannot_send_is_skipped(url: str) -> None:
    assert read_inventory(_envelope(_RUNNER.replace("https://orch.example/app", json.dumps(url)[1:-1]))) == ()


def test_an_address_aiohttp_would_connect_to_under_another_name_skips_its_entry() -> None:
    assert read_inventory(_envelope(_RUNNER, "https://\u246027.0.0.1")) == ()


@pytest.mark.parametrize(
    "url",
    [
        "https://ORCH.example/app",
        "https://xn--exmple-cua.com/app",
        "http://[::1]:8935/app",
        "https://orch.example:65535/app",
        "http://orch.example:080/app",
    ],
)
def test_a_runner_url_aiohttp_reads_as_written_is_kept(url: str) -> None:
    (runner,) = read_inventory(_envelope(_RUNNER.replace("https://orch.example/app", url)))
    assert runner.url == url


def test_one_bad_runner_does_not_drop_the_rest() -> None:
    body = _envelope(_RUNNER + "," + _RUNNER.replace('"app":"example"', '"app":""'))
    (runner,) = read_inventory(body)
    assert runner.app == "example"


def _with(field: str, token: str) -> str:
    return _envelope(_RUNNER.removesuffix("}") + f',"{field}":{token}}}')


@pytest.mark.parametrize(
    ("token", "value"),
    [
        ("3", 3),
        ("3.0", 3),
        ("3e0", 3),
        ("1e2", 100),
        ("-0.0", 0),
        ("1.5", None),
        ("1e400", None),
        ('"3"', None),
    ],
)
def test_a_capacity_is_read_as_the_integer_node_accepts(token: str, value: int | None) -> None:
    (runner,) = read_inventory(_with("capacity", token))
    assert runner.capacity == value


@pytest.mark.parametrize("token", ["0", "0.0", "0e0", "-0.0", "false", '"2"', "1.5", "null"])
def test_a_runner_reporting_no_or_a_bad_free_capacity_is_busy(token: str) -> None:
    """Missing means unknown, which selection treats as one free slot. A present value that is not an
    integer must not fall back to that default, or a full runner writing ``0.0`` would be chosen."""
    (runner,) = read_inventory(_with("capacity_available", token))
    assert runner.capacity_available == 0


def test_a_missing_free_capacity_stays_unknown() -> None:
    (runner,) = read_inventory(_envelope(_RUNNER))
    assert runner.capacity_available is None


def test_a_lowercase_rfc_3339_stamp_is_read() -> None:
    (runner,) = read_inventory(
        _envelope(_RUNNER.replace("2026-10-22T18:05:00.123+01:00", "2026-10-22t18:05:00.5z"))
    )
    assert (
        runner.observed_at is not None
        and runner.observed_at.isoformat() == "2026-10-22T18:05:00.500000+00:00"
    )


@pytest.mark.parametrize(
    "stamp",
    [
        "2026-10-22 18:05:00Z",
        "2026-10-22T18:05:00+00",
        "2026-10-22T18:05:00+0000",
        "2026-10-22T18:05Z",
        "2026-W43-3T18:05:00Z",
        "20261022T180500Z",
        "2026-10-22T18:05:00",
        "2026-10-22T23:59:60Z",
        "2026-10-22T18:05:00+00:60",
        "2026-10-22T18:05:00+00:99",
        "2026-10-22T18:05:00+24:00",
        "2026-10-22T24:00:00Z",
        "2026-10-22T18:60:00Z",
    ],
)
def test_a_stamp_that_is_not_rfc_3339_or_does_not_convert_skips_the_runner(stamp: str) -> None:
    assert read_inventory(_envelope(_RUNNER.replace("2026-10-22T18:05:00.123+01:00", stamp))) == ()
