"""Serving two listeners from one engine in one process (D84, #167).

``LPB_HTTP_PRIVATE_PORT`` turns the private listener on. ``serve`` then runs two servers over one
engine: the public listener on ``LPB_HTTP_HOST`` and ``LPB_HTTP_PORT``, the private one on
``LPB_HTTP_PRIVATE_HOST`` (loopback unless set) and that port. The engine starts once and stops once, an
address that cannot be bound stops both and the engine, and one listener never serves without the other.
Without the setting nothing changes: one listener, as tests/unit/test_service_mode_example.py and
tests/unit/test_cli.py pin.

The tests that serve bind loopback on a port the system picked, and leave nothing listening.
"""

import asyncio
import contextlib
import logging
import signal
import socket
from collections.abc import Awaitable, Callable, Iterator
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from pydantic import ValidationError

from livepeer_builder import BuilderEngine, EngineApi, EngineSettings, EngineSupply
from livepeer_builder.http.server import _bound, _listener_server, _serve_together, build_app, serve
from livepeer_builder.store import SqlStore
from tests.unit.test_service_mode_example import settings

TENANTS = "/v1/clearinghouse/tenants"


def free_port() -> int:
    """A loopback port nothing listens on: the system picks it, and it is closed again at once."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def listening(port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(1)
        return probe.connect_ex(("127.0.0.1", port)) == 0


@pytest.fixture(autouse=True)
def the_root_logger_as_it_was() -> Iterator[None]:
    """``serve`` configures logging, as an entrypoint does. The test run's own handlers come back."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


@pytest.fixture
def signals() -> Iterator[dict[int, object]]:
    """Handlers that do nothing, in place of the test run's own, while a test sends the process a
    signal: a signal that reaches the handler ``serve`` found does not end the run. The value is what
    ``serve`` must put back."""
    quiet = {sig: (lambda *_: None) for sig in (signal.SIGINT, signal.SIGTERM)}
    before = {sig: signal.signal(sig, handler) for sig, handler in quiet.items()}
    yield dict(quiet)
    for sig, handler in before.items():
        signal.signal(sig, handler)


def test_one_listener_is_the_default_and_the_private_one_listens_on_loopback() -> None:
    assert settings().http_private_port is None  # one listener: nothing changes for a deployment
    assert settings().http_private_host == "127.0.0.1"
    env = {
        "LPB_DISCOVERY_URL": "http://127.0.0.1:9/discovery",
        "LPB_HTTP_HOST": "0.0.0.0",  # noqa: S104 - a value under test; nothing binds it
        "LPB_HTTP_PRIVATE_PORT": "9090",
    }
    two = EngineSettings.from_env(env)
    assert (two.http_host, two.http_port) == ("0.0.0.0", 8080)  # noqa: S104
    assert (two.http_private_host, two.http_private_port) == ("127.0.0.1", 9090)
    moved = EngineSettings.from_env({**env, "LPB_HTTP_PRIVATE_HOST": "10.0.0.5"})
    assert (moved.http_private_host, moved.http_private_port) == ("10.0.0.5", 9090)


def test_the_two_listeners_cannot_share_an_address() -> None:
    with pytest.raises(ValidationError, match="LPB_HTTP_PRIVATE_PORT"):
        settings(http_host="127.0.0.1", http_port=8080, http_private_port=8080)
    # the same port on another address is two listeners
    assert settings(http_host="10.0.0.5", http_port=8080, http_private_port=8080).http_private_port == 8080
    with pytest.raises(ValidationError):
        settings(http_private_port=0)


async def test_build_app_builds_one_listeners_app_and_says_so_when_two_are_configured(
    store: SqlStore,
) -> None:
    """``build_app`` returns one app for a caller's own server. With a private listener configured it
    would have to pick one, so it refuses before it builds an engine, and names what does both."""
    built: list[EngineApi] = []
    with pytest.raises(ValueError, match=r"LPB_HTTP_PRIVATE_PORT.*create_app\(engine, listener="):
        await build_app(
            settings(http_private_port=free_port()),
            supply=EngineSupply(store=store),
            extend=lambda _, engine: built.append(engine),
        )
    assert not built


