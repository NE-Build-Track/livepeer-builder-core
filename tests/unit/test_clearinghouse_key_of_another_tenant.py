"""#165, the review of #185: a tenant admin key that belongs to another tenant than the engine's.

A tenant's own routes at a clearinghouse name no tenant: the clearinghouse acts on the tenant of the key
(D87 point 3, as amended on 19 November 2026). So nothing at the clearinghouse compares the tenant an
engine means, ``LPB_CLEARINGHOUSE_TENANT_ID``, with the key it sends, ``LPB_CLEARINGHOUSE_TOKEN``. With
another tenant's key the engine would allot from that tenant's grant, top up and revoke that tenant's
actors, read that tenant's usage and store that tenant's keys, and record all of it under its own
tenant. When the paths named the tenant, the clearinghouse refused such a key with 403.

The engine now learns whose key it holds before it trusts the clearinghouse with anything: once in a
process, before the first request, it reads the key's grant, which states the tenant. Another tenant
than its own is a refusal (403) of whatever was asked, and nothing is sent. Every later answer that
states a tenant is held to the same rule.

Here the clearinghouse is a real one (``tests/unit/payment_ports.py``) with two tenants, and each tenant
has an actor ``alice``. The engine allotted its own alice while its key was right. Then it is started
again with the other tenant's key.
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast

import httpx
import pytest

from livepeer_builder import EngineSettings
from livepeer_builder.adapters.payment_probes import probe_payments
from livepeer_builder.adapters.sdk import HttpDiscoverySource
from livepeer_builder.contracts import ActorAllocation, AllotRequest, TopUpRequest, UsageRow
from livepeer_builder.errors import ProviderError, ProviderUnavailable
from livepeer_builder.services.ledger import definite_refusal
from tests.unit.payment_ports import CLEARINGHOUSE_URL, OPERATOR, Ports, open_ports, usd

GRANT = "/v1/clearinghouse/grant"
RUNNER_LIST = CLEARINGHOUSE_URL + "/v1/clearinghouse/discovery"
SAYS = "belongs to another tenant than LPB_CLEARINGHOUSE_TENANT_ID"


@pytest.fixture
async def ports() -> AsyncIterator[Ports]:
    async with open_ports("remote") as opened:
        yield opened


class Before:
    """What there was before the engine was given the wrong key, to compare with what there is after."""

    def __init__(self, ports: Ports, mine: ActorAllocation, theirs: ActorAllocation) -> None:
        self.mine = mine  # the engine's own alice, allotted with the right key
        self.theirs = theirs  # the other tenant's alice, whom a command sent with its key would reach
        self.provider = at_the_provider(ports)
        self.secrets = dict(cast(Any, ports.secrets).values)


def at_the_provider(ports: Ports) -> dict[str, Any]:
    """Everything the payment provider holds: each allocation's amount and status, every key and whether
    it is ended, and what each tenant's grant has left."""
    provider = ports.provider
    return {
        "allocations": {key: (held.granted, held.status) for key, held in provider.allocations.items()},
        "keys": {key: (held, revoked) for key, (held, _, revoked) in provider.keys.items()},
        "unallocated": [provider.unallocated(ports.tenant_id), provider.unallocated(ports.other_tenant)],
    }


def every_key(ports: Ports) -> set[str]:
    """Both tenant admin keys, and every allocation's key the provider ever minted."""
    return {ports.token, ports.other_token, *(secret for _, secret, _ in ports.provider.keys.values())}


async def everything_stored(ports: Ports) -> str:
    """Every row of every table of the engine's database, as text."""
    rows: list[Any] = []
    async with ports.engine.store.engine.connect() as conn:  # type: ignore[attr-defined]
        tables = await conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type = 'table'")
        for (table,) in tables.all():
            rows += (await conn.exec_driver_sql(f'SELECT * FROM "{table}"')).all()  # noqa: S608
    return repr(rows)


async def given_another_tenants_key(ports: Ports) -> Before:
    """The engine allots its alice with its own key; the other tenant has an alice of its own; then the
    engine is started again with the other tenant's key and its own tenant's id."""
    assert ports.clearinghouse is not None
    mine = await ports.engine.payments.allot(
        ports.admin,
        AllotRequest(tenant_id=ports.tenant_id, actor_id="alice", amount=usd(5), operation_ref="allot-alice"),
    )
    theirs = await ports.clearinghouse.payments.allot_for_delivery(
        OPERATOR,
        AllotRequest(tenant_id=ports.other_tenant, actor_id="alice", amount=usd(7), operation_ref="theirs"),
    )
    before = Before(ports, mine, theirs)
    ports.started_again_with(ports.other_token)
    ports.requests.clear()
    return before


