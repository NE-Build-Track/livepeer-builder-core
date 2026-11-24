"""The stock server's composition root: ``lpb serve`` and your own entrypoint.

Customize the service the way you customize the embedded engine, with the same two stages
(docs/design-docs/extending-the-engine.md): ``supply`` parts, ``decorate`` defaults, and ``extend``
the app with routes that receive the engine explicitly::

    asyncio.run(serve(EngineSettings.from_env(), supply=..., decorate=..., extend=...))

With ``LPB_HTTP_PRIVATE_PORT`` set, ``serve`` runs two listeners over the one engine, each with an app
of its own (``http/listeners.py``, #167): the public one on ``LPB_HTTP_HOST`` and ``LPB_HTTP_PORT``, the
private one on ``LPB_HTTP_PRIVATE_HOST`` and that port. ``extend`` runs once for each app, and
``app.state.listener.name`` says which it has: ``"public"`` or ``"private"``.
"""

import asyncio
import contextlib
import logging
import signal
import socket
import threading
from collections.abc import Callable, Iterator, Sequence
from typing import TYPE_CHECKING

from fastapi import FastAPI

from livepeer_builder import BuilderEngine, Decorate, EngineApi, EngineSettings, EngineSupply
from livepeer_builder.http.app import create_app
from livepeer_builder.http.auth import actor_dependency
from livepeer_builder.http.listeners import ListenerName
from livepeer_builder.runtime import configure_logging

if TYPE_CHECKING:
    import uvicorn

_LOG = logging.getLogger(__name__)

Extend = Callable[[FastAPI, EngineApi], None]
"""Adds routes or middleware to the app; receives the engine, so nothing is looked up."""


async def build_app(
    settings: EngineSettings,
    *,
    supply: EngineSupply | None = None,
    decorate: Decorate | None = None,
    extend: Extend | None = None,
) -> FastAPI:
    """An app for your own ASGI stack; logging is yours.

    The engine lives as long as the app's lifespan, which starts and stops it, so run that lifespan (any
    ASGI server does). To hold the engine yourself, use ``BuilderEngine.create`` and ``create_app``.

    Without ``LPB_DISCOVERY_URL`` it refuses here, as ``serve`` does: the lifespan would refuse to start
    the engine (D63, #154), after ``create`` had opened the database and migrated it.
    """
    settings.require_discovery_url()
    if settings.http_private_port is not None:
        raise ValueError(
            "LPB_HTTP_PRIVATE_PORT is set, and build_app builds the app of one listener. Run serve(), "
            "which serves both, or hold the engine yourself (BuilderEngine.create) and build each app "
            'with create_app(engine, listener="public") and create_app(engine, listener="private")'
        )
    engine = await BuilderEngine.create(settings, supply=supply, decorate=decorate)
    try:
        return _app(engine, settings, extend)
    except BaseException:
        await engine.stop()
        raise


async def serve(
    settings: EngineSettings,
    *,
    supply: EngineSupply | None = None,
    decorate: Decorate | None = None,
    extend: Extend | None = None,
) -> None:
    """Run the service. The engine is stopped even when the server never starts (a taken port).

    One listener, or two when ``LPB_HTTP_PRIVATE_PORT`` is set: then an address that cannot be bound
    stops both and the engine, and neither listener serves without the other.

    To stop it from your own code, cancel the task that runs it. That does not wait for requests in
    flight, with one listener or two: each listener is closed and hangs up on its callers, the requests
    still running are cancelled, the app's shutdown handlers run, the engine is stopped, and the
    cancellation is raised.

    Without ``LPB_DISCOVERY_URL`` it refuses here, before the store is opened or migrated: the engine it
    is about to start would refuse (D63, #154), and ``create`` opens the database and migrates it."""
    import uvicorn

    settings.require_discovery_url()
    configure_logging(json_lines=settings.log_json)
    engine = await BuilderEngine.create(settings, supply=supply, decorate=decorate)
    try:
        if settings.http_private_port is None:
            app = _app(engine, settings, extend)
            config = uvicorn.Config(app, host=settings.http_host, port=settings.http_port, log_config=None)
            await _serve_one_listener(uvicorn.Server(config))
        else:
            await _serve_two_listeners(engine, settings, settings.http_private_port, extend)
    finally:
        await engine.stop()  # idempotent: the lifespan may already have stopped it


async def _serve_one_listener(server: "uvicorn.Server") -> None:
    """One server, as uvicorn runs it and with uvicorn's own signal handling, but for a cancel. uvicorn
    leaves a cancelled ``serve()`` where it stood: its listening socket stays open and goes on accepting
    for the rest of the process, with the engine stopped. So the server runs in a task of its own, and
    a cancel is the forced exit it is for two listeners (``_all_ended``): the listener is closed and
    hangs up on its open connections, and the cancellation is raised once the server has ended."""
    task = asyncio.create_task(_run(server), name="lpb-listener-0")
    try:
        await _ended([server], [task])
    except asyncio.CancelledError:
        server.should_exit = True
        await _all_ended([server], [task], forced=True)
        raise
    asked = task.result()
    if asked is not None:
        raise asked


