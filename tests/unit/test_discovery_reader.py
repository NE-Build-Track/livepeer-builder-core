"""The discovery reader against livepeer/node's own output and its own test cases at the pin (D63).

The reader takes the fields the engine stores; it does not validate node's schema. These tests anchor it
to the node commit ``docs/references/upstream-baseline.md`` records: ``node_discovery_eee09c4.json`` is
what that commit's ``livepeer-discovery`` served at ``/discovery`` for ai1 and xodeapp on 18 November.
When the pin moves, capture that file again and keep these tests green.
"""

import json
from decimal import Decimal
from pathlib import Path

import pytest

from livepeer_builder.adapters.discovery_inventory import UnreadableInventory, read_inventory
from livepeer_builder.adapters.runner_call import PaidRequest, PaymentEvents
from livepeer_builder.contracts import Runner
from livepeer_builder.errors import RunnerCallError

_NODE_DOCUMENT = Path(__file__).with_name("node_discovery_eee09c4.json").read_text()
_RUNNER = '{"url":"https://orch.example/app","app":"example","observed_at":"2026-10-22T18:05:00.123+01:00"}'
_METADATA = '[{"nodes":[{"url":"https://orch.example","queried_at":"2026-10-22T18:05:00Z"}]}]'


def _envelope(record: str, address: str = "https://orch.example/deployment") -> str:
    return '[{"address":"' + address + '","runners":[' + record + "]}]"


def _skips_its_only_record(body: str) -> bool:
    """The reader skipped the document's one record. With no runner read, that is an answer the engine
    could not read, not an empty inventory (#114, C28)."""
    with pytest.raises(UnreadableInventory, match=r"no readable runner: \d entries and \d runners skipped"):
        read_inventory(body)
    return True


def test_every_runner_node_serves_is_read_with_its_exact_price() -> None:
    document = json.loads(_NODE_DOCUMENT, parse_float=Decimal, parse_int=Decimal)
    served = [
        (entry["address"], runner) for entry in document if "address" in entry for runner in entry["runners"]
    ]
    read = read_inventory(_NODE_DOCUMENT)
    assert len(read) == len(served) == 26
    for runner, (address, raw) in zip(read, served, strict=True):
        # The dump uses the same host for address and every runner URL, so the address is kept.
        assert (runner.url, runner.app, runner.orchestrator_url) == (raw["url"], raw["app"], address)
        advertised = raw.get("price_info") or {}
        assert runner.price_info is not None
        assert runner.price_info.price == advertised.get("price")
        assert runner.price_info.price_usd == advertised.get("price_usd")
        assert (runner.price_info.currency, runner.price_info.unit) == (
            advertised["currency"],
            advertised["unit"],
        )


