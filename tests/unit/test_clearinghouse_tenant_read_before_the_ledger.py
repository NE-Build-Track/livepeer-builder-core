"""#165, the second review of #185: the read that says whose key the engine holds spends none of a
command's budget.

A command on the operation ledger runs inside half of ``LPB_OPERATION_STALE_S``, and the settings size
that half for the calls the command itself makes at a clearinghouse: three, each within
``LPB_CLEARINGHOUSE_TIMEOUT_S`` (``config.py``). The longest is a rotation that another overtook: it reads
the allocation, sends the rotation, and reads which key the allocation takes. No command writes after its
second call.

The engine also reads the key's grant once in a process, before its first request
(``adapters/clearinghouse_tenant.py``). Made inside a command, that read was a fourth call: the first
top-up of a process wrote at its third, and an overtaken rotation no longer fitted. So the payments
service asks the allocation manager to be ready before it begins a command's record
(``ReadyBeforeCommands``): the read is made there, outside the budget and before the reference is
claimed, and a command finds the tenant known.

Each test here is the first command of a process: the engine is started again, so nothing is known of
its key.
"""

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import httpx
import pytest

from livepeer_builder.adapters.batteries import BatteriesClient
from livepeer_builder.contracts import ActorAllocation, AllotRequest, TopUpRequest
from livepeer_builder.errors import ProviderError, ProviderUnavailable
from livepeer_builder.protocols import ReadyBeforeCommands
from livepeer_builder.testing import FakeAllocationManager
from tests.unit.payment_ports import OPERATOR, Ports, open_ports, usd
from tests.unit.test_clearinghouse_handed_keys import a_whole_before_b_reaches_the_clearinghouse, settled

GRANT = "/v1/clearinghouse/grant"
ALLOCATION = "/v1/clearinghouse/actors/alice/allocation"
Command = Callable[[Ports], Awaitable[object]]


@pytest.fixture
async def ports() -> AsyncIterator[Ports]:
    async with open_ports("remote") as opened:
        yield opened


def allot_alice(ports: Ports) -> Awaitable[ActorAllocation]:
    request = AllotRequest(
        tenant_id=ports.tenant_id, actor_id="alice", amount=usd(5), operation_ref="allot-alice"
    )
    return ports.engine.payments.allot(ports.admin, request)


def top_up(ports: Ports) -> Awaitable[object]:
    request = TopUpRequest(amount=usd(1), operation_ref="t-1")
    return ports.engine.payments.top_up(ports.admin, ports.tenant_id, "alice", request)


def set_status(ports: Ports) -> Awaitable[object]:
    return ports.engine.payments.set_status(ports.admin, ports.tenant_id, "alice", "paused")


def revoke(ports: Ports) -> Awaitable[object]:
    return ports.engine.payments.revoke(ports.admin, ports.tenant_id, "alice")


def rotate(ports: Ports, ref: str = "r-b") -> Awaitable[object]:
    return ports.engine.payments.rotate_payment_key(ports.admin, ports.tenant_id, "alice", ref)


def records_begun(ports: Ports, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, int]]:
    """Each operation record the engine begins from now on, as (command, how many requests had crossed
    the wire when it was begun): the moment the ledger's budget starts."""
    store = ports.engine.store
    original = store.begin_operation
    begun: list[tuple[str, int]] = []

    async def begin(record: Any) -> Any:
        begun.append((record.command, len(ports.requests)))
        return await original(record)

    monkeypatch.setattr(store, "begin_operation", begin)
    return begun


def crossed(ports: Ports, since: int = 0) -> list[tuple[str, str]]:
    return [(request.method, request.url.path) for request in ports.requests[since:]]


async def a_new_process(ports: Ports, *, allotted: bool) -> None:
    """The engine started again with its own key, with alice allotted before or not: nothing is known
    of the key, and no request has been sent."""
    if allotted:
        await allot_alice(ports)
    ports.started_again_with(ports.token)
    ports.requests.clear()


