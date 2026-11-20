"""An engine in each role, for the #169 tests (D84): the settings a role needs beside the test's own.

``both`` is the default and needs nothing. ``work`` names a payment clearinghouse; ``clearinghouse`` has
a private listener, and its runner transport forwards signer calls, as the packaged one with a signer
does. The ports are the shared fakes of ``tests/conftest.py``, so no address here is ever called.
"""

from typing import Any, Literal

from fastapi import FastAPI
from fastapi.routing import APIRoute

from livepeer_builder import BuilderEngine, EngineSupply
from livepeer_builder.testing import FakeSigningTransport
from tests.conftest import EngineFactory

Role = Literal["both", "work", "clearinghouse"]
ROLES: tuple[Role, ...] = ("both", "work", "clearinghouse")
PRIVATE_PORT = 18081  # never bound here: the apps are called in process
WORK: dict[str, Any] = {
    "role": "work",
    "clearinghouse_url": "http://clearinghouse.test",
    "clearinghouse_token": "lpk_tenant-admin-key-not-to-be-shown",
    "clearinghouse_tenant_id": "default",
}
CLEARINGHOUSE: dict[str, Any] = {"role": "clearinghouse", "http_private_port": PRIVATE_PORT}
SETTINGS: dict[Role, dict[str, Any]] = {"both": {}, "work": WORK, "clearinghouse": CLEARINGHOUSE}


async def in_role(make_engine: EngineFactory, role: Role, **settings: Any) -> BuilderEngine:
    """An engine in ``role`` over the test's shared fakes, with a signer behind its transport, so a
    role that serves the signer's two paths has them."""
    signer = EngineSupply(transport=FakeSigningTransport())
    return await make_engine(supply=signer, **SETTINGS[role], **settings)


def served(app: FastAPI) -> dict[tuple[str, str], APIRoute]:
    """Every (method, path) the app's own routes answer, with its route."""
    return {
        (method, route.path): route
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in route.methods
    }
