"""#168: the discovery reader carries the tenant admin key to the clearinghouse's runner list, and to no
other address (D87).

The clearinghouse's runner list is the default discovery address of an engine that uses one, and the
route needs a credential of the tenant. An enterprise may read a discovery source of its own instead:
that source is sent no key. No redirect is followed, so the key cannot be carried anywhere else.

The runner list states no tenant, so a reader that carries the key asks whose key it is first (#165): the
engine reads the key's grant, and reads no list with a key of another tenant than its own.
"""

import contextlib
from collections.abc import AsyncIterator

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from livepeer_builder.adapters.sdk import HttpDiscoverySource
from livepeer_builder.errors import ProviderError
from tests.unit.clearinghouse_engine import TOKEN, parts_of, self_hosted, with_clearinghouse
from tests.unit.discovery_wire import document, record

# -- the runner list's credential (D87) -------------------------------------------------------------------


def says_whose_key_it_is(app: web.Application, tenant_id: str = "grant_7") -> None:
    """Add the read of the key's grant to a clearinghouse's stand-in: the key is ``tenant_id``'s. The
    engine of ``with_clearinghouse`` is tenant ``grant_7``."""
    money = {"amount": "1", "currency": "USD"}
    grant = {"tenant_id": tenant_id, "name": "t", "status": "active", "total": money}
    grant |= {"unallocated": money, "spent": money, "allocated_unspent": money}

    async def serve(request: web.Request) -> web.Response:
        del request
        return web.json_response(grant)

    app.router.add_get("/v1/clearinghouse/grant", serve)


@contextlib.asynccontextmanager
async def discovery_server(path: str, key_of: str = "grant_7") -> AsyncIterator[tuple[str, list[str | None]]]:
    """A server that serves a runner list at ``path`` and keeps the ``Authorization`` of each request for
    it. It says the key is tenant ``key_of``'s."""
    seen: list[str | None] = []

    async def serve(request: web.Request) -> web.Response:
        seen.append(request.headers.get("Authorization"))
        return web.Response(body=document(record()), content_type="application/json")

    app = web.Application()
    app.router.add_get(path, serve)
    says_whose_key_it_is(app, key_of)
    async with TestServer(app) as server:
        yield str(server.make_url("")).rstrip("/"), seen


async def test_the_reader_carries_the_tenant_admin_key_to_the_clearinghouse_runner_list() -> None:
    async with discovery_server("/v1/clearinghouse/discovery") as (url, seen):
        async with parts_of(with_clearinghouse(url)) as parts:
            assert isinstance(parts.discovery_source, HttpDiscoverySource)
            assert len(await parts.discovery_source.fetch()) == 1
            assert TOKEN not in repr(parts.discovery_source) + str(vars(parts.discovery_source))
        assert seen == [f"Bearer {TOKEN}"]


async def test_the_reader_of_an_engine_reads_no_runner_list_with_another_tenants_key() -> None:
    """The reader an engine is built with, from its settings alone. The clearinghouse says the key is
    another tenant's than ``LPB_CLEARINGHOUSE_TENANT_ID``: every refresh is refused, with the words of the
    refusal the adapters give, and the runner list is never asked for. A source of the enterprise's own
    is sent no key and asks nothing."""
    async with discovery_server("/v1/clearinghouse/discovery", key_of="grant_8") as (url, seen):
        async with parts_of(with_clearinghouse(url)) as parts:
            for _ in range(2):
                with pytest.raises(ProviderError) as refused:
                    await parts.discovery_source.fetch()
                assert refused.value.status_code == 403
                assert "belongs to another tenant than LPB_CLEARINGHOUSE_TENANT_ID" in refused.value.message
                assert TOKEN not in f"{refused.value} {refused.value!r}"
            assert isinstance(parts.discovery_source, HttpDiscoverySource)
            assert parts.discovery_source.document() is None
        assert seen == []


async def test_the_reader_carries_no_key_to_any_other_discovery_address() -> None:
    async with discovery_server("/discovery") as (own, seen):
        async with parts_of(with_clearinghouse(discovery_url=own + "/discovery")) as parts:
            await parts.discovery_source.fetch()
        async with parts_of(self_hosted(discovery_url=own + "/discovery")) as parts:
            await parts.discovery_source.fetch()
        assert seen == [None, None]


async def test_a_redirect_from_the_runner_list_is_not_followed_with_the_key() -> None:
    async with discovery_server("/discovery") as (elsewhere, seen):

        async def redirect(request: web.Request) -> web.Response:
            raise web.HTTPTemporaryRedirect(elsewhere + "/discovery")

        app = web.Application()
        app.router.add_get("/v1/clearinghouse/discovery", redirect)
        says_whose_key_it_is(app)
        async with TestServer(app) as server:
            url = str(server.make_url("")).rstrip("/")
            async with parts_of(with_clearinghouse(url)) as parts:
                with pytest.raises(Exception, match="307"):
                    await parts.discovery_source.fetch()
        assert seen == []
