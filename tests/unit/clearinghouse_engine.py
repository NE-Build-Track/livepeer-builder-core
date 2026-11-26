"""An engine's settings and default parts for the #168 assembly tests: self-hosted payments, or an engine
in its ``work`` role (#169) with the payment clearinghouse its three settings name."""

import contextlib
from collections.abc import AsyncIterator

from livepeer_builder import EngineSettings, EngineSupply
from livepeer_builder.config import generate_secret_key
from livepeer_builder.runtime.assembly import EngineParts, default_parts

MEMORY = "sqlite+aiosqlite:///:memory:"
TOKEN = "lpk_tenant-admin-key-not-to-be-shown"


def settings(**values: object) -> EngineSettings:
    base: dict[str, object] = {"database_url": MEMORY, "log_json": False, "secret_key": generate_secret_key()}
    return EngineSettings.model_validate({**base, **values})


def self_hosted(**values: object) -> EngineSettings:
    return settings(**{"discovery_url": "http://127.0.0.1:9/discovery", **values})


def with_clearinghouse(url: str = "http://127.0.0.1:9", **values: object) -> EngineSettings:
    return settings(
        role="work",
        clearinghouse_url=url,
        clearinghouse_token=TOKEN,
        clearinghouse_tenant_id="grant_7",
        **values,
    )


@contextlib.asynccontextmanager
async def parts_of(given: EngineSettings, supply: EngineSupply | None = None) -> AsyncIterator[EngineParts]:
    async with contextlib.AsyncExitStack() as stack:
        yield await default_parts(given, stack, supply)