async def answers(port: int, path: str = "/livez", headers: dict[str, str] | None = None) -> httpx.Response:
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=5) as client:
        return await client.get(path, headers=headers)


async def both_listen(serving: "asyncio.Task[None]", *ports: int) -> None:
    """Wait until every port answers, or fail with what ``serve`` raised."""
    for _ in range(200):
        if serving.done():
            serving.result()
            pytest.fail("serve returned before its listeners answered")
        if all(listening(port) for port in ports):
            return
        await asyncio.sleep(0.02)
    pytest.fail("the listeners did not answer within 4 s")


async def test_serve_runs_a_public_and_a_private_listener_over_one_engine(
    store: SqlStore, monkeypatch: pytest.MonkeyPatch, signals: dict[int, object]
) -> None:
    public, private = free_port(), free_port()
    extended: list[tuple[str, EngineApi]] = []
    lifecycle: list[str] = []
    start, stop = BuilderEngine.start, BuilderEngine.stop

    async def counted_start(self: BuilderEngine) -> None:
        lifecycle.append("start")
        await start(self)

    async def counted_stop(self: BuilderEngine) -> None:
        lifecycle.append("stop")
        await stop(self)

    monkeypatch.setattr(BuilderEngine, "start", counted_start)
    monkeypatch.setattr(BuilderEngine, "stop", counted_stop)

    def extend(app: FastAPI, engine: EngineApi) -> None:
        extended.append((app.state.listener.name, engine))

    serving = asyncio.create_task(
        serve(
            settings(http_port=public, http_private_port=private),
            supply=EngineSupply(store=store),
            extend=extend,
        )
    )
    try:
        await both_listen(serving, public, private)
        # extend ran once for each listener's app, with the one engine
        assert [name for name, _ in extended] == ["public", "private"]
        engine = extended[0][1]
        assert extended[1][1] is engine and engine.keys is not None
        admin = {"Authorization": f"Bearer {(await engine.keys.bootstrap_admin()).token}"}
        for port in (public, private):  # health on both, from the one engine, which started
            assert (await answers(port)).json() == {"status": "ok"}
            assert (await answers(port, "/readyz")).json()["ready"] is True
        # the private group is on the private listener only, and the admin key works there only
        assert (await answers(private, "/v1/keys", admin)).status_code == 200
        assert (await answers(public, "/v1/keys", admin)).status_code == 403
        assert (await answers(public, "/v1/keys")).status_code == 401
        private_paths = (await answers(private, "/openapi.json")).json()["paths"]
        public_paths = (await answers(public, "/openapi.json")).json()["paths"]
        assert "/v1/clearinghouse/tenants/{tenant_id}/grant/top-up" in private_paths
        assert "/v1/clearinghouse/tenants/{tenant_id}/grant/top-up" not in public_paths
        assert lifecycle == ["start"]
    finally:
        signal.raise_signal(signal.SIGTERM)  # as a process manager stops the service
        await asyncio.wait_for(serving, timeout=10)
    assert lifecycle == ["start", "stop"]  # once each, for two listeners
    assert not listening(public) and not listening(private)
    assert {sig: signal.getsignal(sig) for sig in signals} == signals  # the process's own are back
    with pytest.raises(RuntimeError, match="engine is stopped"):
        await start(engine)  # type: ignore[arg-type]


