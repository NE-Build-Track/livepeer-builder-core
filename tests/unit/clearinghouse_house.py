"""A clearinghouse for the #166 tests: one engine behind its REST app, its admin, and two onboarded
tenants, each with its own tenant admin key. The parts are ``tests/unit/test_tenants.py``'s."""

import contextlib
from collections.abc import AsyncIterator
from typing import Any

import httpx

from livepeer_builder.http import create_app
from tests.conftest import EngineFactory
from tests.unit.test_tenants import House, onboard


@contextlib.asynccontextmanager
async def open_house(make_engine: EngineFactory, **parts: Any) -> AsyncIterator[House]:
    """``parts`` go to the engine factory: ``supply=EngineSupply(...)`` replaces one of the shared fakes."""
    engine = await make_engine(**parts)
    assert engine.keys is not None
    admin = {"Authorization": f"Bearer {(await engine.keys.bootstrap_admin()).token}"}
    app = create_app(engine, manage_lifecycle=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://engine") as client:
        one, two = await onboard(client, admin, "one"), await onboard(client, admin, "two")
        yield House(client, engine, admin, one, two)
