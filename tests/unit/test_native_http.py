"""Raw HTTP contract and paid-engine behavior, without a network or live inference."""

import hashlib
import inspect
import json
from types import SimpleNamespace

import pytest
from livepeer_gateway.live_runner import call_runner
from multidict import CIMultiDict
from pydantic import ValidationError

from livepeer_builder import ActorContext, EngineSupply, JobRequest
from livepeer_builder.adapters.sdk import SdkRunnerTransport
from livepeer_builder.errors import OperationConflict, RunnerCallError
from livepeer_builder.services.request_identity import request_fingerprint
from livepeer_builder.testing import (
    FakeEngine,
    Reply,
    ScriptedStreamTransport,
    StreamScript,
    make_engine,
    runner,
)


def raw_request(**changes):
    return JobRequest(
        app="chat", raw_body=b' {"model":"m", "new":true} ', content_type="application/json", **changes
    )


@pytest.mark.parametrize("body", [b"", b"\x00\xff\xfe", b' {"unknown":1} '])
def test_binary_rest_roundtrip_and_hidden_repr(body):
    request = JobRequest(app="x", raw_body=body, content_type="application/octet-stream")
    assert JobRequest.model_validate_json(request.model_dump_json()) == request
    assert "raw_body=" not in repr(request)
    assert request.raw_body == body


@pytest.mark.parametrize(
    "values",
    [
        {"raw_body": b"a"},
        {"raw_body": b"", "payload": {}, "content_type": "application/json"},
        {"raw_body": "string", "content_type": "text/plain"},
        {"raw_body": b"a" * 8388609, "content_type": "text/plain"},
        {"content_type": "text/plain\r\nx-secret: x"},
        {"headers": (("Authorization", "secret"),)},
        {"headers": (("Livepeer-Session-Token", "secret"),)},
        {"headers": (("Content-Type", "application/json"),)},
        {"headers": (("X-Good", "bad\nvalue"),)},
        {"headers": (("bad name", "value"),)},
        {"headers": (("x-a", "1"), ("X-A", "2"))},
        {"headers": tuple((f"x-{i}", "v") for i in range(33))},
        {"headers": (("x-large", "a" * 16384),)},
    ],
)
def test_invalid_native_contract(values):
    with pytest.raises(ValidationError):
        JobRequest(app="x", **values)


def test_legacy_identity_stays_stable_and_raw_changes_conflict():
    legacy = JobRequest(app="chat", payload={"x": 1})
    old = legacy.model_dump(mode="json", include={"app", "path", "method", "payload", "capability", "model"})
    assert (
        request_fingerprint(legacy)
        == hashlib.sha256(json.dumps(old, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    )
    raw = raw_request()
    variants = [
        raw.model_copy(update={"raw_body": raw.raw_body + b" "}),
        raw.model_copy(update={"content_type": "application/octet-stream"}),
        raw_request(headers=(("accept", "application/json"),)),
    ]
    assert all(request_fingerprint(v) != request_fingerprint(raw) for v in variants)
    assert request_fingerprint(raw_request(headers=(("X-B", "2"), ("X-A", "1")))) == request_fingerprint(
        raw_request(headers=(("x-a", "1"), ("x-b", "2")))
    )


@pytest.mark.parametrize("body", [b"", b"\xff\x00", b' {"new":true} '])
async def test_adapter_passes_original_bytes_and_content_type(body):
    seen = []

    async def call(url, **kwargs):
        seen.append(kwargs)
        return SimpleNamespace(content=b"{}", data={}, content_type="application/json")

    request = JobRequest(app="chat", raw_body=body, content_type="application/json")
    await SdkRunnerTransport(None, call=call).call(runner("chat"), request, None)
    assert seen[0]["payload"] is body
    assert seen[0]["content_type"] == "application/json"
    assert set(seen[0]) <= set(inspect.signature(call_runner).parameters)


async def test_stream_headers_keep_repeated_values_and_close_sdk():
    seen = []
    closed = []

    async def close():
        closed.append(True)

    async def call(url, **kwargs):
        seen.append(kwargs)
        return SimpleNamespace(
            status=200,
            content_type="text/event-stream",
            headers=CIMultiDict([("X-Trace", "a"), ("X-Trace", "b")]),
            aclose=close,
        )

    stream = await SdkRunnerTransport(None, call=call).open(runner("chat"), raw_request(), None)
    assert stream.headers == (("X-Trace", "a"), ("X-Trace", "b"))
    assert seen[0]["payload"] == raw_request().raw_body and seen[0]["stream"]
    await stream.aclose()
    assert closed == [True]


async def test_custom_headers_fail_before_sdk_dispatch():
    async def forbidden(*args, **kwargs):
        pytest.fail("SDK must not be called")

    transport = SdkRunnerTransport("https://signer", call=forbidden)
    for method in [transport.call, transport.open]:
        with pytest.raises(RunnerCallError, match="custom runner headers") as exc:
            await method(
                runner("chat"), raw_request(headers=(("anthropic-version", "2023-06-01"),)), "secret"
            )
        assert not exc.value.payment_sent


async def test_raw_identity_and_headers_through_real_engine():
    transport = ScriptedStreamTransport(
        opens=[StreamScript(events=[b"raw"], headers=(("X-Trace", "one"), ("X-Trace", "two")))]
    )
    async with make_engine(
        runners=[runner("chat")],
        supply=EngineSupply(transport=transport),
        allocation_mode="per_application",
        shared_payment_key="fixture",
        shared_allocation_id="fixture",
    ) as engine:
        actor = ActorContext(
            actor_id="a", application_id="app", permissions=frozenset({"jobs.run", "jobs.read"})
        )
        request = raw_request(operation_ref="op")
        stream = await engine.jobs.streaming.stream(actor, request)
        async with stream:
            assert stream.headers == (("X-Trace", "one"), ("X-Trace", "two"))
            assert b"".join([chunk async for chunk in stream]) == b"raw"
        assert stream.job.state == "succeeded"
        assert "raw_body" not in stream.job.model_dump_json()
        with pytest.raises(OperationConflict):
            await engine.jobs.streaming.stream(actor, request.model_copy(update={"raw_body": b"different"}))
        assert len(transport.calls) == 1


async def test_fake_engine_exposes_headers():
    engine = FakeEngine(streaming=True, replies={("chat", ""): Reply(headers=(("X-Trace", "fake"),))})
    actor = ActorContext(actor_id="a")
    stream = await engine.jobs.streaming.stream(actor, raw_request())
    assert stream.headers == (("X-Trace", "fake"),)
    await stream.aclose()
