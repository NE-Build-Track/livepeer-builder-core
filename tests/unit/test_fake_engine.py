"""FakeEngine scripting that the real engine cannot be driven into on demand (the contract test covers the rest)."""

from uuid import uuid4

import pytest

from livepeer_builder import ActorContext, JobRequest, NoCapacity, ProviderUnavailable
from livepeer_builder.testing import FakeEngine, Raises

ALICE = ActorContext(actor_id="alice", permissions=frozenset({"jobs.run", "costs.read"}))


async def test_a_scripted_engine_error_is_raised_from_run_and_stream() -> None:
    engine = FakeEngine(replies={("chat", ""): Raises(NoCapacity("busy"))}, streaming=True)
    streaming = engine.jobs.streaming
    assert streaming is not None
    with pytest.raises(NoCapacity, match="busy"):
        await engine.jobs.run(ALICE, JobRequest(app="chat"))
    with pytest.raises(NoCapacity, match="busy"):
        await streaming.stream(ALICE, JobRequest(app="chat"))


async def test_a_scripted_engine_error_is_raised_for_a_cost() -> None:
    job_id = uuid4()
    engine = FakeEngine(costs={job_id: Raises(ProviderUnavailable("no usage source"))})
    with pytest.raises(ProviderUnavailable, match="no usage source"):
        await engine.costs.for_job(ALICE, job_id)