@pytest.mark.parametrize("taken", ["public", "private"])
async def test_an_address_that_cannot_be_bound_stops_both_listeners_and_the_engine(
    store: SqlStore, monkeypatch: pytest.MonkeyPatch, taken: str
) -> None:
    """A taken address is logged and the process exits, with the status one listener exits with. Both
    addresses are bound before the engine starts, so it never starts, and the other one is free again."""
    ports = {"public": free_port(), "private": free_port()}
    started: list[str] = []
    built: list[EngineApi] = []

    async def never(self: BuilderEngine) -> None:
        started.append("start")

    with socket.socket() as holder:
        holder.bind(("127.0.0.1", ports[taken]))
        holder.listen()
        with pytest.raises(SystemExit) as one_listener:
            await serve(settings(http_port=ports[taken]), supply=EngineSupply(store=store))
        monkeypatch.setattr(BuilderEngine, "start", never)
        with pytest.raises(SystemExit) as exited:
            await serve(
                settings(http_port=ports["public"], http_private_port=ports["private"]),
                supply=EngineSupply(store=store),
                extend=lambda _, engine: built.append(engine),
            )
        assert exited.value.code == one_listener.value.code != 0
        other = ports["private" if taken == "public" else "public"]
        with socket.socket() as probe:  # nothing holds the address that was bound first
            probe.bind(("127.0.0.1", other))
    assert not started
    monkeypatch.undo()
    with pytest.raises(RuntimeError, match="engine is stopped"):
        await built[0].start()


Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]
App = Callable[[dict[str, Any], Receive, Send], Awaitable[None]]


async def hello(scope: dict[str, Any], receive: Receive, send: Send) -> None:
    """An ASGI app that takes no lifespan, so its server starts, and answers nothing else."""
    if scope["type"] == "lifespan":
        raise RuntimeError("no lifespan")


async def failing_startup(scope: dict[str, Any], receive: Receive, send: Send) -> None:
    """An ASGI app whose startup fails, as an extension's own lifespan can."""
    assert scope["type"] == "lifespan"
    await receive()
    await send({"type": "lifespan.startup.failed", "message": "an extension could not start"})


def two_servers(first: App = hello, second: App = hello) -> tuple[list[uvicorn.Server], list[int]]:
    """Two servers as ``serve`` builds them, each on a loopback port the system picked."""
    ports = [free_port(), free_port()]
    servers = [
        _listener_server(app, "127.0.0.1", port)  # type: ignore[arg-type]  # an ASGI app, as FastAPI is
        for app, port in zip((first, second), ports, strict=True)
    ]
    return servers, ports


async def started(servers: list[uvicorn.Server]) -> None:
    for _ in range(200):
        if all(getattr(server, "started", False) for server in servers):
            return
        await asyncio.sleep(0.02)
    pytest.fail("the servers did not start within 4 s")


async def test_one_listener_that_ends_takes_the_other_with_it(signals: dict[int, object]) -> None:
    """Neither listener serves without the other: a private listener that is gone would leave a
    clearinghouse that cannot be administered, and a public one that is gone serves no tenant."""
    servers, ports = two_servers()
    with _bound(servers) as sockets:
        serving = asyncio.create_task(_serve_together(servers, sockets))
        await started(servers)
        assert all(listening(port) for port in ports)
        servers[1].should_exit = True
        await asyncio.wait_for(serving, timeout=10)
    assert not any(listening(port) for port in ports)
    assert {sig: signal.getsignal(sig) for sig in signals} == signals


class Held:
    """An ASGI app that takes a request and answers nothing until its caller hangs up, as a stream or a
    slow job holds a connection open. ``arrived`` is set once a request is in the app, and
    ``disconnected`` once the app is told its caller is gone."""

    def __init__(self) -> None:
        self.arrived = asyncio.Event()
        self.disconnected = asyncio.Event()

    async def __call__(self, scope: dict[str, Any], receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            raise RuntimeError("no lifespan")
        self.arrived.set()
        while (await receive())["type"] != "http.disconnect":
            pass
        self.disconnected.set()


async def held_open(port: int, app: Held) -> asyncio.StreamWriter:
    """A real connection to the listener on ``port``, with a request the app has taken and not answered.
    The caller closes it."""
    _, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), timeout=5)
    writer.write(b"GET /held HTTP/1.1\r\nHost: listener.test\r\n\r\n")
    await writer.drain()
    await asyncio.wait_for(app.arrived.wait(), timeout=5)
    return writer


