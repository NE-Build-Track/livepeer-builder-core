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
from livepeer_builder.http.server import (
    _bound,
    _listener_server,
    _serve_one_listener,
    _serve_together,
    build_app,
    serve,
)
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
    """Wait until every port answers a request, or fail with what ``serve`` raised. A port that takes a
    connection is not enough: two listeners listen before the engine starts, and answer once it has."""
    for _ in range(200):
        if serving.done():
            serving.result()
            pytest.fail("serve returned before its listeners answered")
        if all(listening(port) for port in ports):
            break
        await asyncio.sleep(0.02)
    else:
        pytest.fail("the listeners did not answer within 4 s")
    for port in ports:
        try:
            await answers(port)
        except httpx.HTTPError:
            if serving.done():
                serving.result()
            raise


async def test_a_caller_that_connects_while_the_engine_starts_is_answered_once_it_has(
    store: SqlStore, monkeypatch: pytest.MonkeyPatch, signals: dict[int, object]
) -> None:
    """Both listeners listen before the engine starts, so an overlap of their addresses is found first.
    A request that arrives during the start is not refused and not answered early: it waits."""
    public, private = free_port(), free_port()
    start, starting, go = BuilderEngine.start, asyncio.Event(), asyncio.Event()

    async def held_start(self: BuilderEngine) -> None:
        starting.set()
        await go.wait()
        await start(self)

    monkeypatch.setattr(BuilderEngine, "start", held_start)
    serving = asyncio.create_task(
        serve(settings(http_port=public, http_private_port=private), supply=EngineSupply(store=store))
    )
    try:
        await asyncio.wait_for(starting.wait(), timeout=5)
        assert listening(public) and listening(private)
        asked = asyncio.create_task(answers(public, "/readyz"))
        done, _ = await asyncio.wait([asked], timeout=0.3)
        assert not done, "a request was answered before the engine had started"
        go.set()
        assert (await asyncio.wait_for(asked, timeout=5)).json()["ready"] is True
    finally:
        go.set()
        serving.cancel()
        await asyncio.wait([serving], timeout=10)
    assert not listening(public) and not listening(private)


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


