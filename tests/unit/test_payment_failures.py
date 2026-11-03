"""ISS-12, D58: a payment failure says why, in the engine's words.

``Failure.kind`` keeps deciding behavior (failover, cost); ``Failure.reason`` says what the builder can
do: ``not_authorized`` (signer 401 or 403), ``allowance_exhausted`` (402), ``signer_unavailable`` (the
signer could not be reached, or failed, before anything was signed) and ``signer_unknown`` (it was asked
and may have charged). The engine, the Live Runner transport and the SDK's signer client are real; only
the network is local (tests/unit/livepeer_net.py).
"""

import logging
import socket
from collections.abc import AsyncIterator

import aiohttp
import pytest
from livepeer_gateway.errors import LivepeerGatewayError, LivepeerHTTPError
from livepeer_gateway.http import post_json
from pydantic import ValidationError

from livepeer_builder import EngineSettings
from livepeer_builder.adapters.signer_failures import never_reached_signer
from livepeer_builder.contracts import ActorContext, Failure, JobRequest
from livepeer_builder.errors import JobFailed, OperationExists, RunnerCallError
from livepeer_builder.services.failover import failure_of
from tests.conftest import EngineFactory
from tests.unit.livepeer_net import Net, identity_only_signer, start
from tests.unit.test_signing_intent import paid_engine, states

APP = "example/echo"


@pytest.fixture
async def net() -> AsyncIterator[Net]:
    async for value in start():
        yield value


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (401, "not_authorized"),  # Batteries: invalid or revoked key
        (403, "not_authorized"),  # grant or allocation inactive (paused too), or a binding mismatch
        (402, "allowance_exhausted"),  # the allocation is spent
    ],
)
async def test_a_signer_refusal_says_why_is_unpaid_and_does_not_fail_over(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, net: Net, status: int, reason: str
) -> None:
    engine, _ = await paid_engine(make_engine, admin, net)  # two runners on two orchestrators
    net.sign = [status]
    with pytest.raises(JobFailed) as failed:
        await engine.jobs.run(alice, JobRequest(app=APP))
    job = await engine.jobs.get(alice, failed.value.job.id)
    assert job.failure is not None
    assert (job.state, job.failure.kind, job.failure.reason) == ("failed", "payment", reason)
    assert len(job.attempts) == 1  # every orchestrator shares the signer: no failover
    assert states(job.attempts[0]) == [("m-1", "unpaid")] and not job.attempts[0].payment_sent


def closed_port_url() -> str:
    """A loopback URL nothing listens on: connecting is refused."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    return f"http://127.0.0.1:{port}"


async def sdk_error(net: Net, case: str) -> LivepeerGatewayError:
    """What the pinned SDK's ``post_json`` (every signer call goes through it) raises in each case."""
    payload = {"ManifestID": "m-0", "orchestrator": "params", "type": "fixed"}
    url, timeout = f"{net.url}/generate-live-payment", 5.0
    if case == "refused":
        url = f"{closed_port_url()}/generate-live-payment"
    elif case == "dns":
        url = "http://signer.invalid/generate-live-payment"  # RFC 6761: never resolves
    elif case == "timeout":
        net.sign, timeout = [5.0], 0.2  # the signer took the request and does not answer in time
    elif case == "drop":
        net.sign = ["drop"]  # the connection closes after the request was sent
    elif case == "5xx":
        net.sign = [502]
    with pytest.raises(LivepeerGatewayError) as raised:
        await post_json(url, payload, timeout=timeout)
    return raised.value


