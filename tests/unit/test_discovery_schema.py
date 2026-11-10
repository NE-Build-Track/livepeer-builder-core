"""The vendored livepeer/node discovery schema: pin, fixtures, and a differential check (D63)."""

import hashlib
import json
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from livepeer_builder.adapters.discovery_inventory import (
    _RUNNER,
    document_is_valid,
    loads_inventory,
    read_inventory,
    schema_pin,
)

_RUNNER_JSON = (
    '{"url":"https://orch.example/app","app":"example","observed_at":"2026-10-22T18:05:00.123+01:00"}'
)
_METADATA = '[{"nodes":[{"url":"https://orch.example","queried_at":"2026-10-22T18:05:00Z"}]}]'


def _envelope(record: str) -> str:
    return '[{"address":"https://orch.example/deployment","runners":[' + record + "]}]"


def _cases() -> list[tuple[str, str, bool]]:
    """Port of discovery/schema_test.go discoverySchemaCases at f45d83f."""
    runner = _RUNNER_JSON
    return [
        ("empty", "[]", True),
        ("empty runners", _envelope(""), True),
        ("metadata", _METADATA, True),
        ("metadata with empty runners", '[{"nodes":[],"runners":[]}]', True),
        ("minimal runner", _envelope(runner), True),
        ("IPv6 with port", _envelope(runner).replace("orch.example", "[::1]:8935"), True),
        ("mixed-case schemes", _envelope(runner).replace("https://", "hTtPs://"), True),
        ("mixed-case node scheme", _METADATA.replace("https://", "HtTp://"), True),
        (
            "runner URL components",
            _envelope(
                runner.replace("https://orch.example/app", "https://user:pass@orch.example/app?x=1#part")
            ),
            True,
        ),
        (
            "unknown properties",
            _envelope(runner.removesuffix("}") + ',"extension":{"count":9007199254740993}}'),
            True,
        ),
        (
            "extensible price",
            _envelope(
                runner.removesuffix("}")
                + ',"mode":"future","price_info":{"price":123456789012345678901234567890,'
                + '"price_usd":0.123456789012345678901234567890123456,"currency":"credits",'
                + '"unit":"tokens","extension":true}}'
            ),
            True,
        ),
        (
            "USD price omitted",
            _envelope(
                runner.removesuffix("}") + ',"price_info":{"price":1,"currency":"wei","unit":"fixed"}}'
            ),
            True,
        ),
        ("price string", _envelope(runner.removesuffix("}") + ',"price_info":{"price":"1"}}'), False),
        (
            "USD price string",
            _envelope(runner.removesuffix("}") + ',"price_info":{"price":1,"price_usd":"0.1"}}'),
            False,
        ),
        ("missing URL", _envelope(runner.replace('"url":"https://orch.example/app",', "")), False),
        ("missing app", _envelope(runner.replace('"app":"example",', "")), False),
        ("missing observation", _envelope('{"url":"https://orch.example/app","app":"example"}'), False),
        ("empty app", _envelope(runner.replace('"app":"example"', '"app":""')), False),
        (
            "invalid observation",
            _envelope(runner.replace("2026-10-22T18:05:00.123+01:00", "yesterday")),
            False,
        ),
        (
            "invalid query time",
            '[{"nodes":[{"url":"https://orch.example","queried_at":"yesterday"}]}]',
            False,
        ),
        ("missing query time", '[{"nodes":[{"url":"https://orch.example"}]}]', False),
        ("missing node URL", '[{"nodes":[{"queried_at":"2026-10-22T18:05:00Z"}]}]', False),
        ("metadata with address", '[{"nodes":[],"address":"https://orch.example"}]', False),
        ("metadata with runners", '[{"nodes":[],"runners":[' + runner + "]}]", False),
        ("metadata with non-array runners", '[{"nodes":[],"runners":{}}]', False),
        ("missing runners", '[{"address":"https://orch.example"}]', False),
        (
            "non-HTTP address",
            _envelope(runner).replace("https://orch.example/deployment", "ftp://orch.example"),
            False,
        ),
        (
            "non-HTTP runner URL",
            _envelope(runner.replace("https://orch.example/app", "ftp://orch.example/app")),
            False,
        ),
        (
            "address credentials",
            _envelope(runner).replace(
                "https://orch.example/deployment", "https://user:pass@orch.example/deployment"
            ),
            False,
        ),
        (
            "address query",
            _envelope(runner).replace(
                "https://orch.example/deployment", "https://orch.example/deployment?x=1"
            ),
            False,
        ),
        (
            "address empty query",
            _envelope(runner).replace("https://orch.example/deployment", "https://orch.example/deployment?"),
            False,
        ),
        (
            "address fragment",
            _envelope(runner).replace(
                "https://orch.example/deployment", "https://orch.example/deployment#part"
            ),
            False,
        ),
        (
            "node credentials",
            _METADATA.replace("https://orch.example", "https://user:pass@orch.example"),
            False,
        ),
        ("node query", _METADATA.replace("https://orch.example", "https://orch.example?x=1"), False),
        ("node fragment", _METADATA.replace("https://orch.example", "https://orch.example#part"), False),
        (
            "hostless address",
            _envelope(runner).replace("https://orch.example/deployment", "https://:8935/deployment"),
            False,
        ),
        ("hostless node", _METADATA.replace("https://orch.example", "https://:8935"), False),
        (
            "userinfo-only runner",
            _envelope(runner.replace("https://orch.example/app", "https://user:pass@/app")),
            False,
        ),
    ]