async def hung_up(writer: asyncio.StreamWriter) -> None:
    writer.close()
    with contextlib.suppress(OSError):
        await asyncio.wait_for(writer.wait_closed(), timeout=5)


async def test_a_listener_that_ends_does_not_leave_the_other_waiting_for_its_open_connections(
    signals: dict[int, object],
) -> None:
    """A listener that ended for a reason of its own leaves a process that must end, so a supervisor can
    start it again. The other listener does not wait for its open connections: uvicorn waits for them
    without limit, and the process would stay up with one listener gone and the other accepting nothing."""
    held = Held()
    servers, ports = two_servers(first=held)
    with _bound(servers) as sockets:
        serving = asyncio.create_task(_serve_together(servers, sockets))
        await started(servers)
        writer = await held_open(ports[0], held)
        try:
            servers[1].should_exit = True  # the private listener ends by itself; no signal was sent
            await asyncio.wait_for(serving, timeout=10)
            # the listener hung up on its caller, and the request in flight was told so
            await asyncio.wait_for(held.disconnected.wait(), timeout=5)
        finally:
            await hung_up(writer)
    assert [server.force_exit for server in servers] == [True, True]
    assert not any(listening(port) for port in ports)
    assert {sig: signal.getsignal(sig) for sig in signals} == signals


async def test_a_first_signal_still_waits_for_an_open_connection(signals: dict[int, object]) -> None:
    """Stopping the service in order is unchanged: after one SIGTERM both listeners stop accepting, and
    the one with a request in flight ends when that request does, although the other has ended."""
    held = Held()
    servers, ports = two_servers(first=held)
    with _bound(servers) as sockets:
        serving = asyncio.create_task(_serve_together(servers, sockets))
        await started(servers)
        writer = await held_open(ports[0], held)
        try:
            signal.raise_signal(signal.SIGTERM)
            await nothing_listens(ports)  # both stopped accepting: the idle listener has ended
            done, _ = await asyncio.wait([serving], timeout=1)
            assert not done and not held.disconnected.is_set()  # the request in flight is waited for
            assert [(server.should_exit, server.force_exit) for server in servers] == [(True, False)] * 2
        finally:
            await hung_up(writer)
        await asyncio.wait_for(serving, timeout=10)
    assert [server.force_exit for server in servers] == [False, False]
    assert {sig: signal.getsignal(sig) for sig in signals} == signals


async def nothing_listens(ports: list[int]) -> None:
    for _ in range(200):
        if not any(listening(port) for port in ports):
            return
        await asyncio.sleep(0.02)
    pytest.fail("the listeners still accepted connections after 4 s")


async def test_a_listener_whose_app_cannot_start_ends_both_and_exits_as_one_listener_does(
    signals: dict[int, object],
) -> None:
    """uvicorn exits with status 3 when an app's startup fails. With two listeners the other one is
    ended first, in order, and the exit is raised from ``serve``, where the engine is then stopped."""
    servers, ports = two_servers(second=failing_startup)
    with _bound(servers) as sockets, pytest.raises(SystemExit) as exited:
        await asyncio.wait_for(_serve_together(servers, sockets), timeout=10)
    assert exited.value.code == 3
    assert not any(listening(port) for port in ports)
    assert {sig: signal.getsignal(sig) for sig in signals} == signals


async def test_a_cancelled_serve_ends_both_listeners(signals: dict[int, object]) -> None:
    """An embedder that runs ``serve`` in a task stops it by cancelling the task."""
    servers, ports = two_servers()
    with _bound(servers) as sockets:
        serving = asyncio.create_task(_serve_together(servers, sockets))
        await started(servers)
        serving.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(serving, timeout=10)
    assert not any(listening(port) for port in ports)
    assert {sig: signal.getsignal(sig) for sig in signals} == signals


