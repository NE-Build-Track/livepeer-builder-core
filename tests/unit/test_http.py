"""Service mode: the REST adapter authenticates, delegates, and maps errors (ISS-05 skeleton)."""

from collections.abc import AsyncIterator

import httpx
import pytest

from livepeer_builder.http import create_app
from livepeer_builder.runtime import BuilderEngine
from livepeer_builder.testing import ScriptedTransport
from tests.conftest import EngineFactory


@pytest.fixture
async def api(make_engine: EngineFactory) -> AsyncIterator[tuple[httpx.AsyncClient, BuilderEngine]]:
    engine = await make_engine()
    app = create_app(engine, manage_lifecycle=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://engine") as client:
        yield client, engine


async def tokens(engine: BuilderEngine) -> tuple[str, str]:
    admin = await engine.keys.bootstrap_admin()
    from livepeer_builder.contracts import ActorContext

    operator = ActorContext(actor_id="operator", credential_kind="admin")
    alice = await engine.keys.issue(operator, kind="actor", actor_id="alice", operation_ref="issue-http-27")
    return admin.token, alice.token


async def test_unauthenticated_requests_get_a_challenge(api: tuple[httpx.AsyncClient, BuilderEngine]) -> None:
    client, _ = api
    response = await client.get("/v1/runners", params={"app": "example/echo"})
    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Bearer")
    assert (await client.get("/livez")).status_code == 200


async def test_offerings_are_public_and_name_no_runner(api: tuple[httpx.AsyncClient, BuilderEngine]) -> None:
    """#5: the catalog needs no key, and gives an anonymous caller nothing to reach a runner with."""
    client, _ = api
    response = await client.get("/v1/offerings")
    assert response.status_code == 200
    offerings = response.json()
    assert [offering["app"] for offering in offerings] == ["example/echo"]
    # The fixture runner is https://runner-1.example behind https://orch.example. Neither host
    # contains the substring "url", so a copied address has to be rejected by name.
    assert "https://runner-1.example" not in response.text
    assert "https://orch.example" not in response.text
    for offering in offerings:
        assert set(offering) == {
            "app",
            "modes",
            "rate_low",
            "rate_high",
            "rate_status",
            "stale",
            "rates_excluded",
            "runner_count",
            "ready_count",
            "observed_at",
        }
    assert (await client.get("/v1/offerings", params={"app": "other/app"})).json() == []


async def test_offered_rates_carry_their_source_and_age(api: tuple[httpx.AsyncClient, BuilderEngine]) -> None:
    """ISS-10: a caller can inspect units, currency, source and age before invoking anything."""
    client, _ = api
    (offering,) = (await client.get("/v1/offerings")).json()
    assert set(offering["rate_low"]) == {
        "amount", "currency", "unit", "price_usd", "source", "observed_at", "observed_by",
    }  # fmt: skip
    assert (offering["rate_low"]["source"], offering["rate_low"]["observed_by"]) == ("orchestrator", "engine")
    assert (offering["rate_status"], offering["stale"], offering["rates_excluded"]) == ("available", False, 0)
    assert offering["rate_low"]["observed_at"] == offering["observed_at"]  # the fixture runner has no stamp


async def test_job_round_trip_and_admin_boundary(
    api: tuple[httpx.AsyncClient, BuilderEngine], transport: ScriptedTransport
) -> None:
    client, engine = api
    admin_token, alice_token = await tokens(engine)
    as_admin = {"Authorization": f"Bearer {admin_token}"}
    as_alice = {"Authorization": f"Bearer {alice_token}"}

    forbidden = await client.post(
        "/v1/admin/applications/default/actors/alice/allocation",
        json={"amount": {"amount": "5", "currency": "USD"}, "operation_ref": "p0"},
        headers=as_alice,
    )
    assert forbidden.status_code == 403
    provisioned = await client.post(
        "/v1/admin/applications/default/actors/alice/allocation",
        json={"amount": {"amount": "5", "currency": "USD"}, "grant_id": "grant_1", "operation_ref": "p1"},
        headers=as_admin,
    )
    assert provisioned.status_code == 201
    assert "lpg_" not in provisioned.text  # the payment key never appears in a response

    offerings = await client.get("/v1/offerings", headers=as_alice)
    assert [o["app"] for o in offerings.json()] == ["example/echo"]

    transport.steps = [ScriptedTransport.ok(b'{"echo": "hi"}')]
    ran = await client.post("/v1/jobs", json={"app": "example/echo", "path": "/echo"}, headers=as_alice)
    assert ran.status_code == 200 and ran.json() == {"echo": "hi"}
    job_id = ran.headers["livepeer-job-id"]
    assert (await client.get(f"/v1/jobs/{job_id}", headers=as_alice)).json()["state"] == "succeeded"
    assert (await client.get(f"/v1/jobs/{job_id}/cost", headers=as_alice)).json()["status"] == "pending"


async def test_failures_and_reserved_routes(api: tuple[httpx.AsyncClient, BuilderEngine]) -> None:
    client, engine = api
    _, alice_token = await tokens(engine)
    as_alice = {"Authorization": f"Bearer {alice_token}"}

    unprovisioned = await client.post("/v1/jobs", json={"app": "example/echo"}, headers=as_alice)
    assert unprovisioned.status_code == 402
    assert unprovisioned.json()["job"]["failure"]["kind"] == "not_provisioned"

    reserved = await client.post("/v1/jobs", json={"app": "example/echo", "mode": "async"}, headers=as_alice)
    assert reserved.status_code == 501 and reserved.json()["milestone"] == "M4"
    assert (await client.get("/v1/events", headers=as_alice)).status_code == 501
    assert (
        await client.post("/v1/sessions", json={"app": "example/echo"}, headers=as_alice)
    ).status_code == 501


async def test_openapi_declares_the_bearer_scheme(api: tuple[httpx.AsyncClient, BuilderEngine]) -> None:
    client, _ = api
    spec = (await client.get("/openapi.json")).json()
    schemes = spec["components"].get("securitySchemes", {})
    bearer = [name for name, scheme in schemes.items() if scheme.get("scheme") == "bearer"]
    assert bearer, "the spec declares a bearer scheme, so Swagger UI shows Authorize"
    assert spec["paths"]["/v1/runners"]["get"]["security"] == [{bearer[0]: []}]
    assert "security" not in spec["paths"]["/readyz"]["get"]  # health stays unauthenticated
    assert "security" not in spec["paths"]["/v1/offerings"]["get"]  # the catalog is public (#5)
