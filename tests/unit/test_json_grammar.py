"""``is_json`` reads the grammar the JSON decoder reads (#158, and the review of #189).

A usage item the decoder refuses for its size is left out of its page, and only if it is JSON: the
decoder stops at the limit it meets, so the rest of such an item is read by ``is_json``, which meets
neither limit. The two must agree on what JSON is, or an item that is none would be left out and its
page checkpointed. Here they are held to each other, on texts written by hand and on generated ones.
"""

import json

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from livepeer_builder.adapters.batteries_usage_rows import is_json

JSON = [
    "0", "-0", "1.5", "-1e9", "1E+2", "2e-3", "true", "false", "null", "NaN", "Infinity", "-Infinity",
    '""', '"a\\nb"', '"\\u00e9\\ud83d\\ude00"', '"\\"\\\\\\/"', '"é and 日本"',
    "[]", "{}", "[[]]", "[{}]", '{"a": []}', '{"a": {"b": [1, 2, {"c": null}]}}',
    ' \t\n\r[ 1 , 2 ]\n', '{ "a" : 1 , "b" : [ ] }', '[1,"a",true,null,{"x":[]}]',
    '{"a": 1, "a": 2}',  # a key named twice is JSON to the decoder
]  # fmt: skip
NOT_JSON = [
    "", " ", "[", "]", "{", "}", "[}", "{]", "[1,]", "[,1]", "[1 2]", "{,}", '{"a"}', '{"a":}', '{"a" 1}',
    '{"a": 1,}', "{1: 2}", "{'a': 1}", "'a'", "01", "1.", ".5", "+1", "1e", "0x10", "- 1", "--1",
    "tru", "True", "nul", "nan", "infinity", "+Infinity", '"', '"a', '"\\x"', '"\\u12"', '"\\u12g4"',
    '"a\nb"', '"a\tb"', '"\x00"', "1 2", "[] []", "{} x", "[1] ,", ",", ":", "[:]", '{"a": 1}}', "[[]",
    '{"a": [}', "\uff11", "[\uff11]", "1\x0b", "﻿1", "/* c */ 1", "[1, // c\n2]",
]  # fmt: skip


@pytest.mark.parametrize("text", JSON)
def test_what_the_decoder_reads_is_json(text: str) -> None:
    json.loads(text)
    assert is_json(text)


@pytest.mark.parametrize("text", NOT_JSON)
def test_what_the_decoder_refuses_as_no_json_is_no_json(text: str) -> None:
    with pytest.raises(json.JSONDecodeError):
        json.loads(text)
    assert not is_json(text)


def test_neither_of_the_decoders_limits_is_met() -> None:
    """What the decoder cannot say, for its own limits: these are JSON, and these are not."""
    long = "9" * 5000
    deep = "[" * 100_000 + "]" * 100_000
    for text in (long, f'{{"a": {long}}}', deep, '{"a": ' * 50_000 + "1" + "}" * 50_000, f"[{long}, {deep}]"):
        with pytest.raises((ValueError, RecursionError)) as refused:
            json.loads(text)
        assert not isinstance(refused.value, json.JSONDecodeError)
        assert is_json(text)
    for text in (
        f'{{"a": {long}, "b": oops}}',
        f"[{long},]",
        f"0{long}",
        f"{long}.",
        "[" * 100_000 + "}" * 100_000,
        "[" * 100_000 + "]" * 99_999,
        deep + "]",
        deep[:-1] + ", oops]",
    ):
        assert not is_json(text)


VALUES = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats() | st.text(),
    lambda inner: st.lists(inner, max_size=4) | st.dictionaries(st.text(max_size=5), inner, max_size=4),
    max_leaves=20,
)


@settings(max_examples=300, deadline=None)
@given(VALUES, st.sampled_from([None, 1, "\t"]), st.booleans())
def test_every_text_the_encoder_writes_is_json(
    value: object, indent: int | str | None, ascii_only: bool
) -> None:
    assert is_json(json.dumps(value, indent=indent, ensure_ascii=ascii_only))


@settings(max_examples=2000, deadline=None)
@given(st.text(alphabet='[]{}",:.0123456789eE+- \n\\/ubfnrtlasINiy\x01é', max_size=24))
def test_the_two_agree_on_any_text(text: str) -> None:
    try:
        json.loads(text)
        read = True
    except json.JSONDecodeError:
        read = False
    assert is_json(text) is read