async def test_a_second_interrupt_does_not_wait_for_open_connections(signals: dict[int, object]) -> None:
    """As uvicorn does for one listener: the first Ctrl-C asks both servers to end, the second forces it."""
    servers, _ = two_servers()
    with _bound(servers) as sockets:
        serving = asyncio.create_task(_serve_together(servers, sockets))
        await started(servers)
        signal.raise_signal(signal.SIGINT)
        assert [(server.should_exit, server.force_exit) for server in servers] == [(True, False)] * 2
        signal.raise_signal(signal.SIGINT)
        assert [(server.should_exit, server.force_exit) for server in servers] == [(True, True)] * 2
        await asyncio.wait_for(serving, timeout=10)


@pytest.mark.parametrize("held_on", ["one listener", "both listeners"])
async def test_a_second_interrupt_ends_serving_while_a_request_is_held_open(
    signals: dict[int, object], held_on: str
) -> None:
    """The forced exit must hang up on open connections itself (John, #181 review): from Python 3.12 the
    listening server's ``wait_closed()`` returns only once every connection is gone, so ``force_exit``
    alone left ``serve`` waiting for a request that never ends. With a request held on both listeners,
    neither server ends after the first Ctrl-C, and the second must still end them."""
    apps = [Held(), Held()] if held_on == "both listeners" else [Held()]
    servers, ports = two_servers(*apps)
    with _bound(servers) as sockets:
        serving = asyncio.create_task(_serve_together(servers, sockets))
        await started(servers)
        writers = [await held_open(port, app) for port, app in zip(ports, apps, strict=False)]
        try:
            signal.raise_signal(signal.SIGINT)
            await nothing_listens(ports)  # both stopped accepting, and the held requests are waited for
            assert not serving.done() and not any(app.disconnected.is_set() for app in apps)
            signal.raise_signal(signal.SIGINT)
            ended, _ = await asyncio.wait([serving], timeout=10)
            told = [app.disconnected.is_set() for app in apps]
        finally:  # a serve() left waiting ends once its callers hang up, so a failure is an assertion
            for writer in writers:
                await hung_up(writer)
            await asyncio.wait_for(serving, timeout=10)
    assert ended, "serve() still waited for a held request 10 s after the second interrupt"
    assert all(told)  # each request in flight was told its caller is gone
    assert [server.force_exit for server in servers] == [True, True]
    assert {sig: signal.getsignal(sig) for sig in signals} == signals


def listener_tasks() -> "list[asyncio.Task[Any]]":
    """The tasks ``_serve_together`` runs its servers in, while one is still running or pending."""
    return [task for task in asyncio.all_tasks() if task.get_name().startswith("lpb-listener-")]


async def gone(tasks: "list[asyncio.Task[Any]]") -> None:
    """Nothing a failed test left behind outlives it."""
    if tasks:
        await asyncio.wait(tasks, timeout=10)


@pytest.mark.parametrize("held_on", ["one listener", "both listeners"])
async def test_a_cancelled_serve_ends_while_a_request_is_held_open(
    signals: dict[int, object], held_on: str
) -> None:
    """Cancelling is how an embedder stops ``serve``, and off the main thread it is the only way. It is
    a forced exit, as it is for one listener, where uvicorn's ``serve`` raises at once (#181 review): the
    listeners hang up on their open connections, so a request that never ends does not hold the task."""
    apps = [Held(), Held()] if held_on == "both listeners" else [Held()]
    servers, ports = two_servers(*apps)
    with _bound(servers) as sockets:
        serving = asyncio.create_task(_serve_together(servers, sockets))
        await started(servers)
        writers = [await held_open(port, app) for port, app in zip(ports, apps, strict=False)]
        try:
            serving.cancel()
            ended, _ = await asyncio.wait([serving], timeout=10)
            told = [app.disconnected.is_set() for app in apps]
            forced = [server.force_exit for server in servers]
            still_listening = [listening(port) for port in ports]
            left = listener_tasks()
        finally:  # a serve() left waiting ends once its callers hang up, so a failure is an assertion
            for writer in writers:
                await hung_up(writer)
            await asyncio.wait([serving], timeout=10)
            await gone(left)
    assert ended, "serve() still waited for a held request 10 s after it was cancelled"
    assert all(told)  # each request in flight was told its caller is gone
    assert forced == [True, True]
    assert not any(still_listening) and not left
    assert {sig: signal.getsignal(sig) for sig in signals} == signals
    assert serving.cancelled()  # the caller sees its own cancellation, and nothing else
    with pytest.raises(asyncio.CancelledError):
        await serving


