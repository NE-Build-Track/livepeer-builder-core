"""TLS on the discovery and paid-call paths, over real local HTTPS servers (D42).

Discovery and the paid call verify certificates with the process trust store. An orchestrator whose
certificate that store does not accept is left out of the snapshot at startup and is never called; one
it accepts is kept. A runner that fails the TLS check at call time is ``unreachable`` with no payment.
"""

import ipaddress
import json
import ssl
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import aiohttp
import aiohttp.connector
import livepeer_gateway.live_runner
import pytest
from aiohttp import web
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from livepeer_builder import BuilderEngine, EngineSettings
from livepeer_builder.adapters import sdk
from livepeer_builder.adapters.sdk import SdkRunnerTransport
from livepeer_builder.contracts import JobRequest
from livepeer_builder.errors import RunnerCallError
from livepeer_builder.testing import runner as make_runner

MEMORY = "sqlite+aiosqlite:///:memory:"
APP = "tls/test"


@dataclass(frozen=True)
class Pem:
    cert: bytes
    key: bytes


@dataclass(frozen=True)
class Certificates:
    ca: bytes  # the test CA a "trusted" process accepts
    signed: Pem  # issued by the test CA for 127.0.0.1
    self_signed: Pem  # trusted by nobody


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def _issue(
    subject: str,
    key: ec.EllipticCurvePrivateKey,
    issuer: x509.Name,
    issuer_key: ec.EllipticCurvePrivateKey,
    *,
    ca: bool,
) -> x509.Certificate:
    now = datetime.now(UTC)
    builder = (
        x509.CertificateBuilder()
        .subject_name(_name(subject))
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
    )
    if not ca:
        builder = builder.add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False
        )
    return builder.sign(issuer_key, hashes.SHA256())


def _pem(cert: x509.Certificate, key: ec.EllipticCurvePrivateKey) -> Pem:
    return Pem(
        cert=cert.public_bytes(serialization.Encoding.PEM),
        key=key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        ),
    )


@pytest.fixture(scope="module")
def certificates() -> Certificates:
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca = _issue("lpb test CA", ca_key, _name("lpb test CA"), ca_key, ca=True)
    signed_key = ec.generate_private_key(ec.SECP256R1())
    signed = _issue("127.0.0.1", signed_key, ca.subject, ca_key, ca=False)
    self_key = ec.generate_private_key(ec.SECP256R1())
    self_signed = _issue("127.0.0.1", self_key, _name("127.0.0.1"), self_key, ca=False)
    return Certificates(
        ca=ca.public_bytes(serialization.Encoding.PEM),
        signed=_pem(signed, signed_key),
        self_signed=_pem(self_signed, self_key),
    )


Serve = Callable[[Pem], Awaitable[str]]


@pytest.fixture
async def serve(tmp_path: Path) -> AsyncIterator[Serve]:
    """Start an HTTPS orchestrator on 127.0.0.1 that advertises one runner on itself."""
    runners: list[web.AppRunner] = []

    async def start(pem: Pem) -> str:
        cert, key = tmp_path / f"{len(runners)}.crt", tmp_path / f"{len(runners)}.key"
        cert.write_bytes(pem.cert)
        key.write_bytes(pem.key)
        context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        context.load_cert_chain(cert, key)
        address: dict[str, str] = {}

        async def discovery(request: web.Request) -> web.Response:
            entry = {"address": address["url"], "runners": [{"url": f"{address['url']}/runner", "app": APP}]}
            return web.json_response([entry])

        async def runner_call(request: web.Request) -> web.Response:
            return web.json_response({"ok": True})

        app = web.Application()
        app.router.add_get("/discovery", discovery)
        app.router.add_route("*", "/runner/{tail:.*}", runner_call)
        app_runner = web.AppRunner(app)
        await app_runner.setup()
        site = web.TCPSite(app_runner, "127.0.0.1", 0, ssl_context=context)
        await site.start()
        runners.append(app_runner)
        port = app_runner.addresses[0][1]
        address["url"] = f"https://127.0.0.1:{port}"
        return address["url"]

    yield start
    for app_runner in runners:
        await app_runner.cleanup()


@pytest.fixture
def trust_test_ca(monkeypatch: pytest.MonkeyPatch, certificates: Certificates) -> None:
    """Make the process trust store accept the test CA, as a public CA is accepted in production."""
    if not hasattr(aiohttp.connector, "_SSL_CONTEXT_VERIFIED"):
        pytest.skip("aiohttp no longer exposes its default verified SSL context")
    context = ssl.create_default_context(cadata=certificates.ca.decode())
    context.set_alpn_protocols(("http/1.1",))
    monkeypatch.setattr(aiohttp.connector, "_SSL_CONTEXT_VERIFIED", context)


async def _start_engine(tmp_path: Path, addresses: list[str]) -> BuilderEngine:
    path = tmp_path / "orchestrators.json"
    path.write_text(json.dumps([{"address": address} for address in addresses]), encoding="utf-8")
    engine = await BuilderEngine.from_settings(
        EngineSettings(database_url=MEMORY, orchestrator_list=str(path), log_json=False)
    )
    await engine.start()
    return engine


async def test_startup_excludes_a_self_signed_orchestrator(
    tmp_path: Path, certificates: Certificates, serve: Serve
) -> None:
    url = await serve(certificates.self_signed)
    engine = await _start_engine(tmp_path, [url])
    try:
        health = engine.health()
        assert engine.discovery.snapshot.runners == ()
        assert not health.discovery_fresh
        assert health.discovery_error is not None and "certificate" in health.discovery_error.lower()
    finally:
        await engine.stop()


@pytest.mark.usefixtures("trust_test_ca")
async def test_startup_keeps_only_orchestrators_the_process_trusts(
    tmp_path: Path, certificates: Certificates, serve: Serve
) -> None:
    trusted = await serve(certificates.signed)
    untrusted = await serve(certificates.self_signed)
    engine = await _start_engine(tmp_path, [untrusted, trusted])
    try:
        health = engine.health()
        assert [runner.orchestrator_url for runner in engine.discovery.snapshot.runners] == [trusted]
        assert health.discovery_fresh and health.discovery_error is None
    finally:
        await engine.stop()


@pytest.fixture
def signer_info(monkeypatch: pytest.MonkeyPatch) -> None:
    """The signer is never reached: the runner's TLS check fails on the unpaid first request."""

    async def get_signer_info(*args: object, **kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(address="0x" + "1" * 40)

    monkeypatch.setattr(livepeer_gateway.live_runner, "get_signer_info", get_signer_info)


@pytest.mark.usefixtures("signer_info")
@pytest.mark.parametrize("streamed", [False, True])
async def test_a_call_time_tls_failure_is_unreachable_with_no_payment(
    certificates: Certificates, serve: Serve, streamed: bool
) -> None:
    url = await serve(certificates.self_signed)
    transport = SdkRunnerTransport("https://signer.invalid")
    runner = make_runner(APP, f"{url}/runner", price=None)
    request = JobRequest(app=APP, path="/v1/run", payload={"x": 1}, timeout_s=10)
    attempt = transport.open if streamed else transport.call
    with pytest.raises(RunnerCallError) as failed:
        await attempt(runner, request, "lpg_key")
    assert (failed.value.kind, failed.value.payment_sent, failed.value.manifest_ids) == (
        "unreachable",
        False,
        (),
    )
    assert sdk._untrusted_certificate(failed.value)  # the certificate check, not another network error
