"""#158: a status command stores the status the provider reports after it, not only the one it asked for.

``set_status`` sets the status at the provider, then stores it. Two of them at the same moment, a pause
and an activate, could reach the provider in one order and the store in the other, and the stored status
ended the opposite of the provider's. After its write, a status command now reads the allocation back at
the provider, and when the provider reports the other of the two statuses, it stores that and reads again.
The command that writes last has therefore read the provider after its write, and found what it wrote.

``tests/unit/test_payment_port_suite.py`` plays the two commands against the provider itself and through
a clearinghouse. Here: what the read-back does with each thing a provider can answer.
"""

import asyncio
import logging

import pytest

from livepeer_builder.contracts import ActorAllocation, ActorContext, Allowance
from livepeer_builder.errors import NotFound, ProviderError, ProviderUnavailable
from livepeer_builder.testing import FakeAllocationManager
from tests.conftest import EngineFactory
from tests.unit.test_actor_writes import Desk, allot_request, during


@pytest.fixture
async def desk(make_engine: EngineFactory, admin: ActorContext) -> Desk:
    return Desk(await make_engine(), admin)


async def allotted(desk: Desk) -> ActorAllocation:
    return await desk.payments.allot(desk.admin, allot_request())


async def stored(desk: Desk) -> ActorAllocation:
    return await desk.payments.get_actor(desk.admin, "default", "alice")


def since(manager: FakeAllocationManager) -> list[str]:
    """Forget the provider calls made so far, and answer the list the next ones are added to."""
    manager.calls.clear()
    return manager.calls


async def test_a_status_command_reads_the_provider_once_after_its_write(
    desk: Desk, manager: FakeAllocationManager
) -> None:
    """The ordinary command: the provider reports the status it was sent, and nothing more is done."""
    await allotted(desk)
    calls = since(manager)
    paused = await desk.pause()
    assert calls == ["set_allocation_status", "read_allocation"]
    assert paused.status == "paused" and await stored(desk) == paused
    active = await desk.activate()
    assert calls == ["set_allocation_status", "read_allocation"] * 2
    assert active.status == "active" and await stored(desk) == active


async def test_a_read_that_went_stale_before_the_command_ended_does_not_put_the_old_status_back(
    desk: Desk, manager: FakeAllocationManager
) -> None:
    """A read of the provider is only good until the next command. A pause that read ``paused`` and was
    then held up, while an activate ran to its end, must not store what it read: the write comes
    before the read, so the pause has nothing left to write, and the activate's status stays."""
    await allotted(desk)
    during(manager, "read_allocation", desk.activate)
    paused = await desk.pause()
    assert paused.status == "paused"  # what the provider reported when the pause read it
    assert manager.allocations[paused.allocation_id].status == "active"
    assert (await stored(desk)).status == "active"