async def test_a_second_cancel_while_serve_cleans_up_still_ends_both_listeners(
    signals: dict[int, object],
) -> None:
    """A caller that cancels twice (a timeout, then its own shutdown) lands the second cancel in the
    clean-up. It still finishes: the process's signal handlers are back, and no server is left running."""
    held = Held()
    servers, ports = two_servers(first=held)
    with _bound(servers) as sockets:
        serving = asyncio.create_task(_serve_together(servers, sockets))
        await started(servers)
        writer = await held_open(ports[0], held)
        try:
            serving.cancel()
            await asyncio.sleep(0)  # the first cancel is in, and the clean-up has begun
            cleaning_up = not serving.done()
            serving.cancel()
            ended, _ = await asyncio.wait([serving], timeout=10)
            left = listener_tasks()
            handlers = {sig: signal.getsignal(sig) for sig in signals}
            still_listening = [listening(port) for port in ports]
        finally:
            await hung_up(writer)
            await asyncio.wait([serving], timeout=10)
            await gone(left)
    assert cleaning_up, "the second cancel must arrive while serve() is still cleaning up"
    assert ended, "serve() had not ended 10 s after it was cancelled twice"
    assert not left, "a server was left running after serve() ended"
    assert handlers == signals
    assert not any(still_listening)
    assert held.disconnected.is_set()
    assert [server.force_exit for server in servers] == [True, True]
    assert serving.cancelled()


async def test_a_cancel_after_a_first_signal_stops_waiting_for_an_open_connection(
    signals: dict[int, object],
) -> None:
    """After one SIGTERM ``serve`` waits for a request in flight, with no limit. A cancel that arrives
    during that wait is the same forced exit, and the caller still sees its cancellation."""
    held = Held()
    servers, ports = two_servers(first=held)
    with _bound(servers) as sockets:
        serving = asyncio.create_task(_serve_together(servers, sockets))
        await started(servers)
        writer = await held_open(ports[0], held)
        try:
            signal.raise_signal(signal.SIGTERM)
            await nothing_listens(ports)  # the idle listener has ended, and the other waits
            assert not serving.done() and not held.disconnected.is_set()
            serving.cancel()
            ended, _ = await asyncio.wait([serving], timeout=10)
            told, left = held.disconnected.is_set(), listener_tasks()
            handlers = {sig: signal.getsignal(sig) for sig in signals}
        finally:
            await hung_up(writer)
            await asyncio.wait([serving], timeout=10)
            await gone(left)
    assert ended, "serve() still waited for a held request 10 s after it was cancelled"
    assert told and not left
    assert handlers == signals
    assert servers[0].force_exit  # the listener that still had a caller was forced
    assert serving.cancelled()


async def test_off_the_main_thread_no_signal_handler_is_installed(signals: dict[int, object]) -> None:
    """Python delivers signals to the main thread only, and refuses a handler set from another. An
    embedder that serves from a worker thread ends the listeners itself."""
    servers, ports = two_servers()
    seen: list[dict[int, object]] = []

    async def serve_in_this_thread() -> None:
        with _bound(servers) as sockets:
            serving = asyncio.create_task(_serve_together(servers, sockets))
            await started(servers)
            seen.append({sig: signal.getsignal(sig) for sig in signals})
            servers[0].should_exit = True
            await asyncio.wait_for(serving, timeout=10)

    await asyncio.to_thread(asyncio.run, serve_in_this_thread())
    assert seen == [signals]
    assert not any(listening(port) for port in ports)
