"""ISS-15 (D59): job history pages over REST, and what the docs promise about inputs and results.

Results are returned inline once and not kept; the job keeps a ``ResultRef`` (size and sha256) so a
builder can check a copy it saved, and ``GET /v1/jobs/{id}/result`` answers 501 until M4 (ISS-26).
"""

import hashlib
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from livepeer_builder import BuilderEngine
from livepeer_builder.contracts import ActorContext
from livepeer_builder.http import create_app
from livepeer_builder.testing import FixedClock, ScriptedTransport
from tests.conftest import EngineFactory
from tests.unit.livepeer_net import Net, Reply, start
from tests.unit.test_signing_intent import paid_engine

APP = "example/echo"


@dataclass
class History:
    client: httpx.AsyncClient
    engine: BuilderEngine
    clock: FixedClock
    transport: ScriptedTransport
    admin: dict[str, str]
    alice: dict[str, str]
    bob: dict[str, str]

    async def run(self, who: dict[str, str]) -> str:
        """One job, a second after the last: its id."""
        self.clock.advance(1)
        self.transport.steps = [ScriptedTransport.ok()]
        response = await self.client.post("/v1/jobs", json={"app": APP}, headers=who)
        assert response.status_code == 200, response.text
        return response.headers["livepeer-job-id"]

    async def page(self, who: dict[str, str], **params: str | int) -> list[str]:
        response = await self.client.get("/v1/jobs", params=params, headers=who)
        assert response.status_code == 200, response.text
        return [job["id"] for job in response.json()]


