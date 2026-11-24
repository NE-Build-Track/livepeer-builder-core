"""ISS-26 when ``serve()`` is forced to exit (#167, and the review of #181).

The async worker is the engine's: ``BuilderEngine.start()`` starts its loop and ``stop()`` drains it
(``AsyncJobWorker.drain``). ``serve()`` starts and stops the engine, and some of its exits do not wait
for callers: a cancelled ``serve()``, with one listener or two, and a second Ctrl-C with two. None of
them skips the engine's stop, which ``serve()`` runs on every way out. So a job the worker is running is
still ended in order: it is given ``LPB_ASYNC_DRAIN_TIMEOUT_S``, then cancelled and its end written,
before the engine closes its database, and when ``serve()`` has returned no task of the worker or of the
engine is left.

What a cancel does to the listeners themselves is tests/unit/test_listener_serving.py's subject: one
listener or two, each is closed and hangs up on its callers. Here every port is asserted free again.

Each test serves on loopback ports the system picked, over a database in a file, which is read again
once the engine has closed it.
"""

import asyncio
import signal
from pathlib import Path
from typing import Any, cast

import pytest

from livepeer_builder import BuilderEngine, EngineApi, EngineSupply, generate_secret_key
from livepeer_builder.contracts import ActorContext, Job, JobRequest
from livepeer_builder.http.server import serve
from livepeer_builder.store import SqlStore
from livepeer_builder.testing import FakeAllocationManager, FakeUsageSource, runner
from livepeer_builder.testing import serve as serve_discovery
from tests.conftest import allot
from tests.store.test_async_restart import PaysThenWaits, until
from tests.unit.test_listener_serving import (  # noqa: F401 - the two fixtures are used by name
    both_listen,
    free_port,
    listening,
    signals,
    the_root_logger_as_it_was,
)
from tests.unit.test_service_mode_example import settings

ALICE = ActorContext(actor_id="alice", permissions=frozenset({"jobs.run", "jobs.read", "costs.read"}))
QUEUED = JobRequest(app="example/echo", mode="async", payload={"n": 1})


def engine_tasks() -> list[str]:
    """The tasks the engine, its worker and ``serve()`` name, while one is still running."""
    return sorted(task.get_name() for task in asyncio.all_tasks() if task.get_name().startswith("lpb-"))


class Served:
    """``serve()`` in a task, over an engine whose one runner takes the payment and never answers."""

    def __init__(self, database_url: str, ports: list[int]) -> None:
        self.database_url, self.ports = database_url, ports
        self.transport = PaysThenWaits()
        self.engines: list[EngineApi] = []
        provider = FakeAllocationManager()
        provider.add_grant("default")
        install, self._discovery = serve_discovery((runner(),))
        private = {"http_private_port": ports[1]} if len(ports) == 2 else {}
        self.serving = asyncio.create_task(
            serve(
                settings(
                    database_url=database_url,
                    http_port=ports[0],
                    secret_key=generate_secret_key(),
                    async_workers=1,
                    async_poll_s=0.01,
                    async_drain_timeout_s=0.05,  # the runner never answers: the drain time passes
                    **private,
                ),
                supply=EngineSupply(
                    transport=self.transport,
                    allocation_manager=provider,
                    grants_manager=provider,
                    usage_source=FakeUsageSource(),
                ),
                decorate=install,
                extend=lambda _, engine: self.engines.append(engine),
            )
        )

    @property
    def engine(self) -> BuilderEngine:
        return cast(BuilderEngine, self.engines[0])

    async def a_job_at_the_runner(self) -> Job:
        """An async job the worker claimed and paid for, held at the runner."""
        await both_listen(self.serving, *self.ports)
        await allot(self.engine, ALICE)
        job = await self.engine.jobs.submit(ALICE, QUEUED)
        await until(lambda: len(self.transport.calls) == 1, "the job is at the runner")
        assert "lpb-async-job" in engine_tasks() and "lpb-async-jobs" in engine_tasks()
        return job

    async def ended(self) -> bool:
        """Whether ``serve()`` returned within ten seconds. A failed test leaves nothing running."""
        done, _ = await asyncio.wait([self.serving], timeout=10)
        if not done:
            self.transport.release.set()
            self.serving.cancel()
            await asyncio.wait([self.serving], timeout=10)
        await self._discovery.aclose()
        return bool(done)

    async def stored(self, job: Job) -> Job:
        """The job as the database holds it, read once the engine has closed its own connection."""
        store = await SqlStore.connect(self.database_url, migrate=False)
        try:
            found = await store.get_job(job.id)
        finally:
            await store.close()
        assert found is not None
        return found


def stopped_in_flight(job: Job) -> tuple[Any, ...]:
    """What a paid job cut off at the runner ends as: ``uncertain``, with its one paid attempt kept."""
    paid = job.failure is not None and job.failure.payment_sent
    return (job.state, paid, job.result, [(a.outcome, a.payment_sent) for a in job.attempts])


UNCERTAIN = ("uncertain", True, None, [("in_flight", True)])


@pytest.mark.parametrize("listeners", [1, 2], ids=["one listener", "two listeners"])
async def test_a_cancelled_serve_ends_a_running_async_job_and_leaves_no_task_of_the_engine(
    tmp_path: Path,
    signals: dict[int, object],  # noqa: F811 - the fixture
    listeners: int,
) -> None:
    """Cancelling is how an embedder stops ``serve()``, and no listener then waits for a request in
    flight. The engine's stop is not skipped: the worker's job gets its drain time, is cancelled, and its
    end is in the database the engine then closes."""
    ports = [free_port() for _ in range(listeners)]
    served = Served(f"sqlite+aiosqlite:///{tmp_path / 'engine.db'}", ports)
    try:
        job = await served.a_job_at_the_runner()
        served.serving.cancel()
    finally:
        ended = await served.ended()
    assert ended, "serve() had not returned 10 s after it was cancelled"
    assert served.serving.cancelled()  # the caller sees its own cancellation
    assert engine_tasks() == []  # no loop, no job and no listener outlives serve()
    assert served.engine.jobs.worker._running == set()
    assert not any(listening(port) for port in ports)
    assert stopped_in_flight(await served.stored(job)) == UNCERTAIN
    assert len(served.transport.calls) == 1  # never run again
    with pytest.raises(RuntimeError, match="engine is stopped"):
        await served.engine.start()


async def test_a_second_interrupt_ends_a_running_async_job_and_leaves_no_task_of_the_engine(
    tmp_path: Path,
    signals: dict[int, object],  # noqa: F811 - the fixture
) -> None:
    """With two listeners the second Ctrl-C forces both to exit. ``serve()`` then stops the engine as it
    does after any exit, and returns: the job is ended, not left ``running`` for the next start."""
    ports = [free_port(), free_port()]
    served = Served(f"sqlite+aiosqlite:///{tmp_path / 'engine.db'}", ports)
    try:
        job = await served.a_job_at_the_runner()
        signal.raise_signal(signal.SIGINT)
        signal.raise_signal(signal.SIGINT)
    finally:
        ended = await served.ended()
    assert ended, "serve() had not returned 10 s after the second interrupt"
    assert served.serving.exception() is None
    assert engine_tasks() == []
    assert served.engine.jobs.worker._running == set()
    assert not any(listening(port) for port in ports)
    assert stopped_in_flight(await served.stored(job)) == UNCERTAIN
    assert {sig: signal.getsignal(sig) for sig in signals} == signals  # the process's own are back
