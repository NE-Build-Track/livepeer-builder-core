"""Local HTTPS orchestrators for probe tests: one with a certificate from a test CA the prober trusts
(standing in for the system trust store), one self-signed."""

import datetime
import ipaddress
import ssl
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


def _certificate(
    subject: str,
    issuer_key: Any,
    issuer: x509.Name | None,
    *,
    ca: bool,
    names: list[x509.GeneralName] | None = None,
) -> tuple[Any, x509.Certificate]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject)])
    now = datetime.datetime.now(datetime.UTC)
    builder = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(issuer or name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
    )
    if not ca:
        builder = builder.add_extension(
            x509.SubjectAlternativeName(
                names or [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
    return key, builder.sign(issuer_key or key, hashes.SHA256())


def _write(tmp: Path, stem: str, key: Any, cert: x509.Certificate) -> tuple[Path, Path]:
    cert_path, key_path = tmp / f"{stem}.pem", tmp / f"{stem}.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))  # fmt: skip
    return cert_path, key_path


@dataclass
class Tls:
    trusted: ssl.SSLContext  # server side: a certificate from the test CA
    self_signed: ssl.SSLContext  # server side: no CA vouches for it
    named: ssl.SSLContext  # server side: from the test CA, naming only orch.test (no IP address)
    client: ssl.SSLContext  # what the prober uses: trusts only the test CA
    ca_file: Path  # the test CA, for a deployment that extends its trust store (SSL_CERT_FILE)


@pytest.fixture(scope="session")
def tls(tmp_path_factory: pytest.TempPathFactory) -> Tls:
    tmp = tmp_path_factory.mktemp("tls")
    ca_key, ca_cert = _certificate("test CA", None, None, ca=True)
    ca_path, _ = _write(tmp, "ca", ca_key, ca_cert)
    leaf_key, leaf = _certificate("localhost", ca_key, ca_cert.subject, ca=False)
    own_key, own = _certificate("localhost", None, None, ca=False)
    named_key, named = _certificate(
        "orch.test", ca_key, ca_cert.subject, ca=False, names=[x509.DNSName("orch.test")]
    )

    def server(cert: x509.Certificate, key: Any, stem: str) -> ssl.SSLContext:
        context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        context.load_cert_chain(*_write(tmp, stem, key, cert))
        return context

    return Tls(
        trusted=server(leaf, leaf_key, "trusted"),
        self_signed=server(own, own_key, "self-signed"),
        named=server(named, named_key, "named"),
        client=ssl.create_default_context(cafile=str(ca_path)),
        ca_file=ca_path,
    )


Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]
Serve = Callable[[Handler, ssl.SSLContext | None], Awaitable[str]]


@pytest.fixture
async def serve() -> AsyncIterator[Serve]:
    """Start an orchestrator answering GET /discovery with ``handler``; returns its base URL."""
    runners: list[web.AppRunner] = []

    async def start(handler: Handler, context: ssl.SSLContext | None) -> str:
        app = web.Application()
        app.router.add_get("/discovery", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=context)
        await site.start()
        runners.append(runner)
        port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        return f"{'https' if context else 'http'}://127.0.0.1:{port}"

    yield start
    for runner in runners:
        await runner.cleanup()