async def refused(call: Awaitable[Any]) -> ProviderError:
    with pytest.raises(ProviderError) as error:
        await call
    return error.value


async def nothing_happened(
    ports: Ports, before: Before, error: ProviderError, caplog: pytest.LogCaptureFixture
) -> None:
    """The refusal says whose key it is and what to set, it freed its reference, only the read of the
    key's grant crossed the wire, the provider and the engine hold what they held, and no key is in the
    error, the log or the engine's database."""
    assert error.status_code == 403 and definite_refusal(error), error
    assert SAYS in error.message and "LPB_CLEARINGHOUSE_TOKEN" in error.message
    assert repr(ports.tenant_id) in error.message and repr(ports.other_tenant) in error.message
    assert "Nothing was applied" in error.message
    assert ports.sent("POST") == [] and set(ports.sent("GET")) == {GRANT}
    assert at_the_provider(ports) == before.provider
    assert cast(Any, ports.secrets).values == before.secrets
    assert await ports.engine.store.list_actors(ports.tenant_id) == [before.mine]
    assert await ports.engine.store.list_actors(ports.other_tenant) == []
    stored = await everything_stored(ports)
    for key in every_key(ports):
        assert key not in f"{error} {error!r} {error.args!r} {vars(error)!r}"
        assert key not in caplog.text
        assert key not in stored


# -- the commands -------------------------------------------------------------------------------------------


async def test_an_allotment_with_another_tenants_key_is_refused_and_allots_nothing(
    ports: Ports, caplog: pytest.LogCaptureFixture
) -> None:
    """The second review thread: an allotment has no earlier allocation to compare. Sent, it would take
    the amount from the other tenant's grant and hand this engine the key of an allocation there."""
    caplog.set_level(logging.DEBUG)
    before = await given_another_tenants_key(ports)
    request = AllotRequest(
        tenant_id=ports.tenant_id, actor_id="bob", amount=usd(5), operation_ref="allot-bob"
    )

    error = await refused(ports.engine.payments.allot(ports.admin, request))

    await nothing_happened(ports, before, error, caplog)
    assert await ports.engine.store.get_actor(ports.tenant_id, "bob") is None
    assert await ports.engine.store.get_operation(OPERATOR.tenant_id, OPERATOR.actor_id, "allot-bob") is None


async def test_a_rotation_with_another_tenants_key_is_refused_and_rotates_nothing(
    ports: Ports, caplog: pytest.LogCaptureFixture
) -> None:
    """Sent, it would end the key of the other tenant's alice and hand this engine her new one."""
    caplog.set_level(logging.DEBUG)
    before = await given_another_tenants_key(ports)

    error = await refused(
        ports.engine.payments.rotate_payment_key(ports.admin, ports.tenant_id, "alice", "rotate-1")
    )

    await nothing_happened(ports, before, error, caplog)
    assert await ports.engine.store.get_operation(OPERATOR.tenant_id, OPERATOR.actor_id, "rotate-1") is None


