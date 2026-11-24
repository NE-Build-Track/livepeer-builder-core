"""A request in flight when ``serve()`` is forced to exit (#167, and the review of #181).

A forced exit does not wait for its callers: a cancelled ``serve()``, a second Ctrl-C, a listener that
ended by itself. Each listener hangs up on its open connections. That alone leaves the handler running:
uvicorn's forced exit does not wait for its request tasks, and one that awaits a runner is not told its
caller is gone. ``serve()`` then stopped the engine under it, and the job it was running stayed
``running`` in a database the engine had closed.

So a request still running when its server has ended is cancelled, and ``serve()`` waits for it before
it stops the engine. A paid job cut off that way ends as any cancelled job does: ``uncertain``, with its
paid attempt kept.

Each test serves on loopback ports the system picked, over a database in a file, which is read again once
the engine has closed it.
"""

import asyncio
import signal
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

from livepeer_builder import EngineApi, EngineSupply, generate_secret_key
from livepeer_builder.contracts import ActorContext, Job, JobRequest, Runner, RunnerReply
from livepeer_builder.http.server import serve
from livepeer_builder.protocols import OnSigned
from livepeer_builder.store import SqlStore
from livepeer_builder.testing import FakeAllocationManager, FakeUsageSource, ScriptedTransport, runner
from livepeer_builder.testing import serve as serve_discovery
from tests.conftest import allot
from tests.unit.test_listener_serving import (  # noqa: F401 - the two fixtures are used by name
    both_listen,
    free_port,
    lifespan_tasks,
    listening,
    signals,
    the_root_logger_as_it_was,
)
from tests.unit.test_service_mode_example import settings

ADMIN = ActorContext(actor_id="operator", credential_kind="clearinghouse_admin")
ALICE = ActorContext(actor_id="alice", permissions=frozenset({"jobs.run", "jobs.read"}))


@dataclass
class PaysThenWaits(ScriptedTransport):
    """A runner that takes the payment and never answers: the paid request stays in flight."""

    at_runner: asyncio.Event = field(default_factory=asyncio.Event)

    async def call(
        self,
        runner: Runner,
        request: JobRequest,
        credential: str | None,
        on_signed: OnSigned | None = None,
    ) -> RunnerReply:
        assert on_signed is not None
        await on_signed("m-1")
        self.at_runner.set()
        await asyncio.Event().wait()
        raise AssertionError("never answers")


def request_tasks() -> "list[asyncio.Task[Any]]":
    """uvicorn's tasks for requests still running: each runs ``RequestResponseCycle.run_asgi``."""
    return [
        task
        for task in asyncio.all_tasks()
        if "run_asgi" in getattr(task.get_coro(), "__qualname__", "") and not task.done()
    ]


class Served:
    """``serve()`` in a task, with one paid job a caller asked for over HTTP held at the runner."""

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
        self.asking: asyncio.Task[httpx.Response] | None = None

    async def a_job_at_the_runner(self) -> None:
        await both_listen(self.serving, *self.ports)
        engine: Any = self.engines[0]
        await allot(engine, ALICE)
        key = await engine.keys.issue(ADMIN, kind="actor", actor_id="alice", operation_ref="key:alice")
        self.asking = asyncio.create_task(self._ask(key.token))
        await asyncio.wait_for(self.transport.at_runner.wait(), timeout=5)
        assert len(request_tasks()) == 1

    async def _ask(self, token: str) -> httpx.Response:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{self.ports[0]}", timeout=30) as client:
            return await client.post(
                "/v1/jobs",
                json={"app": "example/echo", "payload": {"n": 1}},
                headers={"Authorization": f"Bearer {token}"},
            )

    async def ended(self) -> bool:
        """Whether ``serve()`` returned within ten seconds. A failed test leaves nothing running."""
        done, _ = await asyncio.wait([self.serving], timeout=10)
        if not done:
            self.serving.cancel()
            await asyncio.wait([self.serving], timeout=10)
        left = request_tasks()
        self.still_running, self.lifespans_waiting = len(left), len(lifespan_tasks())
        for task in left:
            task.cancel()
        if left:
            await asyncio.wait(left, timeout=5)
        if self.asking is not None:
            await asyncio.wait([self.asking], timeout=5)
        await self._discovery.aclose()
        return bool(done)

    async def stored(self) -> list[tuple[str, list[tuple[str, bool]]]]:
        """Every job as the database holds it, read once the engine has closed its own connection."""
        store = await SqlStore.connect(self.database_url, migrate=False)
        try:
            jobs: list[Job] = []
            for state in ("running", "uncertain", "failed", "succeeded"):
                jobs += await store.jobs_in_state(state)  # type: ignore[arg-type]
        finally:
            await store.close()
        return [(job.state, [(a.outcome, a.payment_sent) for a in job.attempts]) for job in jobs]


UNCERTAIN = [("uncertain", [("in_flight", True)])]


@pytest.mark.parametrize("listeners", [1, 2], ids=["one listener", "two listeners"])
async def test_a_cancelled_serve_ends_the_request_in_flight_before_it_stops_the_engine(
    tmp_path: Path,
    signals: dict[int, object],  # noqa: F811 - the fixture
    listeners: int,
) -> None:
    ports = [free_port() for _ in range(listeners)]
    served = Served(f"sqlite+aiosqlite:///{tmp_path / 'engine.db'}", ports)
    try:
        await served.a_job_at_the_runner()
        served.serving.cancel()
    finally:
        ended = await served.ended()
    assert ended, "serve() had not returned 10 s after it was cancelled"
    assert served.serving.cancelled()  # the caller sees its own cancellation
    assert served.still_running == 0, "a request was still running after serve() returned"
    assert served.lifespans_waiting == 0, "the app's lifespan was still waiting after serve() returned"
    assert not any(listening(port) for port in ports)
    # the job's end was written before the engine closed its database: paid, and its outcome unknown
    assert await served.stored() == UNCERTAIN
    assert served.asking is not None and served.asking.exception() is not None  # the caller was hung up on


async def test_a_second_interrupt_ends_the_request_in_flight_before_it_stops_the_engine(
    tmp_path: Path,
    signals: dict[int, object],  # noqa: F811 - the fixture
) -> None:
    """With two listeners. The first Ctrl-C waits for the request, and the second does not."""
    ports = [free_port(), free_port()]
    served = Served(f"sqlite+aiosqlite:///{tmp_path / 'engine.db'}", ports)
    try:
        await served.a_job_at_the_runner()
        signal.raise_signal(signal.SIGINT)
        await asyncio.sleep(0.3)
        assert not served.serving.done() and len(request_tasks()) == 1  # waited for
        signal.raise_signal(signal.SIGINT)
    finally:
        ended = await served.ended()
    assert ended, "serve() had not returned 10 s after the second interrupt"
    assert served.serving.exception() is None
    assert served.still_running == 0, "a request was still running after serve() returned"
    assert served.lifespans_waiting == 0, "an app's lifespan was still waiting after serve() returned"
    assert await served.stored() == UNCERTAIN
