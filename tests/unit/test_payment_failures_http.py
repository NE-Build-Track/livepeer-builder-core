"""ISS-12, D58 over REST: each payment reason has its own status, and no body leaks the payment path.

The engine, its REST adapter, the Live Runner transport and the SDK's signer client are real; only the
network is local (tests/unit/livepeer_net.py). The signer is the same loopback server as the runner,
reached as ``localhost`` while the runner is ``127.0.0.1``, so a leaked signer URL is told apart from
the runner URL the attempts rightly carry.
"""

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from decimal import Decimal

import httpx
import pytest

from livepeer_builder import BuilderEngine, EngineSupply
from livepeer_builder.adapters.batteries import BatteriesClient
from livepeer_builder.contracts import ActorContext, Money, ProvisionRequest
from livepeer_builder.http import create_app
from tests.conftest import EngineFactory
from tests.unit.livepeer_net import Net, start
from tests.unit.test_signing_intent import paid_engine

APP = "example/echo"


@dataclass
class Rest:
    client: httpx.AsyncClient
    engine: BuilderEngine
    net: Net
    signer_url: str
    alice: dict[str, str]  # alice's engine key, as a builder sends it

    async def run(self, *, stream: bool = False) -> httpx.Response:
        params = {"stream": "true"} if stream else None
        return await self.client.post("/v1/jobs", json={"app": APP}, params=params, headers=self.alice)


@pytest.fixture
async def net() -> AsyncIterator[Net]:
    async for value in start():
        yield value


