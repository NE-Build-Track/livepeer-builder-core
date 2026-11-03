"""The Railway signer relays its loopback metrics onto the private network."""

import errno
import importlib.util
import signal
import socket
import subprocess
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENTRYPOINT = ROOT / "deploy" / "railway" / "signer" / "entrypoint.py"


def _load():
    spec = importlib.util.spec_from_file_location("railway_signer_entrypoint", ENTRYPOINT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


entrypoint = _load()


def test_metrics_relay_forwards_bytes_to_the_upstream() -> None:
    upstream = socket.create_server(("127.0.0.1", 0))
    relay_at = socket.create_server(("127.0.0.1", 0))
    upstream_port = upstream.getsockname()[1]
    relay_port = relay_at.getsockname()[1]
    upstream.close()
    relay_at.close()

    def answer() -> None:
        server = socket.create_server(("127.0.0.1", upstream_port))
        client, _ = server.accept()
        data = client.recv(64)
        client.sendall(b"ready:" + data)
        client.close()
        server.close()

    threading.Thread(target=answer, daemon=True).start()
    threading.Thread(
        target=entrypoint.serve_metrics_relay,
        args=(entrypoint.relay_listener(("127.0.0.1", relay_port)), ("127.0.0.1", upstream_port)),
        daemon=True,
    ).start()

    with socket.create_connection(("127.0.0.1", relay_port), timeout=2) as client:
        client.sendall(b"ping")
        client.shutdown(socket.SHUT_WR)
        assert client.recv(64) == b"ready:ping"


class Listener:
    """A relay listener whose ``accept`` raises each error in turn."""

    def __init__(self, *errors: OSError) -> None:
        self.errors = list(errors)
        self.accepts = 0

    def accept(self) -> tuple[socket.socket, object]:
        self.accepts += 1
        raise self.errors.pop(0)


def test_a_relay_that_stops_stops_the_signer() -> None:
    """A dead relay leaves a signer that looks healthy while /readyz and /metrics refuse: restart both."""
    killed: list[tuple[int, int]] = []
    listener = Listener(OSError(errno.EBADF, "closed"))
    entrypoint.run_relay(
        lambda: listener, 4242, ("::1", 8938), kill=lambda pid, sig: killed.append((pid, sig))
    )
    assert killed == [(4242, signal.SIGTERM)]


def test_a_dropped_connection_does_not_stop_the_relay() -> None:
    killed: list[tuple[int, int]] = []
    listener = Listener(ConnectionAbortedError(errno.ECONNABORTED, "aborted"), OSError(errno.EBADF, "closed"))
    entrypoint.run_relay(
        lambda: listener, 4242, ("::1", 8938), kill=lambda pid, sig: killed.append((pid, sig))
    )
    assert listener.accepts == 2 and killed == [(4242, signal.SIGTERM)]


def test_the_relay_port_is_bound_before_the_signer_starts() -> None:
    """A port that cannot be bound fails startup, instead of a relay that dies after the exec."""
    main = ENTRYPOINT.read_text().split("def main()", 1)[1]
    assert main.index("relay_listener(") < main.index("os.execv(")


def test_a_relay_process_that_cannot_start_stops_the_signer() -> None:
    """The real child process: a listener it cannot open stops the signer it was started for."""
    signer = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"]
    )  # stands in for the signer
    try:
        relay = subprocess.run(  # noqa: S603
            [sys.executable, str(ENTRYPOINT), "relay", "9999", str(signer.pid)],  # fd 9999 is not open
            capture_output=True,
            timeout=10,
            check=False,
        )
        assert signer.wait(timeout=5) == -signal.SIGTERM
        assert b"metrics relay stopped" in relay.stderr
    finally:
        signer.kill()
