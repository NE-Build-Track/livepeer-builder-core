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
        "https://orch.example/app?",
    ],
)
def test_a_runner_url_the_engine_cannot_send_is_skipped(url: str) -> None:
    assert read_inventory(_envelope(_RUNNER.replace("https://orch.example/app", json.dumps(url)[1:-1]))) == ()


def test_one_bad_runner_does_not_drop_the_rest() -> None:
    body = _envelope(_RUNNER + "," + _RUNNER.replace('"app":"example"', '"app":""'))
    (runner,) = read_inventory(body)
    assert runner.app == "example"
