"""The vendored livepeer/node discovery schema: pin, fixtures, and a differential check (D63)."""

import hashlib
import json
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from livepeer_builder.adapters.discovery_inventory import (
    document_is_valid,
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


_VERDICTS = json.loads(Path(__file__).with_name("discovery_verdicts.json").read_text())
_CASES = _VERDICTS["cases"]
_REQUIRED = {"url": '"https://orch.example/app"', "app": '"example"', "observed_at": '"2026-10-22T18:05:00Z"'}


def _fields(fields: list[tuple[str, str]]) -> str:
    """Runner fields as JSON text. Dotted names (``price_info.price``) share one nested object."""
    grouped: dict[str, list[str]] = {}
    nested: set[str] = set()
    for name, value in fields:
        parent, dot, child = name.partition(".")
        if dot:
            nested.add(parent)
            grouped.setdefault(parent, []).append(f'"{child}":{value}')
        else:
            grouped[parent] = [value]
    return ",".join(
        f'"{name}":' + ("{" + ",".join(values) + "}" if name in nested else values[0])
        for name, values in grouped.items()
    )


def _document(case: dict[str, object]) -> str:
    """scripts/discovery-verdicts Document(): one entry, one runner, the case's value in its place."""
    field, value = str(case["field"]), str(case["value"])
    address = value if field == "address" else '"https://orch.example/deployment"'
    fields = [(name, value if name == field else default) for name, default in _REQUIRED.items()]
    if field != "address" and field not in _REQUIRED:
        fields.append((field, value))
    return '[{"address":' + address + ',"runners":[{' + _fields(fields) + "}]}]"


def test_the_verdicts_come_from_nodes_validator_and_go() -> None:
    assert "santhosh-tekuri/jsonschema/v6 v6.0.3 with AssertFormat" in _VERDICTS["validator"]
    assert "go1.27.3" in _VERDICTS["validator"]


@pytest.mark.parametrize(
    "case", _CASES, ids=[f"{case['field']}={str(case['value'])[:40]}" for case in _CASES]
)
def test_the_reader_agrees_with_node_on_every_recorded_verdict(case: dict[str, object]) -> None:
    """Each verdict is node's own (scripts/discovery-verdicts): the reader keeps the runner exactly then."""
    body = _document(case)
    assert body == case["document"], "this test and the verdict generator must build the same document"
    assert document_is_valid(body) is case["valid"]
    assert len(read_inventory(body)) == (1 if case["valid"] else 0)


def _pool(field: str) -> list[tuple[str, bool]]:
    return [(str(case["value"]), bool(case["valid"])) for case in _CASES if case["field"] == field]


_POOLS = {field: _pool(field) for field in sorted({str(case["field"]) for case in _CASES} - {"address"})}
_ADDRESSES = _pool("address")


@st.composite
def _runner(draw: st.DrawFn) -> tuple[str, bool]:
    """A runner built from recorded values: valid exactly when every value is, and url, app and
    observed_at are all present."""
    names = draw(st.lists(st.sampled_from(sorted(_POOLS)), unique=True, max_size=6))
    for required in _REQUIRED:
        if required not in names and draw(st.integers(0, 9)) > 0:
            names.append(required)
    picked = [(name, draw(st.sampled_from(_POOLS[name]))) for name in names]
    valid = all(ok for _, (_, ok) in picked) and set(_REQUIRED) <= set(names)
    return _fields([(name, value) for name, (value, _) in picked]), valid


@settings(max_examples=300, deadline=None)
@given(
    entries=st.lists(
        st.tuples(st.sampled_from(_ADDRESSES), st.lists(_runner(), max_size=3)), min_size=0, max_size=3
    )
)
def test_a_runner_is_kept_exactly_when_node_would_accept_it(
    entries: list[tuple[tuple[str, bool], list[tuple[str, bool]]]],
) -> None:
    """Node's verdicts are the oracle, not the reader's own validator: a runner is kept exactly when its
    entry's address and every one of its values passed node, and the document is valid exactly when
    every entry is."""
    body = (
        "["
        + ",".join(
            '{"address":'
            + address
            + ',"runners":['
            + ",".join("{" + text + "}" for text, _ in runners)
            + "]}"
            for (address, _), runners in entries
        )
        + "]"
    )
    kept = sum(ok for (_, address_ok), runners in entries if address_ok for _, ok in runners)
    every = all(address_ok and all(ok for _, ok in runners) for (_, address_ok), runners in entries)
    assert len(read_inventory(body)) == kept
    assert document_is_valid(body) is every


def test_an_integer_past_64_bits_keeps_the_runner_and_drops_that_field() -> None:
    body = _document({"field": "capacity", "value": "1e400"})
    (runner,) = read_inventory(body)
    assert runner.capacity is None


def test_a_lowercase_stamp_is_read_and_a_leap_second_leaves_none() -> None:
    (lower,) = read_inventory(_document({"field": "observed_at", "value": '"2026-10-22t18:05:00.5z"'}))
    assert (
        lower.observed_at is not None and lower.observed_at.isoformat() == "2026-10-22T18:05:00.500000+00:00"
    )
    (leap,) = read_inventory(_document({"field": "observed_at", "value": '"2026-10-22T23:59:60Z"'}))
    assert leap.observed_at is None
