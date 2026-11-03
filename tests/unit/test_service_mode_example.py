"""Service mode, customized: the stock server with an enterprise authenticator, a decorated default
and one extra endpoint (docs/design-docs/extending-the-engine.md#service-mode).

The region between the docs markers is the guide's example; scripts/check_docs.py keeps them equal.
"""

import dataclasses
from typing import Annotated

import httpx
import pytest
from fastapi import APIRouter, Depends, FastAPI

from livepeer_builder import ActorContext, EngineApi, EngineParts, EngineSettings, EngineSupply
from livepeer_builder.adapters.static import StaticDiscoverySource
from livepeer_builder.http.server import actor_dependency, build_app, serve
from livepeer_builder.store import SqlStore
from livepeer_builder.testing import runner
from tests.unit.test_extension_examples import (
    DIRECTORY,
    SECRET,
    ApprovedOrchestratorsOnly,
    SignedSsoHeaderAuthenticator,
    sign,
)


# docs:start service-mode
def corp_supply() -> EngineSupply:
    return EngineSupply(authenticators=(SignedSsoHeaderAuthenticator(SECRET),))


def corp_decorations(parts: EngineParts) -> EngineParts:
    approved = ApprovedOrchestratorsOnly(parts.discovery_source, approved={"https://orch.example"})
    return dataclasses.replace(parts, discovery_source=approved)


def corp_routes(app: FastAPI, engine: EngineApi) -> None:
    """An extra endpoint: it gets the engine and the same authentication as the built-in routes."""
    router = APIRouter()
    Actor = Annotated[ActorContext, Depends(actor_dependency(engine))]

    @router.get("/v1/corp/whoami")
    async def whoami(actor: Actor) -> dict[str, str]:
        return {"actor_id": actor.actor_id, "application_id": actor.application_id}

    app.include_router(router)


# docs:end service-mode


def settings(**values: object) -> EngineSettings:
    return EngineSettings.model_validate(
        {"database_url": "sqlite+aiosqlite:///:memory:", "log_json": False, **values}
    )


def sso_headers(user: str = "alice", tenant: str = "acme") -> dict[str, str]:
    team = DIRECTORY[user]
    return {
        "x-sso-user": user,
        "x-sso-tenant": tenant,
        "x-sso-team": team,
        "x-sso-signature": sign(SECRET, user, tenant, team),
    }


async def test_the_customized_service_authenticates_and_serves_the_extra_route(store: SqlStore) -> None:
    supply = dataclasses.replace(
        corp_supply(), store=store, discovery_source=StaticDiscoverySource([runner()])
    )
    app = await build_app(settings(), supply=supply, decorate=corp_decorations, extend=corp_routes)
    async with (
        app.router.lifespan_context(app),  # starts and stops the engine, as an ASGI server would
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://engine") as client,
    ):
        whoami = await client.get("/v1/corp/whoami", headers=sso_headers())
        assert whoami.status_code == 200 and whoami.json()["actor_id"] == "alice"
        assert (await client.get("/v1/corp/whoami")).status_code == 401
        assert "/v1/corp/whoami" in (await client.get("/openapi.json")).json()["paths"]


async def test_serve_stops_the_engine_when_the_app_cannot_be_built(store: SqlStore) -> None:
    built: list[EngineApi] = []

    def broken(app: FastAPI, engine: EngineApi) -> None:
        built.append(engine)
        raise ValueError("a bug in an extension")

    with pytest.raises(ValueError, match="extension"):
        await serve(settings(), supply=EngineSupply(store=store), extend=broken)
    with pytest.raises(RuntimeError, match="engine is stopped"):  # stopped before the error reached us
        await built[0].start()