@pytest.mark.parametrize(("name", "body", "valid"), _cases(), ids=[case[0] for case in _cases()])
def test_the_reader_keeps_or_skips_each_schema_case(name: str, body: str, valid: bool) -> None:
    del name
    assert document_is_valid(body) is valid
    kept = read_inventory(body)
    if valid:
        assert len(kept) == body.count('"app":"example"') + body.count('"app": "example"')
    else:
        assert kept == ()


def test_a_bad_entry_does_not_drop_a_good_one() -> None:
    good = json.loads(_envelope(_RUNNER_JSON))[0]
    bad = {"address": "https://orch.example", "runners": "nope"}
    (runner,) = read_inventory(json.dumps([bad, good]))
    assert runner.app == "example"


def test_the_vendored_schema_matches_its_pin_and_the_baseline() -> None:
    pin = schema_pin()
    schema = Path("src/livepeer_builder/contracts/discovery.schema.json").read_bytes()
    assert hashlib.sha256(schema).hexdigest() == pin["sha256"]
    assert pin["repository"] == "livepeer/node"
    assert pin["path"] == "docs/discovery.schema.json"
    assert pin["commit"] == "f45d83fd68f73d195dffe8b45f6cac296d023c49"
    baseline = Path("docs/references/upstream-baseline.md").read_text()
    assert pin["commit"] in baseline
    assert pin["sha256"] in baseline


_json = st.recursive(
    st.one_of(
        st.none(),
        st.booleans(),
        st.integers(min_value=-(10**6), max_value=10**6),
        st.text(max_size=24),
    ),
    lambda children: st.one_of(
        st.lists(children, max_size=3),
        st.dictionaries(st.text(min_size=1, max_size=12), children, max_size=4),
    ),
    max_leaves=12,
)
_valid_runner = {
    "url": "https://orch.example/app",
    "app": "example",
    "observed_at": "2026-10-22T18:05:00.123+01:00",
}


@settings(max_examples=40, deadline=None)
@given(
    record=st.one_of(
        st.just(_valid_runner), st.dictionaries(st.text(min_size=1, max_size=12), _json, max_size=6)
    )
)
def test_a_runner_is_kept_exactly_when_the_schema_accepts_it(record: dict[str, object]) -> None:
    body = json.dumps([{"address": "https://orch.example/deployment", "runners": [record]}])
    accepted = bool(_RUNNER.is_valid(loads_inventory(json.dumps(record))))
    assert (len(read_inventory(body)) == 1) is accepted