async def test_a_wildcard_address_and_a_specific_one_on_one_port_stop_before_the_engine_starts(
    store: SqlStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Binding alone does not claim an address: uvicorn sets ``SO_REUSEADDR``, and with it Linux lets
    ``0.0.0.0`` and ``127.0.0.1`` bind one port and refuses the second only when it listens. The
    settings allow the pair (the private listener's host is loopback unless set). Both sockets listen
    before the engine starts, so it never starts, where it used to start, recover interrupted work and
    then fail with an ``OSError`` (#181 review)."""
    port = free_port()
    started: list[str] = []
    built: list[EngineApi] = []

    async def never(self: BuilderEngine) -> None:
        started.append("start")

    monkeypatch.setattr(BuilderEngine, "start", never)
    with pytest.raises(SystemExit) as exited:
        await serve(
            settings(http_host="0.0.0.0", http_port=port, http_private_port=port),  # noqa: S104 - the case
            supply=EngineSupply(store=store),
            extend=lambda _, engine: built.append(engine),
        )
    assert exited.value.code == 3  # uvicorn's status for an address it cannot bind
    assert not started
    assert not listening(port)
    monkeypatch.undo()
    with pytest.raises(RuntimeError, match="engine is stopped"):
        await built[0].start()


def test_a_socket_that_cannot_listen_is_closed_and_ends_the_process_as_one_that_cannot_bind() -> None:
    """Whichever of the two calls the system refuses at: the one bound before it is closed too."""
    first, _ = one_server()
    second, _ = one_server()
    opened: list[socket.socket] = []

    class Deaf(socket.socket):
        def listen(self, *_: int) -> None:
            raise OSError(98, "Address already in use")

    def bind(kind: type[socket.socket]) -> socket.socket:
        opened.append(kind())
        opened[-1].bind(("127.0.0.1", 0))
        return opened[-1]

    first.config.bind_socket = lambda: bind(socket.socket)  # type: ignore[method-assign]
    second.config.bind_socket = lambda: bind(Deaf)  # type: ignore[method-assign]
    with pytest.raises(SystemExit) as exited, _bound([first, second]):
        pytest.fail("nothing is served on a socket that does not listen")
    assert exited.value.code == 3  # as for an address that cannot be bound
    assert [sock.fileno() for sock in opened] == [-1, -1]  # both closed


class StartsAsOlderUvicornDoes(uvicorn.Server):
    """uvicorn before 0.50 returns from ``serve()`` when its app's startup fails, where later ones exit
    with status 3: ``startup()`` asks the server to end and returns, with ``started`` never set."""

    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        self.should_exit = True

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


async def test_a_server_that_returns_from_a_failed_start_is_not_taken_for_one_that_served(
    signals: dict[int, object],
) -> None:
    """``pyproject.toml`` allows uvicorn from 0.30. A start that failed must not end ``lpb serve`` with
    status 0, with one listener or two (#181 review)."""
    config = uvicorn.Config(hello, host="127.0.0.1", port=free_port(), log_config=None)  # type: ignore[arg-type]
    with pytest.raises(SystemExit) as one:
        await asyncio.wait_for(_serve_one_listener(StartsAsOlderUvicornDoes(config)), timeout=10)
    assert one.value.code == 3

    servers, ports = two_servers()
    servers[1] = StartsAsOlderUvicornDoes(servers[1].config)
    with _bound(servers) as sockets, pytest.raises(SystemExit) as two:
        await asyncio.wait_for(_serve_together(servers, sockets), timeout=10)
    assert two.value.code == 3
    assert not any(listening(port) for port in ports)
    assert {sig: signal.getsignal(sig) for sig in signals} == signals


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
    a forced exit (#181 review): the listeners hang up on their open connections, so a request that
    never ends does not hold the task."""
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


def one_server(app: App = hello) -> tuple[uvicorn.Server, int]:
    """One server as ``serve`` builds it for one listener: uvicorn's own, which takes the signals itself."""
    port = free_port()
    return uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None)), port  # type: ignore[arg-type]


async def test_a_cancelled_serve_closes_its_one_listener(signals: dict[int, object]) -> None:
    """uvicorn leaves a cancelled ``serve()`` where it stood, with its listening socket open: the port
    stayed taken for the rest of the process and went on accepting, with the engine stopped (#184
    review). One listener ends on a cancel as two do."""
    server, port = one_server()
    serving = asyncio.create_task(_serve_one_listener(server))
    await started([server])
    assert listening(port)
    serving.cancel()
    ended, _ = await asyncio.wait([serving], timeout=10)
    still_listening, left = listening(port), listener_tasks()
    await gone(left)
    assert ended, "serve() had not ended 10 s after it was cancelled"
    assert not still_listening, "the listener still accepted connections after serve() ended"
    assert not left, "the server was left running after serve() ended"
    assert {sig: signal.getsignal(sig) for sig in signals} == signals
    assert serving.cancelled()  # the caller sees its own cancellation, and nothing else


async def test_a_cancelled_serve_with_one_listener_ends_while_a_request_is_held_open(
    signals: dict[int, object],
) -> None:
    """The cancel is a forced exit for one listener too: it hangs up on its open connections, and a
    request that never ends is told its caller is gone."""
    held = Held()
    server, port = one_server(held)
    serving = asyncio.create_task(_serve_one_listener(server))
    await started([server])
    writer = await held_open(port, held)
    try:
        serving.cancel()
        ended, _ = await asyncio.wait([serving], timeout=10)
        told, still_listening, left = held.disconnected.is_set(), listening(port), listener_tasks()
    finally:  # a serve() left waiting ends once its caller hangs up, so a failure is an assertion
        await hung_up(writer)
        await asyncio.wait([serving], timeout=10)
        await gone(left)
    assert ended, "serve() still waited for a held request 10 s after it was cancelled"
    assert told and server.force_exit
    assert not still_listening and not left
    assert {sig: signal.getsignal(sig) for sig in signals} == signals
    assert serving.cancelled()


async def test_a_second_cancel_while_one_listener_ends_still_closes_it(signals: dict[int, object]) -> None:
    held = Held()
    server, port = one_server(held)
    serving = asyncio.create_task(_serve_one_listener(server))
    await started([server])
    writer = await held_open(port, held)
    try:
        serving.cancel()
        await asyncio.sleep(0)  # the first cancel is in, and the server is being ended
        ending = not serving.done()
        serving.cancel()
        ended, _ = await asyncio.wait([serving], timeout=10)
        still_listening, left = listening(port), listener_tasks()
    finally:
        await hung_up(writer)
        await asyncio.wait([serving], timeout=10)
        await gone(left)
    assert ending, "the second cancel must arrive while the server is still being ended"
    assert ended, "serve() had not ended 10 s after it was cancelled twice"
    assert not still_listening and not left
    assert held.disconnected.is_set()
    assert serving.cancelled()


async def test_a_signal_still_ends_one_listener_in_order(signals: dict[int, object]) -> None:
    """Unchanged: uvicorn takes the signal, stops accepting and waits for the request in flight."""
    held = Held()
    server, port = one_server(held)
    serving = asyncio.create_task(_serve_one_listener(server))
    await started([server])
    writer = await held_open(port, held)
    try:
        signal.raise_signal(signal.SIGTERM)
        await nothing_listens([port])
        done, _ = await asyncio.wait([serving], timeout=1)
        assert not done and not held.disconnected.is_set()  # the request in flight is waited for
    finally:
        await hung_up(writer)
    await asyncio.wait_for(serving, timeout=10)
    assert not server.force_exit
    assert not listener_tasks()
    assert {sig: signal.getsignal(sig) for sig in signals} == signals


async def test_one_listener_whose_app_cannot_start_exits_as_uvicorn_does(signals: dict[int, object]) -> None:
    """uvicorn exits with status 3 when an app's startup fails. The server runs in a task, where that
    exit would end the event loop where it stands: it is raised from ``serve`` instead."""
    server, port = one_server(failing_startup)
    with pytest.raises(SystemExit) as exited:
        await asyncio.wait_for(_serve_one_listener(server), timeout=10)
    assert exited.value.code == 3
    assert not listening(port) and not listener_tasks()


async def test_a_cancelled_serve_leaves_nothing_listening_and_the_engine_stopped(
    store: SqlStore, signals: dict[int, object]
) -> None:
    """The whole of it, through ``serve()`` with its default of one listener."""
    port = free_port()
    built: list[EngineApi] = []
    serving = asyncio.create_task(
        serve(
            settings(http_host="127.0.0.1", http_port=port),
            supply=EngineSupply(store=store),
            extend=lambda _, engine: built.append(engine),
        )
    )
    await nothing_listens_yet_then_does(port)
    serving.cancel()
    ended, _ = await asyncio.wait([serving], timeout=10)
    still_listening, left = listening(port), listener_tasks()
    await gone(left)
    assert ended and serving.cancelled()
    assert not still_listening and not left
    with pytest.raises(RuntimeError, match="engine is stopped"):
        await built[0].start()


async def nothing_listens_yet_then_does(port: int) -> None:
    for _ in range(200):
        if listening(port):
            return
        await asyncio.sleep(0.02)
    pytest.fail("the listener did not accept a connection within 4 s")


class WithLifespan:
    """An ASGI app with a lifespan of its own, as the engine's app has, that notes what it is told. A
    request it takes is held until it is cancelled or its caller hangs up."""

    def __init__(self) -> None:
        self.events: list[str] = []
        self.arrived = asyncio.Event()
        self.disconnected = asyncio.Event()

    async def __call__(self, scope: dict[str, Any], receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await receive()
            self.events.append("startup")
            await send({"type": "lifespan.startup.complete"})
            await receive()
            self.events.append("shutdown")
            await send({"type": "lifespan.shutdown.complete"})
            return
        self.arrived.set()
        try:
            await asyncio.Event().wait()  # a handler that awaits a runner, and is told nothing
        finally:
            self.events.append("request ended")


def lifespan_tasks() -> "list[asyncio.Task[Any]]":
    """uvicorn's task for an app's lifespan, while one is still waiting to be told to shut down."""
    return [
        task
        for task in asyncio.all_tasks()
        if "LifespanOn.main" in getattr(task.get_coro(), "__qualname__", "") and not task.done()
    ]


ENDED_IN_ORDER = ["startup", "request ended", "shutdown"]


async def test_a_cancelled_serve_with_one_listener_finishes_its_apps_lifespan(
    signals: dict[int, object],
) -> None:
    """uvicorn's forced exit skips the app's shutdown, and its lifespan task then waits for the rest of
    the process, holding the app and the engine it closes over (#184 review). ``serve()`` finishes the
    lifespan once the listener has closed and its requests have ended."""
    app = WithLifespan()
    server, port = one_server(app)  # type: ignore[arg-type]
    serving = asyncio.create_task(_serve_one_listener(server))
    await started([server])
    writer = await held_open(port, app)  # type: ignore[arg-type]
    try:
        serving.cancel()
        ended, _ = await asyncio.wait([serving], timeout=10)
        events, left = list(app.events), lifespan_tasks()
    finally:
        await hung_up(writer)
        await asyncio.wait([serving], timeout=10)
        for task in lifespan_tasks():
            task.cancel()
    assert ended and serving.cancelled()
    assert events == ENDED_IN_ORDER
    assert not left, "the app's lifespan was still waiting after serve() returned"
    assert not listening(port)


@pytest.mark.parametrize("forced_by", ["a cancel", "a second interrupt", "a listener that ended"])
async def test_two_listeners_forced_to_exit_finish_their_apps_lifespans(
    signals: dict[int, object], forced_by: str
) -> None:
    apps = [WithLifespan(), WithLifespan()]
    servers, ports = two_servers(*apps)  # type: ignore[arg-type]
    with _bound(servers) as sockets:
        serving = asyncio.create_task(_serve_together(servers, sockets))
        await started(servers)
        writer = await held_open(ports[0], apps[0])  # type: ignore[arg-type]
        try:
            if forced_by == "a cancel":
                serving.cancel()
            elif forced_by == "a second interrupt":
                signal.raise_signal(signal.SIGINT)
                signal.raise_signal(signal.SIGINT)
            else:
                servers[1].should_exit = True
            ended, _ = await asyncio.wait([serving], timeout=10)
            events, left = [list(app.events) for app in apps], lifespan_tasks()
        finally:
            await hung_up(writer)
            await asyncio.wait([serving], timeout=10)
            for task in lifespan_tasks():
                task.cancel()
    assert ended
    assert events == [ENDED_IN_ORDER, ["startup", "shutdown"]]
    assert not left, "an app's lifespan was still waiting after serve() returned"
    assert not any(listening(port) for port in ports)
    assert {sig: signal.getsignal(sig) for sig in signals} == signals


async def test_a_listener_that_ends_in_order_is_told_to_shut_down_once(signals: dict[int, object]) -> None:
    """Unchanged: uvicorn shuts the app down itself, and it is not told a second time."""
    app = WithLifespan()
    server, port = one_server(app)  # type: ignore[arg-type]
    serving = asyncio.create_task(_serve_one_listener(server))
    await started([server])
    signal.raise_signal(signal.SIGTERM)
    await asyncio.wait_for(serving, timeout=10)
    assert app.events == ["startup", "shutdown"]
    assert not lifespan_tasks() and not listening(port)


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