@pytest.mark.parametrize(
    ("case", "cause", "never_reached"),
    [
        ("refused", aiohttp.ClientConnectorError, True),  # connect phase: nothing was sent
        ("dns", aiohttp.ClientConnectorDNSError, True),
        ("timeout", TimeoutError, False),  # sent: the signer may have signed (D54)
        ("drop", aiohttp.ServerDisconnectedError, False),
        ("5xx", None, False),  # it answered: LivepeerHTTPError, no cause
    ],
)
async def test_only_a_connect_phase_failure_never_reached_the_signer(
    net: Net, case: str, cause: type[BaseException] | None, never_reached: bool
) -> None:
    """Q3: a request the signer never received cannot have charged. The SDK wraps every failure in a
    ``LivepeerGatewayError`` and chains aiohttp's own error, so the engine reads the chain."""
    error = await sdk_error(net, case)
    if cause is None:
        assert isinstance(error, LivepeerHTTPError) and error.__cause__ is None
    else:
        assert isinstance(error.__cause__, cause)
    assert never_reached_signer(error) is never_reached


async def test_a_signer_that_refuses_connections_before_signing_is_unavailable_and_does_not_fail_over(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, net: Net
) -> None:
    """At sign-orchestrator-info nothing is signed yet, and every runner shares the signer."""
    engine, _ = await paid_engine(make_engine, admin, net, signer_url=closed_port_url())
    with pytest.raises(JobFailed) as failed:
        await engine.jobs.run(alice, JobRequest(app=APP))
    job = await engine.jobs.get(alice, failed.value.job.id)
    assert job.failure is not None
    assert (job.state, job.failure.kind, job.failure.reason) == ("failed", "payment", "signer_unavailable")
    assert [(a.outcome, a.payment_sent) for a in job.attempts] == [("payment", False)]
    assert not any(entry.startswith("request") for entry in net.log)  # no runner was asked to work


@pytest.mark.parametrize("step", [502, 503, "drop"])
async def test_a_signer_that_fails_before_signing_is_unavailable_and_does_not_fail_over(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, net: Net, step: int | str
) -> None:
    engine, _ = await paid_engine(make_engine, admin, net)
    net.info = [step, step]  # the identity is cached only once it was read
    with pytest.raises(JobFailed) as failed:
        await engine.jobs.run(alice, JobRequest(app=APP))
    job = await engine.jobs.get(alice, failed.value.job.id)
    assert job.failure is not None
    assert (job.state, job.failure.kind, job.failure.reason) == ("failed", "payment", "signer_unavailable")
    assert [(a.outcome, a.payment_sent, a.sessions) for a in job.attempts] == [("payment", False, ())]
    assert "challenge m-1" not in net.log  # no runner was asked to work


async def test_a_signer_that_refuses_the_payment_connection_is_unavailable_and_the_manifest_unpaid(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, net: Net
) -> None:
    """Q3: the request to sign never reached the signer, so nothing was signed. The manifest stored
    before signing (D54) becomes ``unpaid``, the job ``failed``, and no other runner is tried."""
    async with identity_only_signer() as signer:
        signer_url = str(signer.make_url("")).rstrip("/")
        engine, _ = await paid_engine(make_engine, admin, net, signer_url=signer_url)
        net.on_challenge = signer.close  # the signer goes down after the engine read its identity
        with pytest.raises(JobFailed) as failed:
            await engine.jobs.run(alice, JobRequest(app=APP))
    job = await engine.jobs.get(alice, failed.value.job.id)
    assert job.failure is not None
    assert (job.state, job.failure.kind, job.failure.reason) == ("failed", "payment", "signer_unavailable")
    assert [(a.outcome, a.payment_sent) for a in job.attempts] == [("payment", False)]
    assert states(job.attempts[0]) == [("m-1", "unpaid")]
    assert (await engine.costs.for_job(alice, job.id)).status == "none"  # never pending: nothing was signed


@pytest.mark.parametrize("step", [502, "drop", "malformed"])
async def test_a_signer_that_gives_no_answer_while_signing_is_unknown(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, net: Net, step: int | str
) -> None:
    """D54 unchanged: it was asked and may have charged, so the job is ``uncertain`` and pays no one else."""
    engine, _ = await paid_engine(make_engine, admin, net)
    net.sign = [step]
    with pytest.raises(JobFailed) as failed:
        await engine.jobs.run(alice, JobRequest(app=APP))
    job = await engine.jobs.get(alice, failed.value.job.id)
    assert job.failure is not None
    assert (job.state, job.failure.kind, job.failure.reason) == (
        "uncertain",
        "payment_unknown",
        "signer_unknown",
    )
    assert len(job.attempts) == 1 and states(job.attempts[0]) == [("m-1", "unknown")]


