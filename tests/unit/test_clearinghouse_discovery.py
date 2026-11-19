"""#166: ``GET /v1/clearinghouse/discovery`` serves the clearinghouse's runner list in livepeer/node's
discovery format (D87).

An enterprise's engine reads the clearinghouse's list by default: its ``LPB_DISCOVERY_URL`` is this
route. So the route serves the document the clearinghouse's own engine last read from its discovery
source, byte for byte: nothing is parsed into another shape and written again, and the reader behind
``LPB_DISCOVERY_URL`` reads it as it reads livepeer/node's ``livepeer-discovery``. Nothing is enforced at
signing in this version.
"""

import dataclasses
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

from livepeer_builder import BuilderEngine, EngineParts
from livepeer_builder.adapters.discovery_inventory import read_inventory
from livepeer_builder.adapters.sdk import HttpDiscoverySource
from livepeer_builder.contracts import ActorContext, Runner
from livepeer_builder.http import create_app
from livepeer_builder.testing import FAKE_DISCOVERY_URL, make_engine
from tests.unit.discovery_wire import ORCHESTRATOR, document, record

ROUTE = "/v1/clearinghouse/discovery"
CLEARINGHOUSE_URL = "http://clearinghouse.test" + ROUTE  # what an enterprise sets as LPB_DISCOVERY_URL
NODE_DUMP = Path(__file__).with_name("node_discovery_eee09c4.json").read_bytes()  # the pinned node's own
OPERATOR = ActorContext(actor_id="operator", credential_kind="clearinghouse_admin")


class Upstream:
    """The clearinghouse's own discovery source (livepeer/node's ``livepeer-discovery``), as a test sets
    it: a document, an error status, or no answer."""

    def __init__(self, answer: bytes | int | Exception) -> None:
        self.answer = answer

    def handle(self, request: httpx.Request) -> httpx.Response:
        assert "authorization" not in request.headers  # the upstream takes no credential (D63)
        if isinstance(self.answer, Exception):
            raise self.answer
        if isinstance(self.answer, int):
            return httpx.Response(self.answer, json={"error": "upstream"})
        return httpx.Response(200, content=self.answer)


@dataclass
class Clearinghouse:
    engine: BuilderEngine
    client: httpx.AsyncClient
    upstream: Upstream
    keys: dict[str, dict[str, str]]  # a bearer header for each kind of credential

    def reader(self, headers: dict[str, str] | None = None) -> HttpDiscoverySource:
        """Today's discovery reader, as an enterprise's engine runs it, pointed at the route. The one
        thing added is the credential, as a header on the HTTP client it is given."""
        transport = httpx.ASGITransport(app=create_app(self.engine, manage_lifecycle=False))
        return HttpDiscoverySource(
            CLEARINGHOUSE_URL, client=httpx.AsyncClient(transport=transport, headers=headers or {})
        )


@asynccontextmanager
async def clearinghouse(answer: bytes | int | Exception, **parts: Any) -> AsyncIterator[Clearinghouse]:
    upstream = Upstream(answer)
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream.handle)) as wire:

        def from_upstream(given: EngineParts) -> EngineParts:
            source = HttpDiscoverySource(FAKE_DISCOVERY_URL, client=wire)
            return dataclasses.replace(given, discovery_source=source)

        async with make_engine(decorate=parts.pop("decorate", from_upstream), **parts) as engine:
            assert engine.keys is not None
            issued = {
                "clearinghouse_admin": (await engine.keys.bootstrap_admin()).token,
                **{
                    kind: (
                        await engine.keys.issue(
                            OPERATOR, kind=kind, actor_id=f"a-{kind}", tenant_id="t1", operation_ref=kind
                        )
                    ).token
                    for kind in ("tenant_admin", "auditor", "actor")
                },
            }
            keys = {kind: {"Authorization": f"Bearer {token}"} for kind, token in issued.items()}
            app = create_app(engine, manage_lifecycle=False)
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://clearinghouse.test") as client:
                yield Clearinghouse(engine, client, upstream, keys)


