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
import contextlib
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
from livepeer_builder.errors import ProviderError, ProviderOutcomeUnknown, ProviderUnavailable
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


async def test_the_runner_list_is_read_with_the_key_and_asks_nothing_of_the_grant(ports: Ports) -> None:
    """The runner list is the same for every tenant, so whose key the reader carries changes nothing of
    it: the reader sends the key, which the route needs, and does not read the key's grant first. With
    another tenant's key the engine still has a catalog, and the key is refused everything else it is
    sent with (John, #185 review; the reader asked first, and a wrong key left the engine no catalog)."""
    await given_another_tenants_key(ports)
    assert ports.wire is not None
    client = httpx.AsyncClient(transport=ports.wire._client._transport)
    reader = HttpDiscoverySource(RUNNER_LIST, client=client, bearer=ports.other_token)
    ports.requests.clear()

    with contextlib.suppress(httpx.HTTPStatusError):  # this clearinghouse may have read no list yet
        await reader.fetch()

    [asked] = ports.requests
    assert str(asked.url) == RUNNER_LIST and asked.headers["authorization"] == f"Bearer {ports.other_token}"
    assert GRANT not in ports.sent("GET")


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


async def answered_for_another_tenant(ports: Ports) -> None:
    """A read the clearinghouse answers for the other tenant: the top-up's read of alice's allocation."""
    top_up = TopUpRequest(amount=usd(3), operation_ref="t-1")
    await refused(ports.engine.payments.top_up(ports.admin, ports.tenant_id, "alice", top_up))


async def test_a_read_of_the_grant_that_was_in_flight_does_not_bring_the_keys_tenant_back(
    ports: Ports,
) -> None:
    """A grant read is made beside other requests (the tenant desk reads the grant by itself). One that
    was sent before an answer for another tenant, and comes back after it naming this tenant, is older
    word than the contradiction: it set the key right again, and the next usage page was taken without a
    new read (review of ``f71101e``)."""
    assert ports.transport is not None
    await given_another_tenants_key(ports)
    ports.transport.grant_says = ports.tenant_id
    wire = ports.manager._wire  # type: ignore[attr-defined]
    await wire.confirm()
    ports.transport.takes = lambda request: 0.2 if request.url.path == GRANT else 0
    in_flight = asyncio.create_task(wire.grant())
    await asyncio.sleep(0.05)
    await answered_for_another_tenant(ports)
    assert (await in_flight).tenant_id == ports.tenant_id  # its own caller has the answer it asked for
    ports.transport.takes = None
    ports.requests.clear()

    await ports.usage.page(None)

    assert ports.sent("GET") == [GRANT, "/v1/clearinghouse/usage"]  # asked again before the page


@pytest.mark.parametrize("then", ["nothing more", "the key is confirmed again"])
async def test_a_usage_page_in_flight_when_the_keys_tenant_came_into_question_is_not_taken(
    ports: Ports, then: str
) -> None:
    """A usage page states no tenant: it is taken on what the engine knows of its key. One that was asked
    for before an answer for another tenant, and arrives after it, was sent with the key that has just
    failed the check, and its rows are that tenant's payments. It is not taken, whatever the engine has
    learned since: another request may have read the grant again by then (John, #185 review)."""
    assert ports.transport is not None
    await given_another_tenants_key(ports)
    ports.transport.grant_says = ports.tenant_id
    wire = ports.manager._wire  # type: ignore[attr-defined]
    await wire.confirm()
    ports.transport.takes = lambda request: 0.2 if request.url.path.endswith("/usage") else 0
    page = asyncio.create_task(ports.usage.page(None))
    await asyncio.sleep(0.05)
    await answered_for_another_tenant(ports)
    if then == "the key is confirmed again":
        await wire.confirm()

    error = await refused(page)

    assert error.status_code == 403 and "came into question" in error.message
    assert "/v1/clearinghouse/usage" in ports.sent("GET")  # it was asked for, and its answer dropped


async def test_a_first_read_of_the_grant_that_was_contradicted_on_its_way_confirms_nothing(
    ports: Ports,
) -> None:
    """The same for the read a first request waits for: it comes back naming this tenant, after another
    read of the grant was answered for the other one. The request that waited is refused, and nothing
    of it is sent."""
    assert ports.transport is not None
    await given_another_tenants_key(ports)
    wire = ports.manager._wire  # type: ignore[attr-defined]
    slow_once = [0.2]
    ports.transport.takes = lambda request: slow_once.pop() if slow_once and request.url.path == GRANT else 0
    ports.requests.clear()
    page = asyncio.create_task(ports.usage.page(None))  # its read of the grant is on its way
    await asyncio.sleep(0.05)
    assert (await refused(wire.grant())).status_code == 403  # a second read says whose key it is
    ports.transport.grant_says = ports.tenant_id  # and the first comes back with the older word

    error = await refused(page)

    assert error.status_code == 403 and "came into question" in error.message
    assert ports.sent("GET") == [GRANT, GRANT]  # no page was asked for