@pytest.mark.parametrize(
    ("info", "sign", "message"),
    [
        ("ok", 401, "the signer refused payment: not authorized (HTTP 401)"),
        ("ok", 403, "the signer refused payment: not authorized (HTTP 403)"),
        ("ok", 402, "the signer refused payment: allowance exhausted (HTTP 402)"),
        (502, "ok", "the signer is unavailable (HTTP 502); nothing was signed"),
        ("drop", "ok", "the signer is unavailable: it could not be reached; nothing was signed"),
        ("ok", 502, "the signer gave no answer while signing (HTTP 502); it may have charged"),
        ("ok", "drop", "the signer gave no answer while signing; it may have charged"),
        ("ok", "malformed", "the signer gave no answer while signing; it may have charged"),
        ("malformed", "ok", "the signer's answer was not valid"),  # payment, no reason: nothing signed
        (404, "ok", "the signer refused payment (HTTP 404)"),  # at info: not unavailable, nothing signed
        ("ok", 404, "the signer gave no answer while signing (HTTP 404); it may have charged"),  # D54
    ],
)
async def test_a_payment_failure_is_told_in_the_engine_s_words(
    make_engine: EngineFactory,
    admin: ActorContext,
    alice: ActorContext,
    net: Net,
    info: int | str,
    sign: int | str,
    message: str,
) -> None:
    """The signer's URL and its own words go to the log only; ``body`` is the runner's, never the signer's."""
    engine, _ = await paid_engine(make_engine, admin, net)
    net.info, net.sign = [info, info], [sign]
    with pytest.raises(JobFailed) as failed:
        await engine.jobs.run(alice, JobRequest(app=APP))
    failure = failed.value.job.failure
    assert failure is not None
    assert (failure.message, failure.body, str(failed.value)) == (message, None, message)


@pytest.mark.parametrize(("info", "sign"), [("ok", 402), (502, "ok")])
async def test_the_signer_s_own_words_and_url_go_to_the_log(
    make_engine: EngineFactory,
    admin: ActorContext,
    alice: ActorContext,
    net: Net,
    caplog: pytest.LogCaptureFixture,
    info: int | str,
    sign: int | str,
) -> None:
    """What the operator needs to find the fault, and the builder must not see."""
    engine, _ = await paid_engine(make_engine, admin, net)
    net.info, net.sign = [info, info], [sign]
    with (
        caplog.at_level(logging.WARNING, logger="livepeer_builder.adapters.runner_call"),
        pytest.raises(JobFailed),
    ):
        await engine.jobs.run(alice, JobRequest(app=APP))
    [record] = [item for item in caplog.records if item.message == "signer error"]
    assert net.url in record.detail and "UPSTREAM-" in record.detail
    assert record.error_type == "LivepeerHTTPError"


@pytest.mark.parametrize(
    ("kind", "reason"),
    [
        ("timeout", "not_authorized"),  # a reason belongs to payment failures only
        ("not_provisioned", "allowance_exhausted"),  # its kind already says why
        ("payment", "signer_unknown"),  # unknown is payment_unknown: the job is uncertain
        ("payment_unknown", "allowance_exhausted"),  # a refusal is known: payment
    ],
)
def test_a_failure_refuses_a_reason_its_kind_cannot_have(kind: str, reason: str) -> None:
    with pytest.raises(ValidationError):
        Failure.model_validate({"kind": kind, "message": "m", "reason": reason})


def test_a_payment_failure_stored_before_reasons_existed_still_reads() -> None:
    assert Failure.model_validate({"kind": "payment", "message": "m"}).reason is None