@pytest.fixture
async def house() -> AsyncIterator[Clearinghouse]:
    async with clearinghouse(NODE_DUMP) as opened:
        yield opened


# -- what is served -----------------------------------------------------------------------------------------


async def test_the_route_serves_the_document_the_clearinghouse_last_read_byte_for_byte(
    house: Clearinghouse,
) -> None:
    answer = await house.client.get(ROUTE, headers=house.keys["tenant_admin"])
    assert answer.status_code == 200
    assert answer.headers["content-type"] == "application/json"
    assert answer.content == NODE_DUMP  # livepeer/node's own document, as its livepeer-discovery served it


async def test_an_engine_pointed_at_the_route_reads_it_with_todays_reader(house: Clearinghouse) -> None:
    """The proof that the format is node's: the reader an engine runs today, given the route as its
    address and a tenant admin key as a header, reads every runner the clearinghouse's own engine holds,
    with each exact price."""
    reader = house.reader(house.keys["tenant_admin"])
    runners = await reader.fetch()
    assert runners == house.engine.discovery.snapshot.runners == read_inventory(NODE_DUMP)
    assert len(runners) >= 5 and all(isinstance(runner, Runner) for runner in runners)
    priced = [runner.price_info.price for runner in runners if runner.price_info is not None]
    assert priced and all(price is not None for price in priced)


async def test_the_reader_needs_a_credential_and_fails_its_refresh_without_one(house: Clearinghouse) -> None:
    """The reader sends no credential by default (D63). Pointed at the route as it is, its refresh fails,
    and the enterprise's engine keeps its last good snapshot, as after any failed refresh."""
    with pytest.raises(httpx.HTTPStatusError) as refused:
        await house.reader().fetch()
    assert refused.value.response.status_code == 401


async def test_nothing_of_the_document_is_rewritten() -> None:
    """A price with more digits than a float holds, unusual spacing, an entry of metadata, a field the
    engine does not read and a record its reader skips: all served as they arrived."""
    odd = (
        b'[ {"nodes":[{"url":"https://o.example","queried_at":"2026-11-18T11:44:59Z"}]},\n'
        b'  {"address":"https://orch.example","future_field":{"a":[1,2.50]},"runners":[\n'
        b'    {"url":"https://orch.example/apps/a","app":"a/b","observed_at":"2026-10-22T17:05:00Z",\n'
        b'     "price_info":{"price":0.1234567890123456789012345,"price_usd":1e-7,"currency":"wei",'
        b'"unit":"seconds"}},\n'
        b'    {"url":"https://orch.example/apps/skipped","app":"no/stamp"} ] } ]\n'
    )
    async with clearinghouse(odd) as house:
        answer = await house.client.get(ROUTE, headers=house.keys["tenant_admin"])
        assert answer.content == odd
        [runner] = await house.reader(house.keys["tenant_admin"]).fetch()
        assert runner.price_info is not None
        assert str(runner.price_info.price) == "0.1234567890123456789012345"


# -- who may call -------------------------------------------------------------------------------------------


async def test_any_credential_of_a_tenant_and_a_clearinghouse_admin_may_read_the_list(
    house: Clearinghouse,
) -> None:
    """A tenant's credentials at a clearinghouse are its admin's and its auditor's keys: those are what
    the tenant routes issue. An actor key, which an enterprise's own engine issues to its users, is not
    one, and neither is a payment key, which asks for payments and nothing else."""
    for kind in ("tenant_admin", "auditor", "clearinghouse_admin"):
        assert (await house.client.get(ROUTE, headers=house.keys[kind])).status_code == 200, kind
    assert (await house.client.get(ROUTE, headers=house.keys["actor"])).status_code == 403
    assert (await house.client.get(ROUTE)).status_code == 401
    payment_key = {"Authorization": "Bearer lpp_" + "x" * 43}
    assert (await house.client.get(ROUTE, headers=payment_key)).status_code == 401
    for refused in ({}, house.keys["actor"], payment_key):
        assert NODE_DUMP[:40] not in (await house.client.get(ROUTE, headers=refused)).content