# What each command asks of the clearinghouse itself, which #182 pinned for a later command of a process
# (``test_no_command_writes_after_its_second_call_and_a_third_call_only_reads``): one or two calls, and
# no write after the second. On the ledger or not: a status change and a revoke take no reference and
# begin no record (D57), so they run under no budget, and a status change reads the status back in a
# third call (#158).
COMMANDS: dict[str, tuple[Command, bool, list[tuple[str, str]]]] = {
    "allot": (allot_alice, True, [("POST", ALLOCATION)]),
    "top_up": (top_up, True, [("GET", ALLOCATION), ("POST", ALLOCATION + "/top-up")]),
    "rotate_payment_key": (
        rotate,
        True,
        [("GET", ALLOCATION), ("POST", ALLOCATION + "/rotate-payment-key")],
    ),
    "set_status": (
        set_status,
        False,
        [("GET", ALLOCATION), ("POST", ALLOCATION + "/status"), ("GET", ALLOCATION)],
    ),
    "revoke": (revoke, False, [("GET", ALLOCATION), ("POST", ALLOCATION + "/revoke")]),
}


@pytest.mark.parametrize("name", list(COMMANDS))
async def test_the_first_command_of_a_process_reads_the_keys_grant_before_its_record_is_begun(
    ports: Ports, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """The read of the key's grant is the first request, it is made once, and a command on the ledger
    begins its record after it. Inside the budget the command makes the calls it made before, and no
    more: what the six-timeouts rule counts holds for a first command too."""
    command, on_the_ledger, own_calls = COMMANDS[name]
    await a_new_process(ports, allotted=name != "allot")
    begun = records_begun(ports, monkeypatch)

    await command(ports)

    assert crossed(ports) == [("GET", GRANT), *own_calls]
    assert begun == ([(name, 1)] if on_the_ledger else [])  # one request before the record: the grant read
    inside = crossed(ports, since=1)
    assert ("GET", GRANT) not in inside and len(inside) <= (2 if on_the_ledger else 3)
    assert [method for method, _ in inside].index("POST") <= 1  # no write after the second call


async def test_an_overtaken_rotation_that_is_the_first_command_makes_three_calls_inside_its_budget(
    ports: Ports, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The longest command. B is the first command of the process, and rotation A overtakes it. The read
    of the key's grant comes before B's record; inside B's budget are A, whole, and B's own three calls,
    the last of which is the read that decides which key the engine keeps."""
    await a_new_process(ports, allotted=True)
    begun = records_begun(ports, monkeypatch)
    a_whole_before_b_reaches_the_clearinghouse(ports, monkeypatch)

    await rotate(ports, "r-b")

    assert crossed(ports)[0] == ("GET", GRANT)
    assert begun == [("rotate_payment_key", 1), ("rotate_payment_key", 1)]  # B, then A inside it
    assert crossed(ports, since=1) == [
        ("GET", ALLOCATION),  # A, whole: its read
        ("POST", ALLOCATION + "/rotate-payment-key"),  # and its rotation
        ("GET", ALLOCATION),  # B reads the allocation
        ("POST", ALLOCATION + "/rotate-payment-key"),  # B's rotation, its second call
        ("GET", ALLOCATION),  # B asks which key the allocation takes, its third
    ]
    await settled(ports, rotations=2)


async def test_an_overtaken_first_rotation_fits_its_budget_when_every_call_takes_almost_a_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The budget is three clearinghouse timeouts (here 3 s, for a timeout of 1 s), and each call B
    makes takes almost one: the read of the key's grant, the read of the allocation, the rotation, and
    the read of which key the allocation takes. With the first of them inside the budget the fourth was
    cut short, and B answered that it ran past its budget. A's calls, which this test puts inside B, take
    no time."""
    almost_a_timeout = 0.8  # four of them are past the budget, and three leave room on a busy machine
    exact = {"operation_stale_s": 6.0, "clearinghouse_timeout_s": 1.0, "batteries_timeout_s": 1.0}
    async with open_ports("remote", **exact) as ports:
        assert ports.transport is not None
        await a_new_process(ports, allotted=True)
        b_is_calling = [False]

        def takes(request: httpx.Request) -> float:
            return almost_a_timeout if b_is_calling[0] or request.url.path == GRANT else 0.0

        async def now_b_calls() -> None:  # NOSONAR - async, as the helper awaits it
            b_is_calling[0] = True

        ports.transport.takes = takes
        a_whole_before_b_reaches_the_clearinghouse(ports, monkeypatch, then=now_b_calls)

        await rotate(ports, "r-b")

        ports.transport.takes = None
        assert crossed(ports)[0] == ("GET", GRANT) and len(crossed(ports)) == 6
        await settled(ports, rotations=2)


async def test_a_wrong_tenant_on_the_first_command_begins_no_record_and_sends_no_command(
    ports: Ports, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refused before the reference is claimed: there is no record to free, and nothing to resume."""
    await allot_alice(ports)
    actors = await ports.engine.store.list_actors(ports.tenant_id)
    assert ports.secrets is not None
    secrets = dict(ports.secrets.values)
    for name, (command, _, _) in COMMANDS.items():
        ports.started_again_with(ports.other_token)
        ports.requests.clear()
        begun = records_begun(ports, monkeypatch)

        with pytest.raises(ProviderError) as refused:
            await (allot_bob(ports) if name == "allot" else command(ports))

        assert refused.value.status_code == 403, name
        assert "belongs to another tenant than LPB_CLEARINGHOUSE_TENANT_ID" in refused.value.message
        assert begun == [], name
        assert crossed(ports) == [("GET", GRANT)], name
        assert await ports.engine.store.list_actors(ports.tenant_id) == actors, name
        assert ports.secrets.values == secrets, name
    for ref in ("allot-bob", "t-1", "r-b"):
        assert await ports.engine.store.get_operation(OPERATOR.tenant_id, OPERATOR.actor_id, ref) is None


def allot_bob(ports: Ports) -> Awaitable[ActorAllocation]:
    request = AllotRequest(
        tenant_id=ports.tenant_id, actor_id="bob", amount=usd(5), operation_ref="allot-bob"
    )
    return ports.engine.payments.allot(ports.admin, request)


@pytest.mark.parametrize("name", ["allot", "top_up", "rotate_payment_key"])
async def test_a_grant_read_that_gets_no_answer_begins_no_record_and_the_same_request_works_after(
    ports: Ports, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """The clearinghouse does not answer the read of the key's grant. The command fails as unavailable
    with nothing sent of it, and no record holds its reference: the same request, once the clearinghouse
    answers, runs as new, with no stale window to wait for."""
    command, _, own_calls = COMMANDS[name]
    await a_new_process(ports, allotted=name != "allot")
    begun = records_begun(ports, monkeypatch)
    ports.lose_the_answer_to("/grant")

    with pytest.raises(ProviderUnavailable):
        await command(ports)

    assert begun == [] and crossed(ports) == [("GET", GRANT)]

    await command(ports)  # the same request, at once

    assert crossed(ports) == [("GET", GRANT), ("GET", GRANT), *own_calls]
    assert begun == [(name, 2)]


async def test_the_repeat_of_a_finished_command_in_a_new_process_reads_the_grant_and_sends_nothing_else(
    ports: Ports, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repeat is answered from the engine's own record. In a process that does not yet know whose key
    it holds, the grant is read first, as before any command: with another tenant's key the repeat is
    refused too."""
    first = await allot_alice(ports)
    ports.started_again_with(ports.token)
    ports.requests.clear()
    begun = records_begun(ports, monkeypatch)

    again = await allot_alice(ports)

    assert again == first and crossed(ports) == [("GET", GRANT)] and begun == [("allot", 1)]


async def test_a_manager_asked_through_the_port_alone_still_reads_the_grant_first(ports: Ports) -> None:
    """A caller of the port that never asks the manager to be ready (a supplied wiring) is as safe: the
    adapter reads the key's grant itself before its first request, inside whatever budget that caller
    runs under."""
    allotted = await allot_alice(ports)
    ports.started_again_with(ports.token)
    ports.requests.clear()
    alice = ports.manager.for_actor(ports.tenant_id, "alice")  # type: ignore[attr-defined]

    await alice.top_up_allocation(allotted.allocation_id, usd(1), "k")

    assert crossed(ports) == [("GET", GRANT), ("GET", ALLOCATION), ("POST", ALLOCATION + "/top-up")]


def test_only_the_remote_manager_is_made_ready_before_a_command(ports: Ports) -> None:
    """Self-hosted payments are as they were: Batteries' client has nothing to learn before a command,
    and the payments service awaits nothing for it."""
    assert isinstance(ports.manager, ReadyBeforeCommands)
    assert isinstance(ports.manager.for_actor(ports.tenant_id, "alice"), ReadyBeforeCommands)  # type: ignore[attr-defined]
    assert not isinstance(FakeAllocationManager(), ReadyBeforeCommands)
    assert not issubclass(BatteriesClient, ReadyBeforeCommands)
