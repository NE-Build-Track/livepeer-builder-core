"""#113 C19: a job's ``operation_ref`` is the admin commands' ``OperationRef``.

An empty string counted as a reference: every empty-reference job of one actor shared it, so the second
was answered as a repeat of the first. A client sends a real reference or leaves the field out.
"""

from collections.abc import AsyncIterator
from decimal import Decimal

import httpx
import pytest
from pydantic import ValidationError

from livepeer_builder import BuilderEngine
from livepeer_builder.contracts import ActorContext, JobRequest, Money, ProvisionRequest, SessionRequest
from livepeer_builder.http import create_app
from livepeer_builder.testing import ScriptedStreamTransport
from tests.conftest import EngineFactory

APP = "example/echo"


def test_an_empty_job_reference_is_refused_and_named() -> None:
    with pytest.raises(ValidationError, match="operation_ref"):
        JobRequest(app=APP, operation_ref="")


@pytest.mark.parametrize("reference", ["", "r" * 201, "a\x00b"])
def test_a_reserved_session_request_takes_the_same_reference(reference: str) -> None:
    """Sessions are not built yet (501), but the contract is published: it must not repeat C19 later."""
    with pytest.raises(ValidationError, match="operation_ref"):
        SessionRequest(app=APP, operation_ref=reference)
    assert SessionRequest(app=APP).operation_ref is None
    assert SessionRequest(app=APP, operation_ref="open-1").operation_ref == "open-1"


def test_a_job_may_still_leave_its_reference_out() -> None:
    assert JobRequest(app=APP).operation_ref is None
    assert JobRequest(app=APP, operation_ref=None).operation_ref is None
    assert JobRequest(app=APP, operation_ref="order-1").operation_ref == "order-1"


@pytest.fixture
async def api(
    make_engine: EngineFactory, admin: ActorContext
) -> AsyncIterator[tuple[httpx.AsyncClient, BuilderEngine, dict[str, str]]]:
    engine = await make_engine()
    alice = await engine.keys.issue(admin, kind="actor", actor_id="alice", operation_ref="issue-ref-1")
    allocation = {"application_id": "default", "actor_id": "alice", "grant_id": "grant_1"}
    amount = Money(amount=Decimal(5), currency="USD")
    await engine.payments.provision(admin, ProvisionRequest(**allocation, amount=amount, operation_ref="p1"))
    app = create_app(engine, manage_lifecycle=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://engine") as client:
        yield client, engine, {"Authorization": f"Bearer {alice.token}"}


async def test_rest_answers_an_empty_reference_with_a_422_that_names_it(
    api: tuple[httpx.AsyncClient, BuilderEngine, dict[str, str]], transport: ScriptedStreamTransport
) -> None:
    client, engine, alice = api
    transport.steps = [ScriptedStreamTransport.ok(), ScriptedStreamTransport.ok()]
    refused = await client.post("/v1/jobs", json={"app": APP, "operation_ref": ""}, headers=alice)
    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"][0]["loc"] == ["body", "operation_ref"]
    reader = ActorContext(actor_id="alice", permissions=frozenset({"jobs.read"}))
    assert await engine.jobs.list_jobs(reader) == [] and transport.calls == []

    assert (await client.post("/v1/jobs", json={"app": APP}, headers=alice)).status_code == 200
    assert (await client.post("/v1/jobs", json={"app": APP}, headers=alice)).status_code == 200
    assert [job.operation_ref for job in await engine.jobs.list_jobs(reader)] == [None, None]
