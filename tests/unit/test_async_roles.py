"""ISS-26 in each role (D84, #169): async jobs belong to the work half.

An engine in its ``both`` role queues a job and runs it (tests/unit/test_async_jobs.py), and one in its
``work`` role pays for it through a clearinghouse (tests/unit/test_async_paying.py). One in its
``clearinghouse`` role queues none and runs none, for an embedding caller too, before anything is
written. Its job routes, the cancel and the result route among them, are 404 there
(tests/unit/test_roles.py), and its worker's loop is never started
(``test_a_role_starts_the_machinery_of_its_half``). The refusal comes from admission, so it holds with
the transport the role is given (``IdentityOnlyTransport``), which could run no job at all.
"""

from uuid import uuid4

import pytest

from livepeer_builder import BuilderEngine
from livepeer_builder.adapters.sdk import IdentityOnlyTransport
from livepeer_builder.contracts import ActorContext, JobRequest
from livepeer_builder.errors import InvalidRequest, NotFound
from livepeer_builder.protocols import BoundedRunnerTransport
from livepeer_builder.testing import ScriptedStreamTransport
from tests.conftest import EngineFactory, allot
from tests.unit.role_engines import in_role
from tests.unit.test_role_start_rules import a_clearinghouse

QUEUED = JobRequest(app="example/echo", mode="async", payload={"n": 1})


async def test_a_clearinghouse_queues_no_job_and_its_worker_runs_none(
    make_engine: EngineFactory, alice: ActorContext
) -> None:
    """``jobs.submit`` and a step of the worker are refused as ``jobs.run`` is, before a job is recorded,
    a runner is called or a payment is asked for: a clearinghouse never runs jobs (D82). It holds no
    job, so the result and the cancel of any job are the ``NotFound`` of a missing one."""
    house = await in_role(make_engine, "clearinghouse")
    await allot(house, alice)
    transport = house.jobs._transport
    assert isinstance(transport, ScriptedStreamTransport)
    transport.steps = [ScriptedStreamTransport.ok()]

    with pytest.raises(InvalidRequest, match="clearinghouse role"):
        await house.jobs.submit(alice, QUEUED)
    with pytest.raises(InvalidRequest, match="clearinghouse role"):
        await house.jobs.worker.run_once()
    for handle in (house.jobs.result, house.jobs.cancel):
        with pytest.raises(NotFound):
            await handle(alice, uuid4())

    assert transport.calls == []
    assert await house.jobs.list_jobs(alice) == []
    assert await house.store.queued_stats() == (0, None)
    assert (house.health().queued_jobs, house.health().oldest_queued_s) == (0, None)


async def test_a_clearinghouse_on_the_transport_its_role_is_given_queues_no_job_either(
    alice: ActorContext,
) -> None:
    """From its settings alone, a clearinghouse holds ``IdentityOnlyTransport`` (#169): no runner call,
    no stream, and no answer read with a bound. Nothing waits to find that out at dispatch: a submit
    and a worker's step are refused at admission, so no job is ever queued for a transport that could
    not run it."""
    house = await BuilderEngine.create(a_clearinghouse())
    try:
        transport = house.jobs._transport
        assert isinstance(transport, IdentityOnlyTransport)
        assert not isinstance(transport, BoundedRunnerTransport) and house.jobs.streaming is None

        with pytest.raises(InvalidRequest, match="clearinghouse role"):
            await house.jobs.submit(alice, QUEUED)
        with pytest.raises(InvalidRequest, match="clearinghouse role"):
            await house.jobs.worker.run_once()

        assert await house.jobs.list_jobs(alice) == []
        assert await house.store.queued_stats() == (0, None)
        assert (house.health().queued_jobs, house.health().oldest_queued_s) == (0, None)
    finally:
        await house.stop()