_STARTUP_FAILURE = 3  # uvicorn's exit status for a server that cannot start: an address, or its app


async def _ended_what_it_left(server: "uvicorn.Server") -> None:
    """End what a server that has ended left running: its requests, then its app's lifespan."""
    left = [request for request in server.server_state.tasks if not request.done()]
    for request in left:
        request.cancel()
    if left:
        await asyncio.wait(left)
    if server.force_exit and server.started:  # in order, uvicorn has shut the app down itself
        await server.lifespan.shutdown()


async def _run(server: "uvicorn.Server", sockets: list[socket.socket] | None = None) -> SystemExit | None:
    """Run one server in a task, to its end and the end of its requests. The exit uvicorn asks for when
    a server cannot start is returned: raised inside a task, it would end the event loop where it
    stands, so the caller raises it, once every server has ended.

    A server that ended in order has waited for its requests. One forced to exit has not: hanging up on
    a caller does not end a handler that awaits a runner, and ``serve()`` would stop the engine under it
    (#181 review). So a request still running is cancelled, as uvicorn cancels one that outlasts its own
    shutdown timeout, and waited for: a job cut off this way writes its end as a cancelled job does,
    before the engine closes its store. Then the app is told to shut down, which a forced exit skips:
    left to uvicorn, its lifespan task waits for the rest of the process, with the app and the engine
    it closes over (#184 review)."""
    try:
        await (server.serve() if sockets is None else server.serve(sockets=sockets))
    except SystemExit as asked:  # NOSONAR - returned for the caller to raise, once the servers have ended
        return asked
    finally:
        # On every way out: after a second Ctrl-C uvicorn gives the signal back to the handler it found,
        # and the one ``asyncio.run`` installs raises ``KeyboardInterrupt`` right where it is awaited.
        await _ended_what_it_left(server)
    # uvicorn before 0.50 returns from a start that failed, where later ones exit: never a server that served.
    return None if server.started else SystemExit(_STARTUP_FAILURE)


def _app(
    engine: EngineApi,
    settings: EngineSettings,
    extend: Extend | None,
    listener: ListenerName = "only",
    *,
    manage_lifecycle: bool = True,
) -> FastAPI:
    app = create_app(
        engine,
        manage_lifecycle=manage_lifecycle,
        max_request_bytes=settings.http_max_request_bytes,
        listener=listener,
    )
    if extend is not None:
        extend(app, engine)
    return app


async def _serve_two_listeners(
    engine: BuilderEngine, settings: EngineSettings, private_port: int, extend: Extend | None
) -> None:
    """The public and the private listener, each a server with its own app, over one engine.

    Neither app's lifespan starts the engine, which starts once: here, after both addresses are bound
    and before either server accepts a request. ``serve`` stops it once both servers have ended."""
    public = (settings.http_host, settings.http_port)
    private = (settings.http_private_host, private_port)
    servers = [
        _listener_server(_app(engine, settings, extend, "public", manage_lifecycle=False), *public),
        _listener_server(_app(engine, settings, extend, "private", manage_lifecycle=False), *private),
    ]
    with _bound(servers) as sockets:
        _LOG.info(
            "two listeners",
            extra={
                "event": "http.listeners_bound",
                "public": f"{public[0]}:{public[1]}",
                "private": f"{private[0]}:{private[1]}",
            },
        )
        await engine.start()
        await _serve_together(servers, sockets)


def _listener_server(app: FastAPI, host: str, port: int) -> "uvicorn.Server":
    import uvicorn

    class ListenerServer(uvicorn.Server):
        """One of two servers in a process. ``_serve_together`` takes the process's signals for both,
        so neither installs handlers of its own: uvicorn's would stop the one server that installed
        them last, and then give the signal back to a handler the other had replaced."""

        @contextlib.contextmanager
        def capture_signals(self) -> Iterator[None]:
            yield

    return ListenerServer(uvicorn.Config(app, host=host, port=port, log_config=None))


@contextlib.contextmanager
def _bound(servers: Sequence["uvicorn.Server"]) -> Iterator[list[socket.socket]]:
    """Both listeners' sockets, bound and listening before the engine starts, and closed on the way out.
    uvicorn binds each as it does for one listener: an address that is taken is logged, and the process
    exits with uvicorn's status for it. The socket opened before it is closed, so no listener is left
    half up. A caller that connects while the engine starts waits for it."""
    with contextlib.ExitStack() as opened:
        yield [opened.enter_context(contextlib.closing(_listening(server))) for server in servers]


