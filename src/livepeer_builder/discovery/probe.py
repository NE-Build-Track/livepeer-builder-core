"""Reading each orchestrator's ``GET /discovery``, the inventory a livepeer-node orchestrator serves.

Only orchestrators whose certificate the deployment's trust store accepts are kept: the engine calls
their runners over verified TLS, so one that is not trusted could never be used. That store is the
system's, as OpenSSL loads it, so ``SSL_CERT_FILE`` / ``SSL_CERT_DIR`` extend it: a deployment that pins
orchestrator certificates for its engine mounts the same pins here. Each answer becomes one
entry under the address we probed (an orchestrator cannot claim another's), with its runner records
passed on untouched for the engine to parse.

A URL the operator did not write (``Orchestrator.public_only``: an on-chain registration) is resolved once,
refused unless every address is public, and fetched from the address that was checked, so a registration
cannot point the service at its own network or a metadata endpoint, nor rebind between check and connect.
One orchestrator's failure, of any kind, is that orchestrator's ``Probe.failure``: it never ends the pass.
"""

import asyncio
import contextlib
import ipaddress
import json
import logging
import socket
import ssl
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict

from livepeer_builder.discovery.directory import Orchestrator

_LOG = logging.getLogger(__name__)

Failure = Literal["untrusted_certificate", "private_address", "failed"]
Resolve = Callable[[str, int], Awaitable[list[str]]]


class _PrivateAddress(Exception):
    """A public-only URL resolved to an address inside some network."""


def public_address(address: str) -> bool:
    """True for an address on the public internet: not private, loopback, link-local, shared (CGNAT),
    reserved, unspecified or multicast, including an IPv4 address mapped into IPv6."""
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


async def _resolve(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


class Probe(BaseModel):
    """One orchestrator's answer: an entry, or why there is none."""

    model_config = ConfigDict(frozen=True)

    orchestrator: Orchestrator
    entry: dict[str, Any] | None = None
    failure: Failure | None = None


class Prober:
    def __init__(
        self,
        *,
        ssl_context: ssl.SSLContext | None = None,
        timeout_s: float = 5.0,
        max_bytes: int = 1 << 20,
        concurrency: int = 16,
        resolve: Resolve = _resolve,
        address_allowed: Callable[[str], bool] = public_address,
    ) -> None:
        # the deployment's trust store: system roots plus SSL_CERT_FILE / SSL_CERT_DIR (pinned certificates)
        self._verify = ssl_context or ssl.create_default_context()
        self._timeout_s = timeout_s
        self._max_bytes = max_bytes
        self._concurrency = concurrency
        self._resolve = resolve
        self._allowed = address_allowed

    async def probe_all(self, orchestrators: Sequence[Orchestrator]) -> list[Probe]:
        """Every orchestrator, at most ``concurrency`` at a time; results in the same order."""
        slots = asyncio.Semaphore(self._concurrency)
        async with self._client() as client:

            async def one(orchestrator: Orchestrator) -> Probe:
                async with slots:
                    return await self._probe(client, orchestrator)

            return list(await asyncio.gather(*(one(o) for o in orchestrators)))

    async def probe(self, orchestrator: Orchestrator) -> Probe:
        async with self._client() as client:
            return await self._probe(client, orchestrator)

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(verify=self._verify, timeout=self._timeout_s, follow_redirects=False)

    async def _probe(self, client: httpx.AsyncClient, orchestrator: Orchestrator) -> Probe:
        try:
            async with asyncio.timeout(self._timeout_s):
                runners = await self._runners(client, *await self._target(orchestrator))
        except httpx.ConnectError as exc:
            return self._excluded(orchestrator, "untrusted_certificate" if _untrusted(exc) else "failed", exc)
        except _PrivateAddress as exc:
            return self._excluded(orchestrator, "private_address", exc)
        except Exception as exc:  # anything else is this orchestrator's failure; the pass goes on
            return self._excluded(orchestrator, "failed", exc)
        return Probe(orchestrator=orchestrator, entry={"address": orchestrator.url, "runners": runners})

    async def _target(self, orchestrator: Orchestrator) -> tuple[str, dict[str, str], dict[str, Any]]:
        """The URL to fetch, with the headers and request extensions that go with it."""
        url = orchestrator.url + "/discovery"
        if not orchestrator.public_only:
            return url, {}, {}
        parts = urlsplit(url)
        host, port = parts.hostname or "", parts.port or (443 if parts.scheme == "https" else 80)
        addresses = await self._resolve(host, port)
        if not addresses:
            raise OSError(f"{host} has no address")
        refused = [address for address in addresses if not self._allowed(address)]
        if refused:
            raise _PrivateAddress(f"{host} resolves to a non-public address")
        literal = f"[{addresses[0]}]" if ":" in addresses[0] else addresses[0]
        pinned = parts._replace(netloc=f"{literal}:{port}").geturl()
        return pinned, {"Host": parts.netloc}, {"sni_hostname": host}  # TLS still verifies the name

    async def _runners(
        self, client: httpx.AsyncClient, url: str, headers: dict[str, str], extensions: dict[str, Any]
    ) -> list[dict[str, Any]]:
        request = client.build_request(
            "GET", url, headers={"Accept": "application/json", **headers}, extensions=extensions
        )
        response = await client.send(request, stream=True)
        async with contextlib.aclosing(response):
            if response.status_code != 200:
                raise ValueError(f"HTTP {response.status_code}")
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body += chunk
                if len(body) > self._max_bytes:
                    raise ValueError(f"inventory larger than {self._max_bytes} bytes")
        entries = json.loads(body)
        if not isinstance(entries, list):
            raise ValueError("inventory is not a JSON list")
        runners: list[dict[str, Any]] = []
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("runners") is None:  # null: a Go nil slice, none
                continue
            if not isinstance(entry["runners"], list):
                raise ValueError(
                    "runners is not a JSON list"
                )  # never an empty inventory that replaces a good one
            runners += [runner for runner in entry["runners"] if isinstance(runner, dict)]
        return runners

    def _excluded(self, orchestrator: Orchestrator, reason: Failure, exc: BaseException) -> Probe:
        _LOG.warning(
            "orchestrator left out of discovery",
            extra={
                "event": "discovery.orchestrator_excluded",
                "orchestrator": orchestrator.url,
                "reason": reason,
                "error": f"{type(exc).__name__}: {exc}"[:200],
            },
        )
        return Probe(orchestrator=orchestrator, failure=reason)


def _untrusted(exc: BaseException) -> bool:
    seen: BaseException | None = exc
    while seen is not None:
        if isinstance(seen, ssl.SSLCertVerificationError):
            return True
        seen = seen.__cause__ or seen.__context__
    return "CERTIFICATE_VERIFY_FAILED" in str(exc)
