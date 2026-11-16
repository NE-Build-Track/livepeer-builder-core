"""#114 C13: a payment challenge whose manifest id the store cannot keep is refused before signing.

The orchestrator chooses the manifest id, and the engine stores it before the signer is asked (D54). One
longer than 200 characters, or holding a NUL, is text PostgreSQL refuses: the intent write failed and the
job ended ``payment`` with no failover; on SQLite it was signed and paid. The challenge is now refused
as ``payment_refused``, which is about this orchestrator only, so the job may fail over.
"""

import json
from collections.abc import AsyncIterator

import pytest
from aiohttp import web

from livepeer_builder.contracts import ActorContext, JobRequest
from livepeer_builder.errors import JobFailed, RunnerCallError
from tests.conftest import EngineFactory
from tests.unit.livepeer_net import Net, start
from tests.unit.test_runner_call import call
from tests.unit.test_signing_intent import paid_engine, states

APP = "example/echo"
UNSTORABLE = {
    "201 characters": "m" * 201,
    "NUL": "m\x00",
    "control": "m\x1f",
    "newline": "m\n",
    "C1 control": "m\x85",
    "lone surrogate": "m\ud800",
}


@pytest.fixture
async def net() -> AsyncIterator[Net]:
    async for value in start():
        yield value


def challenge(net: Net, manifest_id: str) -> bytes:
    body = {"payment_params": "params", "manifest_id": manifest_id, "payment_url": f"{net.url}/pay/m"}
    return json.dumps(body).encode()


@pytest.mark.parametrize("name", UNSTORABLE)
async def test_a_challenge_with_a_manifest_id_the_store_cannot_keep_is_refused_before_signing(
    net: Net, name: str
) -> None:
    net.challenge_body = challenge(net, UNSTORABLE[name])
    with pytest.raises(RunnerCallError) as refused:
        await call(net)
    error = refused.value
    assert (error.kind, error.status_code, error.payment_sent) == ("payment_refused", 402, False)
    assert error.manifest_ids == ()
    assert net.log == ["signer info"]  # no intent stored, the signer never asked to pay, nothing sent paid
    assert UNSTORABLE[name] not in str(error)  # the id is the orchestrator's text: never quoted


async def test_a_manifest_id_of_exactly_200_characters_is_signed(net: Net) -> None:
    manifest = "m" * 200
    net.challenge_body = challenge(net, manifest)
    net.challenge_paid = False
    reply = await call(net)
    assert (reply.status_code, reply.manifest_ids, reply.payment_sent) == (200, (manifest,), True)


@pytest.mark.parametrize("name", ["201 characters", "NUL"])
async def test_a_job_fails_over_from_an_orchestrator_whose_manifest_id_cannot_be_stored(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, net: Net, name: str
) -> None:
    engine, _ = await paid_engine(make_engine, admin, net)  # two runners on two orchestrators
    net.challenge_body = challenge(net, UNSTORABLE[name])
    answer = net._app

    async def bad_challenge_once(request: web.Request) -> web.StreamResponse:
        response = await answer(request)
        net.challenge_body = None  # the next orchestrator's challenge is a good one
        return response

    net._app = bad_challenge_once  # type: ignore[method-assign]
    result = await engine.jobs.run(alice, JobRequest(app=APP))
    job = await engine.jobs.get(alice, result.job.id)
    first, second = job.attempts
    assert (job.state, first.outcome, second.outcome) == ("succeeded", "payment_refused", "succeeded")
    assert (first.payment_sent, states(first)) == (False, [])  # nothing signed, no manifest stored
    assert (second.payment_sent, states(second)) == (True, [("m-1", "paid")])


async def test_a_job_whose_every_orchestrator_sends_such_a_manifest_id_fails_unpaid(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, net: Net
) -> None:
    engine, _ = await paid_engine(make_engine, admin, net)
    net.challenge_body = challenge(net, "m" * 201)
    with pytest.raises(JobFailed) as failed:
        await engine.jobs.run(alice, JobRequest(app=APP))
    job = await engine.jobs.get(alice, failed.value.job.id)
    assert job.failure is not None
    assert (job.state, job.failure.kind, job.failure.payment_sent) == ("failed", "payment", False)
    assert [attempt.outcome for attempt in job.attempts] == ["payment_refused", "payment_refused"]
    assert not any(entry.startswith("sign m") for entry in net.log)
