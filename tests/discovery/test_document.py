"""The discovery document keeps runner objects as received and checks only the envelope."""

import pytest

from livepeer_builder.discovery.document import discovery_document, runner_records
from livepeer_builder.utils import loads_json_numbers


def test_a_non_object_runner_is_dropped_and_the_last_runners_field_wins() -> None:
    body = b'[{"runners":[{"dropped":true}],"runners":[1,true,{"url":"u","price_usd":1e-999999}]}]'
    assert runner_records(body) == ['{"url":"u","price_usd":1e-999999}']


def test_a_nodes_entry_and_a_null_runners_contribute_nothing() -> None:
    body = b'[{"nodes":[{"url":"https://o"}]},{"address":"https://o","runners":null},{"runners":[]}]'
    assert runner_records(body) == []


def test_a_number_decimal_cannot_parse_is_refused() -> None:
    """Float accepts ``1e1000000000000000000`` as ``inf`` and would re-serve the token.

    The engine parses the aggregated document with ``Decimal``, so one such number fails every refresh.
    """
    body = b'[{"runners":[{"url":"u","price":1e1000000000000000000},{"url":"kept","price":1}]}]'
    with pytest.raises(ValueError, match="not a JSON number"):
        runner_records(body)
    with pytest.raises(ValueError, match="not a JSON number"):
        loads_json_numbers(body)


def test_trailing_json_is_refused() -> None:
    with pytest.raises(ValueError, match="JSON list"):
        runner_records(b"[][]")


def test_the_served_document_embeds_runner_text_without_quoting_it() -> None:
    runner = '{"price_usd":1e-999999}'
    other = '{"price":1e-999999}'
    assert discovery_document([{"address": 'https://a.example/"b', "runners": [runner, other]}]) == (
        '[{"address":"https://a.example/\\"b","runners":[{"price_usd":1e-999999},{"price":1e-999999}]}]'
    )