@pytest.mark.parametrize("then", ["nothing more", "the key is confirmed again"])
async def test_a_command_overtaken_by_an_answer_for_another_tenant_sends_nothing_more(then: str) -> None:
    """A command is made ready once, before its record is begun, and then makes its calls. An answer for
    another tenant that arrives between two of them stops it: the next call is not sent. It went on
    when the key had been confirmed again by then, and a top-up sent its payment (John, #185 review).
    It asks nothing of the grant itself either, inside the budget of its record."""
    async with open_ports("remote") as ports:
        assert ports.transport is not None
        allot = AllotRequest(tenant_id=ports.tenant_id, actor_id="alice", amount=usd(5), operation_ref="a-1")
        await ports.engine.payments.allot(ports.admin, allot)
        wire = ports.manager._wire  # type: ignore[attr-defined]
        request = TopUpRequest(amount=usd(3), operation_ref="t-1")
        ports.requests.clear()
        slow = [0.2]  # the top-up's read of the allocation, held on its way
        ports.transport.takes = lambda asked: (
            slow.pop() if slow and asked.url.path.endswith("/allocation") else 0
        )
        top_up = asyncio.create_task(
            ports.engine.payments.top_up(ports.admin, ports.tenant_id, "alice", request)
        )
        await asyncio.sleep(0.05)
        ports.transport.grant_says = ports.other_tenant
        await refused(wire.grant())  # a second request is answered for another tenant
        ports.transport.grant_says = None
        if then == "the key is confirmed again":
            await wire.confirm()  # and a third reads the grant again, which names this tenant

        error = await refused(top_up)

        assert error.status_code == 403 and definite_refusal(error) and "came into question" in error.message
        assert ports.sent("POST") == []  # no payment was sent
        own_reads = 2 if then == "the key is confirmed again" else 1
        assert ports.sent("GET").count(GRANT) == own_reads  # the test's own: the top-up read no grant
        # nothing was sent that could have been applied, so the reference is free: the same request runs
        await ports.engine.payments.top_up(ports.admin, ports.tenant_id, "alice", request)
        assert ports.sent("POST") == ["/v1/clearinghouse/actors/alice/allocation/top-up"]


async def test_a_command_stopped_after_it_wrote_is_an_unknown_outcome_and_not_a_refusal(ports: Ports) -> None:
    """Once a command has sent a write, the clearinghouse may have applied it: a stop before its next
    call is no refusal, and the operation ledger keeps the reference."""
    assert ports.transport is not None
    allot = AllotRequest(tenant_id=ports.tenant_id, actor_id="alice", amount=usd(5), operation_ref="a-1")
    await ports.engine.payments.allot(ports.admin, allot)
    shared = ports.manager._wire  # type: ignore[attr-defined]
    command = shared.for_command()
    path = "/v1/clearinghouse/actors/alice/allocation"
    await command.write_as(ActorAllocation, path + "/status", {"status": "paused"})
    ports.transport.grant_says = ports.other_tenant
    await refused(shared.grant())
    ports.requests.clear()

    with pytest.raises(ProviderOutcomeUnknown, match="came into question") as stopped:
        await command.read_as(ActorAllocation, path)

    assert not definite_refusal(stopped.value) and ports.requests == []


async def test_one_commands_manager_goes_by_the_engines_one_answer_of_whose_key_it_is(ports: Ports) -> None:
    """The manager of one command is still the engine's manager: made ready, or asked for the grant, it
    asks the wire all the adapters share, and what it learns there is what the others go by. Called
    with nobody having made it ready, it asks at its first call, and is stopped as any command is."""
    assert ports.transport is not None
    await given_another_tenants_key(ports)
    ports.transport.grant_says = ports.tenant_id
    shared = ports.manager._wire  # type: ignore[attr-defined]
    command = ports.manager.for_actor(ports.tenant_id, "alice")
    assert command.for_actor(ports.tenant_id, "alice")._wire is command._wire  # type: ignore[attr-defined]
    ports.requests.clear()

    await command.ready_for_commands()  # type: ignore[attr-defined]
    assert (await command.read_grant(ports.tenant_id)).tenant_id == ports.tenant_id

    assert shared._confirmed and ports.sent("GET") == [GRANT, GRANT]
    await refused(command.read_allocation("alloc_none"))  # its first call: it goes by the key as it is now
    ports.transport.grant_says = ports.other_tenant
    await refused(shared.grant())
    ports.requests.clear()
    error = await refused(command.read_allocation("alloc_none"))
    assert "came into question while this command ran" in error.message and ports.requests == []


async def test_a_write_on_the_shared_wire_asks_whose_key_it_is_first(ports: Ports) -> None:
    """Every command of the adapters goes through one command's view of the wire. A write sent on the
    shared wire itself is held to the tenant as a read is: asked first, and not sent with another's key."""
    await given_another_tenants_key(ports)
    shared = ports.manager._wire  # type: ignore[attr-defined]
    ports.requests.clear()

    error = await refused(
        shared.write_as(ActorAllocation, "/v1/clearinghouse/actors/alice/allocation/revoke")
    )

    assert error.status_code == 403 and ports.sent("GET") == [GRANT] and ports.sent("POST") == []


async def test_a_write_on_the_shared_wire_is_sent_with_the_tenants_own_key(ports: Ports) -> None:
    allot = AllotRequest(tenant_id=ports.tenant_id, actor_id="alice", amount=usd(5), operation_ref="a-1")
    await ports.engine.payments.allot(ports.admin, allot)
    shared = ports.manager._wire  # type: ignore[attr-defined]
    path = "/v1/clearinghouse/actors/alice/allocation/status"

    assert (await shared.write_as(ActorAllocation, path, {"status": "paused"})).tenant_id == ports.tenant_id


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