def test_a_claimed_address_on_another_host_is_not_a_payment_origin() -> None:
    """livepeer-discovery republishes the claimed address; a challenge on that host must not be signed."""
    claimed = "https://claimed.example:8935"
    (runner,) = read_inventory(_envelope(_RUNNER, claimed))
    assert runner.orchestrator_url is None
    paid = PaidRequest(
        runner.url,
        method="POST",
        body=b"{}",
        app="example",
        rate=None,
        signer_url=None,
        signer_headers=None,
        retries=0,
        events=PaymentEvents(),
        orchestrator_url=runner.orchestrator_url,
    )
    challenge = json.dumps(
        {"payment_params": "p", "manifest_id": "m-1", "payment_url": f"{claimed}/session/m-1/payment"}
    )
    with pytest.raises(RunnerCallError, match="invalid payment challenge") as refused:
        paid._challenge(challenge)
    assert (refused.value.kind, refused.value.status_code, refused.value.payment_sent) == (
        "http",
        402,
        False,
    )
    assert (
        paid._challenge(
            json.dumps(
                {
                    "payment_params": "p",
                    "manifest_id": "m-1",
                    "payment_url": "https://orch.example/session/m-1/payment",
                }
            )
        ).manifest_id
        == "m-1"
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
    assert _skips_its_only_record(body)


def _with(fields: str) -> str:
    return _envelope(_RUNNER.removesuffix("}") + "," + fields + "}")


# Cases livepeer/node added by v1.0.7 (eee09c4), by its own names: schema_test.go and, marked (v), its
# TestDiscoveryRunnerValidation. From that version node's decoder drops a runner with an optional field
# of the wrong type, a repeated key or a key in the wrong letter case (node #82), so behind its
# livepeer-discovery such a record never reaches the engine. The reader is not node's validator: it reads
# the fields the engine stores and treats a wrong one as absent (docs/contracts/discovery.md). Where the
# two agree the case is in _V107_SAME; where node rejects and the reader keeps, on purpose, it is below.
_V107_SAME = {
    "unknown properties": (
        _with('"gpu.name":null,"vers\u0131on":null,"extension":{"count":9007199254740993}'),
        True,
    ),
    "integer tokens": (
        _with('"capacity":4.0,"capacity_used":1e0,"capacity_available":9007199254740993'),
        True,
    ),
    "scaled integers": (
        _with('"capacity":1200.0e-2,"capacity_used":1.20E1,"capacity_available":-0.00e-100'),
        True,
    ),
    "large integer exponent (v)": (_with('"gpu":{"vram_mb":1e1000000}'), True),
    "overflow integer exponent (v)": (_with('"gpu":{"vram_mb":1e999999999999999999999}'), True),
    "zero with overflow exponent (v)": (_with('"gpu":{"vram_mb":0e-999999999999999999999}'), True),
    "wrong URL field case": (_envelope(_RUNNER.replace('"url":', '"URL":')), False),
    "comma observation fraction": (_envelope(_RUNNER.replace("00.123", "00,123")), False),
    "invalid observation offset": (_envelope(_RUNNER.replace("+01:00", "+24:00")), False),
}
_V107_KEPT_THOUGH_NODE_REJECTS = {
    "capacity string": _with('"capacity":"4"'),
    "fractional capacity": _with('"capacity_available":1.5'),
    "version number": _with('"version":1'),
    "null metadata": _with('"metadata":null'),
    "null GPU": _with('"gpu":null'),
    "GPU name number": _with('"gpu":{"name":1}'),
    "fractional VRAM": _with('"gpu":{"vram_mb":1.5}'),
    "null price": _with('"price_info":{"price":null}'),
    "large fractional exponent (v)": _with('"gpu":{"vram_mb":1e-1000000}'),
    "overflow fractional exponent (v)": _with('"gpu":{"vram_mb":1e-999999999999999999999}'),
    "case alias (v)": _with('"URL":"https://other.example/app"'),
    "Unicode case alias (v)": _with('"capacity_used":1,"capacity_u\u017fed":9'),
    "nested Unicode case alias (v)": _with('"price_info":{"price_usd":0.1,"price_u\u017fd":0.9}'),
    "optional field case (v)": _with('"Mode":"persistent"'),
    "duplicate field (v)": _with('"capacity":1,"capacity":2'),
    "duplicate extension field (v)": _with('"extension":{"x":1,"x":2}'),
    "invalid Unicode (v)": _with('"extension":"\\ud800"'),
}


@pytest.mark.parametrize(("body", "kept"), _V107_SAME.values(), ids=_V107_SAME.keys())
def test_the_reader_and_node_v1_0_7_agree(body: str, kept: bool) -> None:
    assert len(read_inventory(body)) == 1 if kept else _skips_its_only_record(body)


@pytest.mark.parametrize(
    "body", _V107_KEPT_THOUGH_NODE_REJECTS.values(), ids=_V107_KEPT_THOUGH_NODE_REJECTS.keys()
)
def test_a_record_node_v1_0_7_drops_for_a_field_the_reader_ignores_is_kept(body: str) -> None:
    (runner,) = read_inventory(body)
    assert (runner.url, runner.app) == ("https://orch.example/app", "example")


def test_what_the_reader_makes_of_the_wrong_fields_it_keeps_a_runner_with() -> None:
    def read(name: str) -> Runner:
        (runner,) = read_inventory(_V107_KEPT_THOUGH_NODE_REJECTS[name])
        return runner

    assert read("capacity string").capacity is None  # absent
    assert read("fractional capacity").capacity_available == 0  # present and no integer: tried last
    assert read("duplicate field (v)").capacity == 2  # the last one, as Python's json reads it
    assert read("case alias (v)").url == "https://orch.example/app"  # the lower-case key is the field
    assert read("Unicode case alias (v)").capacity_used == 1
    assert read("optional field case (v)").mode is None
    price = read("null price").price_info
    assert price is not None and price.price is None


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
    assert _skips_its_only_record(
        _envelope(_RUNNER.replace("https://orch.example/app", json.dumps(url)[1:-1]))
    )


def test_an_address_aiohttp_would_connect_to_under_another_name_skips_its_entry() -> None:
    assert _skips_its_only_record(_envelope(_RUNNER, "https://\u246027.0.0.1"))


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
    assert _skips_its_only_record(_envelope(_RUNNER.replace("2026-10-22T18:05:00.123+01:00", stamp)))