@pytest.mark.parametrize(
    "failure",
    [ProviderUnavailable("Batteries unreachable"), ProviderError(500, "boom"), ProviderError(404, "gone")],
    ids=["unreachable", "500", "404"],
)
async def test_a_provider_that_cannot_be_read_back_leaves_the_status_the_command_set(
    desk: Desk, manager: FakeAllocationManager, failure: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    """The provider took the status, so the command is done and answers as it did before there was a
    read-back. The failed read is logged, and is not tried again."""
    await allotted(desk)
    manager.fail_once("read_allocation", failure)
    caplog.set_level(logging.WARNING)
    calls = since(manager)
    paused = await desk.pause()
    assert paused.status == "paused" and await stored(desk) == paused
    assert calls == ["set_allocation_status", "read_allocation"]
    [logged] = [entry for entry in caplog.records if hasattr(entry, "event")]
    assert logged.event == "payments.status_read_back_failed"  # type: ignore[attr-defined]
    assert (logged.actor_id, logged.allocation_id) == ("alice", paused.allocation_id)  # type: ignore[attr-defined]


@pytest.mark.parametrize("reported", ["exhausted", "revoked", "closed", ""])
async def test_only_the_two_statuses_a_command_sets_are_stored_from_the_read_back(
    desk: Desk, manager: FakeAllocationManager, monkeypatch: pytest.MonkeyPatch, reported: str
) -> None:
    """The provider also reports ``exhausted`` (the allocation has nothing left) and ``revoked``. Neither
    is a status command's to store: a revoke stores ``revoked`` and deletes the key, and the engine's
    ``exhausted`` is an allocation allotted with nothing. The status the command set stays."""
    allocation = await allotted(desk)
    read = manager.read_allocation

    async def reports(allocation_id: str) -> Allowance:
        return (await read(allocation_id)).model_copy(update={"status": reported})

    monkeypatch.setattr(manager, "read_allocation", reports)
    paused = await desk.pause()
    assert paused.status == "paused" and await stored(desk) == paused
    assert manager.calls.count("read_allocation") == 1 and allocation.status == "active"


async def test_a_provider_that_reports_the_other_status_each_time_is_read_three_times_at_most(
    desk: Desk, manager: FakeAllocationManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each read that reports the other status is stored and read again, since another command was at
    the provider meanwhile. Three reads, then the command answers with what it stored last."""
    await allotted(desk)
    read = manager.read_allocation

    async def the_other(allocation_id: str) -> Allowance:
        allowance = await read(allocation_id)
        held = (await stored(desk)).status
        return allowance.model_copy(update={"status": "active" if held == "paused" else "paused"})

    monkeypatch.setattr(manager, "read_allocation", the_other)
    calls = since(manager)
    answer = await desk.pause()
    assert calls.count("set_allocation_status") == 1 and calls.count("read_allocation") == 3
    assert answer == await stored(desk)
    assert answer.status == "active"  # paused, then the three reads' active, paused and active


async def test_a_status_command_overtaken_by_a_revoke_is_not_read_back(
    desk: Desk, manager: FakeAllocationManager
) -> None:
    """The actor stays revoked and the command answers 404, as before: there is nothing to settle."""
    await allotted(desk)
    during(manager, "set_allocation_status", desk.revoke)
    calls = since(manager)
    with pytest.raises(NotFound):
        await desk.pause()
    assert "read_allocation" not in calls and (await stored(desk)).status == "revoked"


async def test_three_status_commands_at_the_same_moment_still_end_on_the_providers_status(
    desk: Desk, manager: FakeAllocationManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pause is held up after the provider took it. An activate overtakes it and is held up after its
    own read, and a second pause then runs to its end. The provider ended ``paused``. The first pause
    writes ``paused`` and the activate, which read ``active`` before the second pause, has stored
    nothing since: every record write is followed by a read, and the last one found what it wrote."""
    allocation = await allotted(desk)
    taken, first_may_end, read_once, activate_may_end = (asyncio.Event() for _ in range(4))
    set_status, read = manager.set_allocation_status, manager.read_allocation

    async def held_up(allocation_id: str, status: str) -> None:
        await set_status(allocation_id, status)
        if not taken.is_set():
            taken.set()
            await first_may_end.wait()

    async def held_after(allocation_id: str) -> Allowance:
        allowance = await read(allocation_id)
        if not read_once.is_set():
            read_once.set()
            await activate_may_end.wait()
        return allowance

    monkeypatch.setattr(manager, "set_allocation_status", held_up)
    monkeypatch.setattr(manager, "read_allocation", held_after)
    first = asyncio.ensure_future(desk.pause())
    await taken.wait()
    activate = asyncio.ensure_future(desk.activate())
    await asyncio.wait_for(read_once.wait(), 5)
    await desk.pause()
    first_may_end.set()
    await first
    activate_may_end.set()
    await activate
    assert manager.allocations[allocation.allocation_id].status == "paused"
    assert (await stored(desk)).status == "paused"