@pytest.fixture
async def history(
    make_engine: EngineFactory, clock: FixedClock, transport: ScriptedTransport
) -> AsyncIterator[History]:
    engine = await make_engine(allocation_mode="per_application", shared_payment_key="lpg_shared")
    operator = ActorContext(actor_id="operator", credential_kind="admin")
    admin = await engine.keys.bootstrap_admin()
    alice = await engine.keys.issue(operator, kind="actor", actor_id="alice", operation_ref="issue-alice")
    bob = await engine.keys.issue(operator, kind="actor", actor_id="bob", operation_ref="issue-bob")
    app = create_app(engine, manage_lifecycle=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://engine") as client:
        yield History(
            client,
            engine,
            clock,
            transport,
            *({"Authorization": f"Bearer {key.token}"} for key in (admin, alice, bob)),
        )


async def test_before_pages_the_history_newest_first_and_only_the_callers_own(history: History) -> None:
    first = await history.run(history.alice)
    bobs = await history.run(history.bob)
    second = await history.run(history.alice)
    third = await history.run(history.alice)
    assert await history.page(history.alice, limit=2) == [third, second]
    cursor = (await history.client.get(f"/v1/jobs/{second}", headers=history.alice)).json()["created_at"]
    assert await history.page(history.alice, limit=2, before=cursor) == [first]  # bob's is not alice's
    assert await history.page(history.admin, before=cursor) == [bobs, first]  # the application's


async def test_before_in_another_zone_is_the_same_instant_in_utc(history: History) -> None:
    """``created_at`` is stored in UTC; a cursor written in another zone pages from the same instant."""
    first = await history.run(history.alice)
    second = await history.run(history.alice)
    cursor = (await history.client.get(f"/v1/jobs/{second}", headers=history.alice)).json()["created_at"]
    shifted = datetime.fromisoformat(cursor).astimezone(timezone(timedelta(hours=2))).isoformat()
    assert shifted.endswith("+02:00")
    assert await history.page(history.alice, before=shifted) == [first]


async def test_paging_does_not_depend_on_the_servers_local_zone(
    history: History, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing reads the process's local zone: the same cursor pages the same under TZ=Pacific/Chatham."""
    monkeypatch.setenv("TZ", "Pacific/Chatham")  # +12:45 or +13:45, far from UTC
    time.tzset()
    try:
        first = await history.run(history.alice)
        second = await history.run(history.alice)
        cursor = (await history.client.get(f"/v1/jobs/{second}", headers=history.alice)).json()["created_at"]
        assert await history.page(history.alice, before=cursor) == [first]
    finally:
        monkeypatch.undo()
        time.tzset()


@pytest.mark.parametrize("before", ["2026-10-12T12:00:00", "yesterday"])
async def test_before_needs_a_time_with_its_offset(history: History, before: str) -> None:
    """A time without an offset could mean any zone, so it is refused, not guessed (422)."""
    response = await history.client.get("/v1/jobs", params={"before": before}, headers=history.alice)
    assert response.status_code == 422


async def test_the_result_route_is_404_for_another_actors_job_then_501_naming_m4_and_iss_26(
    history: History,
) -> None:
    job_id = await history.run(history.alice)
    assert (await history.client.get(f"/v1/jobs/{job_id}/result", headers=history.bob)).status_code == 404
    reserved = await history.client.get(f"/v1/jobs/{job_id}/result", headers=history.alice)
    assert reserved.status_code == 501
    body = reserved.json()
    assert (body["milestone"], body["ticket"]) == ("M4", "ISS-26")
    assert "M4 (ISS-26)" in body["error"]


# -- inputs and results through the real runner call (tests/unit/livepeer_net.py) ----------------------

# A FLUX-schnell-like answer: the image is a URL the model provider hosts, not bytes the engine keeps.
IMAGE = b'{"images":[{"url":"https://v3.fal.media/files/x/abc.png","content_type":"image/png"}]}'


@dataclass
class Loopback:
    client: httpx.AsyncClient
    net: Net
    alice: dict[str, str]


@pytest.fixture
async def loopback(make_engine: EngineFactory, admin: ActorContext) -> AsyncIterator[Loopback]:
    async for net in start():
        engine, _ = await paid_engine(make_engine, admin, net)
        key = await engine.keys.issue(admin, kind="actor", actor_id="alice", operation_ref="issue-alice")
        app = create_app(engine, manage_lifecycle=False)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://engine") as client:
            yield Loopback(client, net, {"Authorization": f"Bearer {key.token}"})


@pytest.mark.parametrize("stream", [False, True], ids=["buffered", "streamed"])
async def test_the_result_ref_is_the_size_and_sha256_of_the_bytes_returned(
    loopback: Loopback, stream: bool
) -> None:
    """The claim the docs make: a builder can check a copy it saved against the job's ``ResultRef``."""
    loopback.net.replies = [Reply(chunks=(IMAGE[:20], IMAGE[20:])) if stream else Reply(body=IMAGE)]
    params = {"stream": "true"} if stream else None
    ran = await loopback.client.post("/v1/jobs", json={"app": APP}, params=params, headers=loopback.alice)
    assert ran.status_code == 200 and ran.content == IMAGE
    job_id = ran.headers["livepeer-job-id"]
    result = (await loopback.client.get(f"/v1/jobs/{job_id}", headers=loopback.alice)).json()["result"]
    assert result["storage"] == "inline" and result["location"] is None  # not kept: nothing to fetch
    assert result["size_bytes"] == len(ran.content)
    assert result["sha256"] == hashlib.sha256(ran.content).hexdigest()
    assert result["content_type"] == "application/json"


async def test_a_url_in_the_payload_reaches_the_runner_unchanged(loopback: Loopback) -> None:
    """The engine hosts and fetches no media: a URL input is the runner's to fetch."""
    payload = {
        "image_url": "https://media.example/in/cat%20photo.png?X-Amz-Signature=a%2Bb%3D&expires=1",
        "mask": {"url": "s3://bucket/mask.png"},
    }
    ran = await loopback.client.post(
        "/v1/jobs", json={"app": APP, "payload": payload}, headers=loopback.alice
    )
    assert ran.status_code == 200
    assert loopback.net.bodies == [payload, payload]  # the unpaid request, then the paid retry
    assert not any("media.example" in line for line in loopback.net.log)  # nobody fetched it