def _listening(server: "uvicorn.Server") -> socket.socket:
    """One listener's socket, bound and listening. Binding alone does not claim an address: uvicorn sets
    ``SO_REUSEADDR``, and with it Linux lets a wildcard address and a specific one bind the same port,
    and refuses the second only when it listens. Left to uvicorn, that refusal came after the engine had
    started and recovered interrupted work."""
    sock = server.config.bind_socket()
    try:
        sock.listen(server.config.backlog)
    except OSError as error:
        sock.close()
        _LOG.error(  # as uvicorn logs an address it cannot bind: one line, no traceback
            "a listener's address cannot be used",
            extra={
                "event": "http.listener_not_bound",
                "address": f"{server.config.host}:{server.config.port}",
                "error": str(error),
            },
        )
        raise SystemExit(_STARTUP_FAILURE) from error
    return sock


async def _serve_together(servers: Sequence["uvicorn.Server"], sockets: Sequence[socket.socket]) -> None:
    """Run every server until one ends, then end the others: a signal ends them all, and one that stops
    for a reason of its own takes the rest with it. The first error a server raised is raised here, and
    so is the exit uvicorn asks for when an app's startup fails, once the other server has ended.

    A signal ends the servers in order: each waits for its open connections, as one listener does. Three
    things end them without that wait (``_ended``): a second Ctrl-C, a server that ended with no signal
    sent, and a cancelled ``serve()``. A server that ended by itself leaves a process that must end:
    uvicorn waits for open connections without limit, and one held open would keep the process up with a
    listener gone, where no supervisor restarts it. Cancelling is how a caller stops ``serve()``, so it
    must not wait for a request that may never end.

    However this is left, the servers have ended and the process has its own signal handlers back. A
    cancelled caller sees its cancellation, whatever the servers ended with."""

    signalled: list[int] = []  # the signals taken: a stop that was asked for ends in order

    def stop(signum: int | None = None, _frame: object = None) -> None:
        if signum is not None:
            signalled.append(signum)
        for server in servers:
            if server.should_exit and signum == signal.SIGINT:
                server.force_exit = True  # a second Ctrl-C does not wait for open connections
            server.should_exit = True

    # Signals reach the main thread only; elsewhere the caller stops serve() by cancelling it.
    takes_signals = threading.current_thread() is threading.main_thread()
    signals = (signal.SIGINT, signal.SIGTERM) if takes_signals else ()
    previous = {signum: signal.signal(signum, stop) for signum in signals}
    tasks = [
        asyncio.create_task(_run(server, [sock]), name=f"lpb-listener-{index}")
        for index, (server, sock) in enumerate(zip(servers, sockets, strict=True))
    ]
    in_order = False  # only a signal ends the servers in order; a cancel raises out of the wait below
    try:
        await _ended(servers, tasks, first_only=True)
        in_order = bool(signalled)
    finally:
        stop()
        try:
            await _all_ended(servers, tasks, forced=not in_order)
            ended = await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)
    for outcome in ended:
        if isinstance(outcome, BaseException):
            raise outcome


async def _all_ended(
    servers: Sequence["uvicorn.Server"], tasks: Sequence["asyncio.Task[SystemExit | None]"], *, forced: bool
) -> None:
    """Wait until every server has ended, through any cancellation: a server left running would outlive
    ``serve()`` with its listener and its connections. A cancel that arrives here forces the servers
    still running, so the wait is short, and it is raised once they have ended."""
    cancelled: asyncio.CancelledError | None = None
    while True:
        for server in servers:
            server.force_exit = server.force_exit or forced
        try:
            await _ended(servers, tasks)
        except asyncio.CancelledError as cancel:  # NOSONAR - re-raised below, once the servers have ended
            cancelled, forced = cancel, True
            continue
        if cancelled is not None:
            raise cancelled
        return


_HANG_UP_EVERY_S = 0.1  # uvicorn's own tick: how soon a server asked to end stops accepting


async def _ended(
    servers: Sequence["uvicorn.Server"],
    tasks: Sequence["asyncio.Task[SystemExit | None]"],
    *,
    first_only: bool = False,
) -> None:
    """Wait until every server has ended, or the first one with ``first_only``. A server asked to end
    waits for its open connections. One forced to exit does not wait for its callers: ``force_exit``
    is uvicorn's forced exit, with no wait for open connections or for requests in flight, and no
    lifespan shutdown (``_run`` ends the requests and the lifespan). It is not enough by itself: from
    Python 3.12 the listening server's ``wait_closed()``, which uvicorn awaits last, returns only once
    every connection is gone. So each open connection of a forced server is hung up, again on every
    tick until it has ended: one may still be accepted before a server closes its listening socket. A
    request in flight sees its caller disconnect, as when a caller hangs up."""
    running = {task for task in tasks if not task.done()}
    while running and not (first_only and len(running) < len(tasks)):
        for server in servers:
            if server.force_exit:
                for connection in list(server.server_state.connections):
                    connection.transport.abort()
        _, running = await asyncio.wait(
            running, timeout=_HANG_UP_EVERY_S, return_when=asyncio.FIRST_COMPLETED
        )


__all__ = ["Extend", "actor_dependency", "build_app", "serve"]