async def test_a_top_up_with_another_tenants_key_is_refused_and_adds_nothing(
    ports: Ports, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    before = await given_another_tenants_key(ports)
    top_up = TopUpRequest(amount=usd(3), operation_ref="t-1")

    error = await refused(ports.engine.payments.top_up(ports.admin, ports.tenant_id, "alice", top_up))

    await nothing_happened(ports, before, error, caplog)
    assert await ports.engine.store.get_operation(OPERATOR.tenant_id, OPERATOR.actor_id, "t-1") is None


async def test_a_status_change_with_another_tenants_key_is_refused_and_pauses_nobody(
    ports: Ports, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    before = await given_another_tenants_key(ports)

    error = await refused(ports.engine.payments.set_status(ports.admin, ports.tenant_id, "alice", "paused"))

    await nothing_happened(ports, before, error, caplog)


async def test_a_revoke_with_another_tenants_key_is_refused_and_ends_nothing(
    ports: Ports, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    before = await given_another_tenants_key(ports)

    error = await refused(ports.engine.payments.revoke(ports.admin, ports.tenant_id, "alice"))

    await nothing_happened(ports, before, error, caplog)


# -- the reads ----------------------------------------------------------------------------------------------


async def test_an_allowance_read_with_another_tenants_key_shows_nothing_of_that_tenant(
    ports: Ports, caplog: pytest.LogCaptureFixture
) -> None:
    """The port refuses, and the allocation read an admin makes answers the engine's own record with no
    allowance: never the other tenant's amounts under this tenant's actor."""
    caplog.set_level(logging.DEBUG)
    before = await given_another_tenants_key(ports)

    error = await refused(ports.allowances.read_allowance(before.mine.allocation_id))
    read = await ports.engine.payments.get_allocation(ports.admin, ports.tenant_id, "alice")

    await nothing_happened(ports, before, error, caplog)
    assert read.allocation_id == before.mine.allocation_id and read.allowance is None


async def test_the_grant_read_with_another_tenants_key_is_refused(
    ports: Ports, caplog: pytest.LogCaptureFixture
) -> None:
    """The first review thread: ``read_grant`` answered whatever grant the key's tenant has."""
    caplog.set_level(logging.DEBUG)
    before = await given_another_tenants_key(ports)

    error = await refused(ports.manager.read_grant(ports.tenant_id))

    await nothing_happened(ports, before, error, caplog)


def usage_of(allocation_id: str, number: int) -> UsageRow:
    return UsageRow(
        id=f"row-{number}",
        event_id=None,
        status="applied",
        manifest_id=f"m-{number}",
        allocation_id=allocation_id,
        payment_session_id=None,
        request_id=None,
        pipeline=None,
        computed_fee_eth=None,
        computed_fee_usd=Decimal("0.5"),
        currency="usd",
        created_at=datetime(2026, 11, 23, 12, number, tzinfo=UTC),
    )


async def test_cost_sync_with_another_tenants_key_stores_nothing(
    ports: Ports, caplog: pytest.LogCaptureFixture
) -> None:
    """The fourth review thread: a usage page states no tenant, so the engine must know whose key it
    holds before it reads one. Cost sync fails and stores no row and no cursor, and a cursor it kept
    from its own tenant is not followed with the other tenant's key."""
    caplog.set_level(logging.DEBUG)
    assert ports.clearinghouse is not None
    mine = await ports.engine.payments.allot(
        ports.admin,
        AllotRequest(tenant_id=ports.tenant_id, actor_id="alice", amount=usd(5), operation_ref="allot-alice"),
    )
    theirs = await ports.clearinghouse.payments.allot_for_delivery(
        OPERATOR,
        AllotRequest(tenant_id=ports.other_tenant, actor_id="alice", amount=usd(7), operation_ref="theirs"),
    )
    ports.provider_usage.grant_of = {
        mine.allocation_id: ports.tenant_id,
        theirs.allocation_id: ports.other_tenant,
    }
    ports.provider_usage.rows = [usage_of(mine.allocation_id, 1)]
    await ports.engine.costs.sync_once()  # with the right key: the engine's own row, and its cursor
    kept = await ports.engine.store.cost_checkpoint()
    assert kept is not None and "row-1" in await everything_stored(ports)
    ports.provider_usage.rows += [usage_of(theirs.allocation_id, n) for n in (2, 3)]
    before = Before(ports, mine, theirs)
    ports.started_again_with(ports.other_token)
    ports.requests.clear()

    error = await refused(ports.engine.costs.sync_once())
    page = await refused(ports.usage.page(None))

    await nothing_happened(ports, before, error, caplog)
    assert page.status_code == 403 and SAYS in page.message
    assert await ports.engine.store.cost_checkpoint() == kept
    stored = await everything_stored(ports)
    assert "row-2" not in stored and "row-3" not in stored


async def test_the_runner_list_is_not_read_with_another_tenants_key(
    ports: Ports, caplog: pytest.LogCaptureFixture
) -> None:
    """The runner list states no tenant either. A reader that carries the tenant admin key asks whose
    key it is first, and reads no list with another tenant's."""
    caplog.set_level(logging.DEBUG)
    before = await given_another_tenants_key(ports)
    assert ports.wire is not None
    client = httpx.AsyncClient(transport=ports.wire._client._transport)
    reader = HttpDiscoverySource(
        RUNNER_LIST, client=client, bearer=ports.other_token, confirm=ports.confirm_tenant
    )

    error = await refused(reader.fetch())

    await nothing_happened(ports, before, error, caplog)
    assert reader.document() is None
    assert RUNNER_LIST not in {str(request.url) for request in ports.requests}


# -- the engine learns whose key it holds once, and before anything else -------------------------------


async def test_the_first_request_of_a_process_is_the_read_of_the_keys_grant_and_it_is_made_once() -> None:
    """With the right key: one read of the grant before the first request, whatever that is, and none
    after. A read that fails says nothing about the key, so the next request asks again."""
    async with open_ports("remote") as ports:
        ports.started_again_with(ports.token)  # a new process: nothing is known of the key yet
        ports.requests.clear()
        ports.lose_the_answer_to("/grant")
        with pytest.raises(ProviderUnavailable):
            await ports.usage.page(None)
        assert ports.sent("GET") == [GRANT]  # the page was not asked for

        await ports.usage.page(None)
        request = AllotRequest(
            tenant_id=ports.tenant_id, actor_id="alice", amount=usd(5), operation_ref="allot-alice"
        )
        allotted = await ports.engine.payments.allot(ports.admin, request)
        await ports.allowances.read_allowance(allotted.allocation_id)

        assert ports.sent("GET")[:3] == [GRANT, GRANT, "/v1/clearinghouse/usage"]
        assert ports.sent("GET").count(GRANT) == 2  # the lost read, and the one that answered
        assert ports.sent("POST") == ["/v1/clearinghouse/actors/alice/allocation"]


# -- an answer that states another tenant, after the clearinghouse said the key was this tenant's ---------


async def test_an_allotment_answered_for_another_tenant_is_no_success_and_its_key_is_dropped(
    ports: Ports, caplog: pytest.LogCaptureFixture
) -> None:
    """Every answer that states a tenant is read, the allotment's too. Here the clearinghouse says the
    key is this tenant's and then allots at the other one. The clearinghouse has applied it, so it is no
    refusal and its reference stays held; nothing is saved here, the key the answer handed over is
    stored nowhere, and the error says what the other tenant's admin must do."""
    caplog.set_level(logging.DEBUG)
    assert ports.transport is not None
    ports.started_again_with(ports.other_token)
    ports.transport.grant_says = ports.tenant_id
    request = AllotRequest(
        tenant_id=ports.tenant_id, actor_id="bob", amount=usd(5), operation_ref="allot-bob"
    )

    error = await refused(ports.engine.payments.allot(ports.admin, request))

    assert error.status_code == 200 and not definite_refusal(error)
    assert SAYS in error.message and "applied this command to tenant" in error.message
    assert "Nothing of it is recorded here" in error.message
    assert "revokes the allocation at the clearinghouse" in error.message
    # what the clearinghouse did: an allocation at the other tenant, from the other tenant's grant
    [(allocation_id, held)] = ports.provider.allocations.items()
    assert held.grant_id == ports.other_tenant and ports.provider.unallocated(ports.tenant_id) == 100
    # and here: no actor, no secret, a reference that is still held, and the handed key nowhere
    assert await ports.engine.store.get_actor(ports.tenant_id, "bob") is None
    assert cast(Any, ports.secrets).values == {}
    held_ref = await ports.engine.store.get_operation(OPERATOR.tenant_id, OPERATOR.actor_id, "allot-bob")
    assert held_ref is not None and held_ref.state == "started"
    stored = await everything_stored(ports)
    for key in every_key(ports):
        assert key not in f"{error} {error!r} {error.args!r} {vars(error)!r}"
        assert key not in caplog.text and key not in stored
    assert allocation_id not in stored  # nothing here points at the other tenant's allocation


async def test_a_read_answered_for_another_tenant_is_refused_and_nothing_is_sent_after_it(
    ports: Ports, caplog: pytest.LogCaptureFixture
) -> None:
    """A top-up reads the actor's allocation first. The other tenant's alice answers: the command is
    refused by that answer, and its POST is never sent."""
    caplog.set_level(logging.DEBUG)
    assert ports.transport is not None
    before = await given_another_tenants_key(ports)
    ports.transport.grant_says = ports.tenant_id
    top_up = TopUpRequest(amount=usd(3), operation_ref="t-1")

    error = await refused(ports.engine.payments.top_up(ports.admin, ports.tenant_id, "alice", top_up))

    assert error.status_code == 403 and definite_refusal(error) and SAYS in error.message
    assert ports.sent("POST") == [] and at_the_provider(ports) == before.provider
    assert await ports.engine.store.list_actors(ports.tenant_id) == [before.mine]


async def test_an_answer_for_another_tenant_ends_what_the_engine_knew_of_its_key(
    ports: Ports,
) -> None:
    """The clearinghouse said the key was this tenant's, and then answered a read for another. What the
    engine knew of its key is gone with that answer: it asks again before anything else. It kept the
    first answer for the life of the process, so the next usage page, which states no tenant, was asked
    for and taken as this tenant's (John, #185 review)."""
    assert ports.transport is not None
    await given_another_tenants_key(ports)
    ports.transport.grant_says = ports.tenant_id  # the clearinghouse says the key is this tenant's
    top_up = TopUpRequest(amount=usd(3), operation_ref="t-1")
    await refused(ports.engine.payments.top_up(ports.admin, ports.tenant_id, "alice", top_up))
    ports.transport.grant_says = None  # and now says whose it is
    ports.requests.clear()

    error = await refused(ports.usage.page(None))

    assert error.status_code == 403 and SAYS in error.message
    assert ports.sent("GET") == [GRANT]  # the grant was read again, and the page was not asked for


async def test_the_first_requests_of_a_process_share_one_read_of_the_keys_grant() -> None:
    """Requests that arrive together before the key's grant has been read wait for one read of it. Each
    started a read of its own, and each of those has the clearinghouse read its balances: a burst at a
    cold start made healthy requests time out (John, #185 review)."""
    async with open_ports("remote") as ports:
        assert ports.transport is not None
        ports.started_again_with(ports.token)
        ports.requests.clear()
        ports.transport.takes = lambda request: 0.05 if request.url.path == GRANT else 0

        pages = await asyncio.gather(*(ports.usage.page(None) for _ in range(5)))

        assert len(pages) == 5
        assert ports.sent("GET").count(GRANT) == 1
        assert ports.sent("GET").count("/v1/clearinghouse/usage") == 5


async def test_a_read_of_the_keys_grant_that_fails_fails_those_that_waited_and_is_made_again() -> None:
    """The requests that waited for a read that failed fail with it, at once and without a read each. The
    next request reads the grant again."""
    async with open_ports("remote") as ports:
        assert ports.transport is not None
        ports.started_again_with(ports.token)
        ports.requests.clear()
        ports.transport.takes = lambda request: 0.05 if request.url.path == GRANT else 0
        ports.lose_the_answer_to("/grant")

        ended = await asyncio.gather(*(ports.usage.page(None) for _ in range(5)), return_exceptions=True)

        assert [type(end) for end in ended] == [ProviderUnavailable] * 5
        assert ports.sent("GET") == [GRANT]  # one read, and no page
        await ports.usage.page(None)
        assert ports.sent("GET") == [GRANT, GRANT, "/v1/clearinghouse/usage"]


async def test_the_runner_list_s_deadline_covers_the_question_of_whose_key_it_is() -> None:
    """Discovery gives a read five seconds. The question came before that clock started, so a slow
    answer to it held a refresh for the clearinghouse's own timeout and then discovery's (John, #185
    review). The deadline covers both."""
    asked: list[str] = []

    async def slow_to_say() -> None:
        asked.append("whose key")
        await asyncio.sleep(5)

    def never(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no list is read before the key's tenant is known")

    client = httpx.AsyncClient(transport=httpx.MockTransport(never))
    reader = HttpDiscoverySource(
        RUNNER_LIST, client=client, bearer="lpk_x", confirm=slow_to_say, timeout_s=0.1
    )
    started = asyncio.get_running_loop().time()
    with pytest.raises(Exception) as ended:  # noqa: PT011 - the kind is asserted below
        await asyncio.wait_for(reader.fetch(), timeout=3)
    assert asyncio.get_running_loop().time() - started < 1
    assert asked == ["whose key"] and isinstance(ended.value, ProviderUnavailable | TimeoutError)
    assert not isinstance(ended.value, asyncio.CancelledError)


# -- lpb payments check ---------------------------------------------------------------------------------


async def test_the_payments_check_fails_plainly_for_another_tenants_key(ports: Ports) -> None:
    """The third and fourth review threads named the check as the one place that compared the key's
    tenant with the setting. It says so plainly, prints no grant, and does not call the other tenant's
    usage page this tenant's."""
    assert ports.wire is not None
    settings = EngineSettings.model_validate(
        {
            "role": "work",
            "clearinghouse_url": CLEARINGHOUSE_URL,
            "clearinghouse_token": ports.other_token,
            "clearinghouse_tenant_id": ports.tenant_id,
            "signer_url": "http://signer.test:8935",
        }
    )
    mounts = {
        "all://clearinghouse.test": ports.wire._client._transport,
        "all://signer.test": httpx.MockTransport(lambda _: httpx.Response(405, headers={"Allow": "POST"})),
    }
    async with httpx.AsyncClient(mounts=mounts) as client:
        report = await probe_payments(settings, client=client)

    found = {probe.name: probe for probe in report.probes}
    assert report.ok is False and report.tenant is None
    grant, usage = found["clearinghouse_grant"], found["clearinghouse_usage"]
    assert grant.outcome == "unexpected" and SAYS in grant.detail
    assert "LPB_CLEARINGHOUSE_TOKEN" in grant.detail
    assert usage.outcome == "unexpected" and "another tenant" in usage.detail
    assert found["signer"].outcome == "ok"
    printed = report.model_dump_json()
    assert ports.other_token not in printed and ports.other_tenant not in printed
    assert {request.method for request in ports.requests} == {"GET"}
