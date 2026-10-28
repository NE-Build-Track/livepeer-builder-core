"""Raw HTTP contract and paid-engine behavior, without a network or live inference."""

import hashlib
import json

import pytest
from pydantic import ValidationError

from livepeer_builder import ActorContext, EngineSupply, JobRequest
from livepeer_builder.errors import OperationConflict
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


@pytest.mark.parametrize("status", [307, 400, 429, 500])
@pytest.mark.parametrize("ending", ["eof", "complete", "disconnect"])
async def test_returned_http_errors_are_failed_jobs_not_successes(status, ending):
    transport = ScriptedStreamTransport(opens=[StreamScript(events=[b"backend error"], status_code=status)])
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
        stream = await engine.jobs.streaming.stream(actor, raw_request(return_http_errors=True))
        async with stream:
            if ending == "eof":
                assert b"".join([part async for part in stream]) == b"backend error"
            elif ending == "complete":
                await stream.complete()
            else:
                await stream.disconnected()
        assert stream.job.state == "failed"
        assert stream.job.failure.status_code == status
        assert stream.job.failure.body is None
        assert stream.job.attempts[-1].payment_sent
        assert stream.job.attempts[-1].outcome == "http"
        assert b"".join([part async for part in stream]) == b""
        assert len(transport.calls) == 1


async def test_error_policy_requires_stream_api_and_changes_identity():
    from livepeer_builder.errors import InvalidRequest

    request = raw_request(return_http_errors=True)
    assert request_fingerprint(request) != request_fingerprint(raw_request())
    async with make_engine() as engine:
        with pytest.raises(InvalidRequest, match="streaming"):
            await engine.jobs.run(ActorContext(actor_id="a"), request)


@pytest.mark.parametrize(("status", "kind"), [(307, "runner_error"), (400, "runner_rejected")])
async def test_fake_native_error_matches_engine_failure(status, kind):
    engine = FakeEngine(
        streaming=True,
        runners=[runner("chat")],
        replies={("chat", ""): Reply(status_code=status, chunks=(b"error",))},
    )
    stream = await engine.jobs.streaming.stream(
        ActorContext(actor_id="a"), raw_request(return_http_errors=True)
    )
    assert b"".join([c async for c in stream]) == b"error"
    assert stream.job.state == "failed"
    assert stream.job.failure.status_code == status
    assert stream.job.failure.kind == kind
    assert stream.job.attempts[-1].payment_sent
    assert stream.job.attempts[-1].outcome == "http"
    await stream.aclose()


def test_rest_schema_limit_matches_encoded_body_limit():
    request = JobRequest(app="x", raw_body=b"a" * 8388608, content_type="application/octet-stream")
    encoded = request.model_dump_json()
    assert JobRequest.model_validate_json(encoded) == request
    field = JobRequest.model_json_schema()["properties"]["raw_body"]
    assert len(json.loads(encoded)["raw_body"]) == field["maxLength"] == 11184812


async def test_fake_rejects_buffered_error_policy_before_recording():
    from livepeer_builder.errors import InvalidRequest

    engine = FakeEngine()
    with pytest.raises(InvalidRequest, match="streaming"):
        await engine.jobs.run(ActorContext(actor_id="a"), raw_request(return_http_errors=True))
    assert not engine.jobs.requests