@pytest.mark.parametrize(
    "url",
    [
        "https://engine:s3cret@signer.example",  # userinfo
        "engine:s3cret@signer.example:8081",  # the SDK reads a bare host:port as https
        "https://signer.example/?token=s3cret",  # a query
        "https://signer.example/#s3cret",  # a fragment
        "//engine:s3cret@signer.example",  # scheme-relative: the @ lands in the path (Cursor, #78)
        "  //engine:s3cret@signer.example:8081",  # the same, with leading spaces
        "signer.example/engine:s3cret@x",  # an @ anywhere: a base URL never needs one
    ],
)
def test_a_signer_url_that_could_carry_a_secret_is_refused_at_startup(url: str) -> None:
    """Error text quotes the signer URL in logs (D58): it must hold nothing secret. Nor does the startup
    error echo it."""
    with pytest.raises(ValidationError) as refused:
        EngineSettings.from_env({"LPB_SIGNER_URL": url})
    assert "signer_url" in str(refused.value) and "s3cret" not in str(refused.value)


@pytest.mark.parametrize(
    "url", ["http://signer:8081", "signer.example:8081", "https://signer.example/", None]
)
def test_a_plain_signer_url_is_accepted(url: str | None) -> None:
    assert EngineSettings.model_validate({"signer_url": url}).signer_url == url


async def test_a_refresh_past_its_budget_on_every_runner_is_told_in_the_engine_s_words(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, net: Net
) -> None:
    """Expired params on every orchestrator: each fails over (``payment_refused``), then the job is
    ``payment`` with no reason, as before D58."""
    engine, _ = await paid_engine(make_engine, admin, net, retries=0)
    net.sign = [480] * 4
    with pytest.raises(JobFailed) as failed:
        await engine.jobs.run(alice, JobRequest(app=APP))
    failure = failed.value.job.failure
    assert failure is not None and (failure.kind, failure.reason) == ("payment", None)
    assert (
        failure.message == "all 2 runners failed; last: the signer kept asking for fresh payment parameters"
    )


@pytest.mark.parametrize(("info", "sign"), [("ok", 402), ("ok", 401), (502, "ok")])
async def test_a_repeat_after_a_payment_refusal_runs_again_under_its_reference(
    make_engine: EngineFactory,
    admin: ActorContext,
    alice: ActorContext,
    net: Net,
    info: int | str,
    sign: int | str,
) -> None:
    """D57's closed list: a ``payment`` failure, whatever its reason, never reached a runner and paid
    nothing, so once the operator funds the allocation (or the signer is back) the same reference runs."""
    engine, _ = await paid_engine(make_engine, admin, net)
    net.info, net.sign = [info], [sign]  # once: then the signer is back
    request = JobRequest(app=APP, operation_ref="op-1")
    with pytest.raises(JobFailed) as failed:
        await engine.jobs.run(alice, request)
    result = await engine.jobs.run(alice, request)  # the signer now pays
    assert result.job.state == "succeeded" and result.job.id != failed.value.job.id


async def test_a_repeat_after_a_signer_that_may_have_charged_is_operation_exists(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, net: Net
) -> None:
    engine, _ = await paid_engine(make_engine, admin, net)
    net.sign = [502]
    request = JobRequest(app=APP, operation_ref="op-1")
    with pytest.raises(JobFailed) as failed:
        await engine.jobs.run(alice, request)
    assert failed.value.job.failure is not None and failed.value.job.failure.reason == "signer_unknown"
    with pytest.raises(OperationExists) as repeated:
        await engine.jobs.run(alice, request)
    assert repeated.value.job is not None and repeated.value.job.id == failed.value.job.id
    assert net.log.count("signer info") <= 1 and len(net.signer_requests) == 1  # nothing asked again


def test_a_transport_s_reason_that_does_not_fit_its_kind_is_dropped() -> None:
    """A custom transport's mistake must not turn a runner failure into an engine error."""
    mismatched = RunnerCallError("unreachable", "down", payment_sent=False, reason="not_authorized")
    failure = failure_of(mismatched, False)
    assert (failure.kind, failure.reason) == ("unreachable", None)
