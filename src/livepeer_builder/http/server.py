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
    """
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
    stops both and the engine, and neither listener serves without the other."""
    import uvicorn

    configure_logging(json_lines=settings.log_json)
    engine = await BuilderEngine.create(settings, supply=supply, decorate=decorate)
    try:
        if settings.http_private_port is None:
            app = _app(engine, settings, extend)
            config = uvicorn.Config(app, host=settings.http_host, port=settings.http_port, log_config=None)
            await uvicorn.Server(config).serve()
        else:
            await _serve_two_listeners(engine, settings, settings.http_private_port, extend)
    finally:
        await engine.stop()  # idempotent: the lifespan may already have stopped it


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
    """Both listeners' sockets, bound before anything is served, and closed on the way out. uvicorn
    binds each as it does for one listener: an address that is taken is logged, and the process exits
    with uvicorn's status for a failed start. The socket bound before it is closed, so no listener is
    left half up."""
    with contextlib.ExitStack() as opened:
        yield [opened.enter_context(contextlib.closing(server.config.bind_socket())) for server in servers]


async def _serve_together(servers: Sequence["uvicorn.Server"], sockets: Sequence[socket.socket]) -> None:
    """Run every server until one ends, then end the others: a signal ends them all, and one that stops
    for a reason of its own takes the rest with it. The first error a server raised is raised here, and
    so is the exit uvicorn asks for when an app's startup fails, once the other server has ended.

    A signal ends the servers in order: each waits for its open connections, as one listener does. A
    server that ended with no signal sent leaves a process that must end, so the rest do not wait for
    theirs (``_end_without_waiting``): uvicorn waits for open connections without limit, and one held
    open would keep the process up with a listener gone, where no supervisor restarts it."""

    async def run(server: "uvicorn.Server", sock: socket.socket) -> SystemExit | None:
        try:
            await server.serve(sockets=[sock])
        except SystemExit as asked:  # raised inside a task, it would end the event loop where it stands
            return asked
        return None

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
        asyncio.create_task(run(server, sock), name=f"lpb-listener-{index}")
        for index, (server, sock) in enumerate(zip(servers, sockets, strict=True))
    ]
    ended_by_itself = False
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        ended_by_itself = not signalled  # a cancelled serve() is asked to stop too, and never gets here
    finally:
        stop()
        if ended_by_itself:
            await _end_without_waiting(servers, tasks)
        ended = await asyncio.gather(*tasks, return_exceptions=True)
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    for outcome in ended:
        if isinstance(outcome, BaseException):
            raise outcome


_HANG_UP_EVERY_S = 0.1  # uvicorn's own tick: how soon a server asked to end stops accepting


async def _end_without_waiting(
    servers: Sequence["uvicorn.Server"], tasks: Sequence["asyncio.Task[SystemExit | None]"]
) -> None:
    """End every server still running without waiting for its callers. ``force_exit`` is uvicorn's
    forced exit: no wait for open connections or for requests in flight, and no lifespan shutdown. It
    is not enough by itself: from Python 3.12 the listening server's ``wait_closed()``, which uvicorn
    awaits last, returns only once every connection is gone. So each open connection is hung up, again
    on every tick until the servers have ended: one may still be accepted before a server closes its
    listening socket. A request in flight sees its caller disconnect, as when a caller hangs up."""
    for server in servers:
        server.force_exit = True
    running = {task for task in tasks if not task.done()}
    while running:
        for server in servers:
            for connection in list(server.server_state.connections):
                connection.transport.abort()
        _, running = await asyncio.wait(running, timeout=_HANG_UP_EVERY_S)


__all__ = ["Extend", "actor_dependency", "build_app", "serve"]