# -- when the clearinghouse's own source is down ------------------------------------------------------------


GOOD = document(record())
NEWER = document(record(app="newer/app"))


async def test_a_source_that_is_down_leaves_the_last_good_document_served(house: Clearinghouse) -> None:
    """As the engine keeps its last good snapshot when a refresh fails (docs/contracts/discovery.md), the
    route keeps serving the document that snapshot was read from: what the clearinghouse itself would
    run a job on. ``observed_at`` on each runner still says how old it is."""
    key = house.keys["tenant_admin"]
    for failure in (httpx.ConnectError("refused"), 503, b"not json", document({"url": "x"})):
        house.upstream.answer = failure
        snapshot = await house.engine.discovery.refresh()
        assert snapshot.last_error is not None
        answer = await house.client.get(ROUTE, headers=key)
        assert (answer.status_code, answer.content) == (200, NODE_DUMP), failure
    assert await house.reader(key).fetch() == house.engine.discovery.snapshot.runners

    house.upstream.answer = NEWER  # the source is back: the next refresh's document is served
    assert (await house.engine.discovery.refresh()).last_error is None
    assert (await house.client.get(ROUTE, headers=key)).content == NEWER
    assert [runner.app for runner in await house.reader(key).fetch()] == ["newer/app"]


async def test_an_empty_list_is_served_as_an_empty_list(house: Clearinghouse) -> None:
    house.upstream.answer = b"[]"  # a truly empty answer replaces the snapshot, and the document
    await house.engine.discovery.refresh()
    key = house.keys["tenant_admin"]
    assert (await house.client.get(ROUTE, headers=key)).content == b"[]"
    assert await house.reader(key).fetch() == ()


async def test_before_any_document_was_read_the_route_is_503_and_a_reader_fails_its_refresh() -> None:
    """Never an empty list in place of an unknown one: an engine reading the route would empty its
    catalog. A 503 is a failed refresh there, which keeps what it had."""
    async with clearinghouse(httpx.ConnectError("refused")) as house:
        assert house.engine.discovery.snapshot.observed_at is None
        key = house.keys["tenant_admin"]
        answer = await house.client.get(ROUTE, headers=key)
        assert answer.status_code == 503 and answer.headers["Retry-After"] == "5"
        assert "runner list" in answer.json()["error"]
        with pytest.raises(httpx.HTTPStatusError):
            await house.reader(key).fetch()
        assert (await house.client.get(ROUTE)).status_code == 401  # a caller is still authenticated first

        house.upstream.answer = GOOD
        await house.engine.discovery.refresh()
        assert (await house.client.get(ROUTE, headers=key)).content == GOOD


# -- a source with no document ------------------------------------------------------------------------------


async def test_a_supplied_source_that_holds_no_document_serves_no_route() -> None:
    """A deployment may replace the discovery source with one that reads no node document (a wrapper,
    or a list of its own). There is then nothing in node's format to serve, and the engine invents none."""

    class OwnList:
        async def fetch(self) -> tuple[Runner, ...]:
            return read_inventory(GOOD)

    def own(given: EngineParts) -> EngineParts:
        return dataclasses.replace(given, discovery_source=OwnList())

    async with clearinghouse(GOOD, decorate=own) as house:
        assert house.engine.runner_list is None
        assert len(house.engine.discovery.snapshot.runners) == 1  # the engine itself runs on that list
        missing = await house.client.get(ROUTE, headers=house.keys["tenant_admin"])
        assert missing.status_code == 404


async def test_the_shipped_fake_discovery_is_served_too() -> None:
    """``make_engine(runners=...)`` runs the real reader over a node-format document, so an engine built
    for a test serves its list like any other."""
    async with make_engine() as engine:
        assert engine.keys is not None and engine.runner_list is not None
        document_served = engine.runner_list.document(OPERATOR)
        assert document_served is not None
        assert [entry["address"] for entry in json.loads(document_served)] == [ORCHESTRATOR]
        assert read_inventory(document_served) == engine.discovery.snapshot.runners
