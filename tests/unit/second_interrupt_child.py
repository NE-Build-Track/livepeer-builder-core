"""A child process for tests/unit/test_forced_exit_requests.py: ``asyncio.run(serve(...))``, as ``lpb
serve`` runs it, with one listener and a paid job held at the runner, sent SIGINT twice.

The handler ``asyncio.run`` installs cannot be had inside the test run: uvicorn gives a second Ctrl-C
back to it once the server has ended, and it raises ``KeyboardInterrupt`` where the server was awaited.
The last line printed is what the parent reads: how ``asyncio.run`` ended, how many requests were still
running when the engine was first asked to stop, and every job as the database holds it afterwards.

    python -m tests.unit.second_interrupt_child <database url> <port>
"""

import asyncio
import json
import os
import signal
import sys
from typing import Any

import httpx

from livepeer_builder import BuilderEngine, EngineSupply, generate_secret_key
from livepeer_builder.http.server import serve
from livepeer_builder.testing import FakeAllocationManager, FakeUsageSource, runner
from livepeer_builder.testing import serve as serve_discovery
from tests.conftest import allot
from tests.unit.test_forced_exit_requests import ADMIN, ALICE, PaysThenWaits, request_tasks, stored
from tests.unit.test_service_mode_example import settings


async def interrupt_twice(engine: Any, transport: PaysThenWaits, port: int) -> None:
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=30) as client:
        for _ in range(200):
            try:
                await client.get("/livez")
                break
            except httpx.HTTPError:
                await asyncio.sleep(0.05)
        await allot(engine, ALICE)
        key = await engine.keys.issue(ADMIN, kind="actor", actor_id="alice", operation_ref="key:alice")
        asking = asyncio.create_task(
            client.post(
                "/v1/jobs",
                json={"app": "example/echo", "payload": {"n": 1}},
                headers={"Authorization": f"Bearer {key.token}"},
            )
        )
        await transport.at_runner.wait()
        os.kill(os.getpid(), signal.SIGINT)
        await asyncio.sleep(0.5)  # the first Ctrl-C waits for the request in flight
        os.kill(os.getpid(), signal.SIGINT)
        await asyncio.wait([asking])


def main(database_url: str, port: int) -> dict[str, Any]:
    transport = PaysThenWaits()
    provider = FakeAllocationManager()
    provider.add_grant("default")
    install, _ = serve_discovery((runner(),))
    driving: list[asyncio.Task[None]] = []
    running_at_stop: list[int] = []
    stop = BuilderEngine.stop

    async def noted_stop(self: BuilderEngine) -> None:
        running_at_stop.append(len(request_tasks()))
        await stop(self)

    BuilderEngine.stop = noted_stop  # type: ignore[method-assign]
    ended = "returned"
    try:
        asyncio.run(
            serve(
                settings(database_url=database_url, http_port=port, secret_key=generate_secret_key()),
                supply=EngineSupply(
                    transport=transport,
                    allocation_manager=provider,
                    grants_manager=provider,
                    usage_source=FakeUsageSource(),
                ),
                decorate=install,
                extend=lambda _, engine: driving.append(
                    asyncio.get_running_loop().create_task(interrupt_twice(engine, transport, port))
                ),
            )
        )
    except KeyboardInterrupt:
        ended = "KeyboardInterrupt"
    return {
        "ended": ended,
        "requests_running_when_the_engine_stopped": running_at_stop[0],
        "jobs": asyncio.run(stored(database_url)),
    }


if __name__ == "__main__":
    print(json.dumps(main(sys.argv[1], int(sys.argv[2]))))  # noqa: T201 - what the parent reads
