"""The discovery document keeps runner objects as received and checks only the envelope."""

import pytest

from livepeer_builder.discovery.document import discovery_document, runner_records


def test_a_non_object_runner_is_dropped_and_the_last_runners_field_wins() -> None:
    body = b'[{"runners":[{"dropped":true}],"runners":[1,true,{"url":"u","price_usd":1e-999999}]}]'
    assert runner_records(body) == ['{"url":"u","price_usd":1e-999999}']


def test_a_nodes_entry_and_a_null_runners_contribute_nothing() -> None:
    body = b'[{"nodes":[{"url":"https://o"}]},{"address":"https://o","runners":null},{"runners":[]}]'
    assert runner_records(body) == []


def test_trailing_json_is_refused() -> None:
    with pytest.raises(ValueError, match="JSON list"):
        runner_records(b"[][]")


def test_the_served_document_embeds_runner_text_without_quoting_it() -> None:
    runner = '{"price_usd":1e-999999}'
    other = '{"price":1e-999999}'
    assert discovery_document([{"address": 'https://a.example/"b', "runners": [runner, other]}]) == (
        '[{"address":"https://a.example/\\"b","runners":[{"price_usd":1e-999999},{"price":1e-999999}]}]'
    )