@pytest.fixture
async def rest(make_engine: EngineFactory, admin: ActorContext, net: Net) -> AsyncIterator[Rest]:
    signer_url = net.url.replace("127.0.0.1", "localhost")
    engine, _ = await paid_engine(make_engine, admin, net, signer_url=signer_url)
    key = await engine.keys.issue(admin, kind="actor", actor_id="alice", operation_ref="issue-alice")
    app = create_app(engine, manage_lifecycle=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://engine") as client:
        yield Rest(client, engine, net, signer_url, {"Authorization": f"Bearer {key.token}"})


@pytest.mark.parametrize(
    ("info", "sign", "status", "retry_after", "reason"),
    [
        ("ok", 401, 403, None, "not_authorized"),  # retrying cannot help: no Retry-After
        ("ok", 403, 403, None, "not_authorized"),
        ("ok", 402, 402, None, "allowance_exhausted"),  # until an operator funds the allocation
        (502, "ok", 503, "10", "signer_unavailable"),  # nothing was signed: retry later
        ("ok", 502, 503, None, "signer_unknown"),  # it may have charged: a retry could pay twice
    ],
)
@pytest.mark.parametrize("stream", [False, True], ids=["buffered", "streamed"])
async def test_each_payment_reason_has_its_own_status(
    rest: Rest,
    info: int | str,
    sign: int | str,
    status: int,
    retry_after: str | None,
    reason: str,
    stream: bool,
) -> None:
    rest.net.info, rest.net.sign = [info, info], [sign]
    response = await rest.run(stream=stream)
    assert (response.status_code, response.headers.get("retry-after")) == (status, retry_after)
    failure = response.json()["job"]["failure"]
    assert failure["reason"] == reason
    assert response.json()["error"] == failure["message"]


async def test_not_provisioned_stays_402_and_has_no_reason(rest: Rest, admin: ActorContext) -> None:
    """``kind`` tells it apart from ``allowance_exhausted``, which is 402 too."""
    bob = await rest.engine.keys.issue(admin, kind="actor", actor_id="bob", operation_ref="issue-bob")
    headers = {"Authorization": f"Bearer {bob.token}"}
    response = await rest.client.post("/v1/jobs", json={"app": APP}, headers=headers)
    assert (response.status_code, response.headers.get("retry-after")) == (402, None)
    failure = response.json()["job"]["failure"]
    assert (failure["kind"], failure["reason"]) == ("not_provisioned", None)


SIGNER_WORDS = ("UPSTREAM-", "signer auth", "generate-live-payment", "sign-orchestrator-info")


@pytest.mark.parametrize(
    ("info", "sign"),
    [
        ("ok", 401),
        ("ok", 402),
        ("ok", 403),
        ("ok", 481),  # this orchestrator's price: fails over, then every runner refused
        ("ok", 400),
        ("ok", 502),
        ("ok", "drop"),
        ("ok", "malformed"),
        (502, "ok"),
        (404, "ok"),
        ("drop", "ok"),
    ],
)
async def test_no_builder_facing_body_carries_the_payment_path(
    rest: Rest, info: int | str, sign: int | str
) -> None:
    """No payment key, no signer URL, none of the signer's own words: on the job problem, the job, the
    job list and the cost reads, buffered or streamed (D58)."""
    rest.net.info, rest.net.sign = [info] * 4, [sign] * 4
    bodies = [await rest.run(), await rest.run(stream=True)]
    job_id = bodies[0].headers["livepeer-job-id"]
    for path in (f"/v1/jobs/{job_id}", "/v1/jobs", f"/v1/jobs/{job_id}/cost", "/v1/costs/projections"):
        bodies.append(await rest.client.get(path, headers=rest.alice))
    signer_host = rest.signer_url.removeprefix("http://")
    for body in bodies:
        assert body.status_code >= 400 or body.request.method == "GET"
        text = body.text
        assert "lpg_" not in text and signer_host not in text, body.request.url
        assert not any(word in text for word in SIGNER_WORDS), (body.request.url, text)


PROVIDER_WORDS = "UPSTREAM-BATTERIES: allocation row locked by tx 4711 on db.internal"


@pytest.mark.parametrize(("provider", "status"), [(500, 502), (404, 404), (409, 409)])
async def test_an_allowance_the_provider_cannot_read_has_a_fixed_message_and_its_status(
    make_engine: EngineFactory,
    admin: ActorContext,
    caplog: pytest.LogCaptureFixture,
    provider: int,
    status: int,
) -> None:
    """``GET /v1/allowance`` is a builder's read: the provider's own words go to the log only (D58)."""

    def batteries(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":  # provisioning: one allocation and key
            return httpx.Response(201, json={"allocation_id": "alloc_1", "id": "key_1", "api_key": "lpg_k"})
        return httpx.Response(provider, json={"error": PROVIDER_WORDS})

    client = httpx.AsyncClient(base_url="https://batteries.test", transport=httpx.MockTransport(batteries))
    manager = BatteriesClient("https://batteries.test", "t", client=client)
    engine = await make_engine(supply=EngineSupply(allocation_manager=manager))
    amount = Money(amount=Decimal(5), currency="USD")
    request = ProvisionRequest(
        operation_ref="p", application_id="default", actor_id="alice", amount=amount, grant_id="g"
    )
    await engine.payments.provision(admin, request)
    key = await engine.keys.issue(admin, kind="actor", actor_id="alice", operation_ref="issue-alice")
    app = create_app(engine, manage_lifecycle=False)
    with caplog.at_level(logging.WARNING, logger="livepeer_builder.services.payments"):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://engine"
        ) as rest:
            response = await rest.get("/v1/allowance", headers={"Authorization": f"Bearer {key.token}"})
    assert response.status_code == status
    assert response.json() == {"error": f"provider HTTP {provider}: the allowance could not be read"}
    [record] = [item for item in caplog.records if item.message == "allowance read failed"]
    assert PROVIDER_WORDS in record.detail and record.provider_status == provider


async def test_an_allowance_read_with_batteries_unreachable_says_so_in_the_engines_words(
    make_engine: EngineFactory, admin: ActorContext, caplog: pytest.LogCaptureFixture
) -> None:
    """D58: httpx's own text (which can name the provider's host) goes to the log; the builder gets 503."""
    provisioned = False

    def batteries(request: httpx.Request) -> httpx.Response:
        nonlocal provisioned
        if not provisioned:  # provisioning: one allocation and key
            provisioned = True
            return httpx.Response(201, json={"allocation_id": "alloc_1", "id": "key_1", "api_key": "lpg_k"})
        raise httpx.ConnectError("UPSTREAM-HTTPX: connect to batteries.internal:8937 refused")

    client = httpx.AsyncClient(base_url="https://batteries.test", transport=httpx.MockTransport(batteries))
    manager = BatteriesClient("https://batteries.test", "t", client=client)
    engine = await make_engine(supply=EngineSupply(allocation_manager=manager))
    amount = Money(amount=Decimal(5), currency="USD")
    request = ProvisionRequest(
        operation_ref="p", application_id="default", actor_id="alice", amount=amount, grant_id="g"
    )
    await engine.payments.provision(admin, request)
    key = await engine.keys.issue(admin, kind="actor", actor_id="alice", operation_ref="issue-alice")
    app = create_app(engine, manage_lifecycle=False)
    with caplog.at_level(logging.WARNING, logger="livepeer_builder.services.payments"):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://engine"
        ) as rest:
            response = await rest.get("/v1/allowance", headers={"Authorization": f"Bearer {key.token}"})
    assert response.status_code == 503
    assert response.json() == {"error": "the allowance could not be read: the provider is unavailable"}
    [record] = [item for item in caplog.records if item.message == "allowance read failed"]
    assert "UPSTREAM-HTTPX" in record.detail
