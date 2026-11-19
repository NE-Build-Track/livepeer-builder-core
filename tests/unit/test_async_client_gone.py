"""ISS-26, "the client going away after submit changes nothing": the job belongs to the worker, not to the
request that submitted it (docs/contracts/engine.md#running).
"""

import httpx

from livepeer_builder.contracts import ActorContext
from livepeer_builder.http import create_app
from livepeer_builder.testing import ScriptedTransport
from tests.conftest import EngineFactory, allot


async def test_a_client_that_goes_away_after_its_submit_changes_nothing(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, transport: ScriptedTransport
) -> None:
    engine = await make_engine()
    await allot(engine, alice)
    assert engine.keys is not None
    key = await engine.keys.issue(admin, kind="actor", actor_id="alice", operation_ref="key-alice")
    headers = {"Authorization": f"Bearer {key.token}"}
    app = create_app(engine, manage_lifecycle=False)
    body = {"app": "example/echo", "mode": "async", "payload": {"n": 1}}

    gone = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://engine")
    accepted = await gone.post("/v1/jobs", json=body, headers=headers)
    assert accepted.status_code == 202
    job_id = accepted.json()["id"]
    await gone.aclose()  # the client goes away and never comes back

    transport.steps = [ScriptedTransport.ok(b"kept for its owner")]
    assert await engine.jobs.worker.run_once() == 1
    assert len(transport.calls) == 1  # run once, by the worker

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://engine") as back:
        job = await back.get(f"/v1/jobs/{job_id}", headers=headers)
        assert (job.status_code, job.json()["state"]) == (200, "succeeded")
        result = await back.get(f"/v1/jobs/{job_id}/result", headers=headers)
        assert (result.status_code, result.content) == (200, b"kept for its owner")
