"""Reading each orchestrator's ``GET /discovery``, the inventory a livepeer-node orchestrator serves.

Only orchestrators whose certificate the deployment's trust store accepts are kept: the engine calls
their runners over verified TLS, so one that is not trusted could never be used. That store is the
system's, as OpenSSL loads it, so ``SSL_CERT_FILE`` / ``SSL_CERT_DIR`` extend it: a deployment that pins
orchestrator certificates for its engine mounts the same pins here. Each answer becomes one
entry under the address we probed (an orchestrator cannot claim another's), with each runner record
kept as the JSON text the orchestrator sent.

A URL the operator did not write (``Orchestrator.public_only``: an on-chain registration) is resolved once,
refused unless every address is public, and fetched from the address that was checked, so a registration
cannot point the service at its own network or a metadata endpoint, nor rebind between check and connect.
One orchestrator's failure, of any kind, is that orchestrator's ``Probe.failure``: it never ends the pass.
"""

import asyncio
import contextlib
import ipaddress
import logging
import socket
import ssl
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict

from livepeer_builder.discovery.directory import Orchestrator
from livepeer_builder.discovery.document import InventoryEntry, RawJson, runner_records
from livepeer_builder.utils import read_capped

_LOG = logging.getLogger(__name__)

Failure = Literal["untrusted_certificate", "private_address", "failed"]
Resolve = Callable[[str, int], Awaitable[list[str]]]


class _PrivateAddress(Exception):
    """A public-only URL resolved to an address inside some network."""


_NAT64 = ipaddress.ip_network("64:ff9b::/96")  # RFC 6052: the last 32 bits are an IPv4 address
_IPV4_COMPATIBLE = ipaddress.ip_network("::/96")  # deprecated, and never a public address


def public_address(address: str) -> bool:
    """True for an address on the public internet: not private, loopback, link-local, site-local, shared
    (CGNAT), reserved, unspecified or multicast. An IPv4 address carried in IPv6 (mapped, NAT64, 6to4) is
    judged as that IPv4 address, so 64:ff9b::a9fe:a9fe is the metadata address it is (#46 review)."""
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address = ipaddress.ip_address(address.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        elif ip in _NAT64:
            ip = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        elif ip.sixtofour is not None:
            ip = ip.sixtofour
        elif ip in _IPV4_COMPATIBLE or ip.is_site_local:
            return False
    return ip.is_global and not ip.is_multicast


async def _resolve(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


class Probe(BaseModel):
    """One orchestrator's answer: an entry, or why there is none."""

    model_config = ConfigDict(frozen=True)

    orchestrator: Orchestrator
    entry: InventoryEntry | None = None
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
        next_address_after_s: float = 1.0,
    ) -> None:
        # the deployment's trust store: system roots plus SSL_CERT_FILE / SSL_CERT_DIR (pinned certificates)
        self._verify = ssl_context or ssl.create_default_context()
        self._timeout_s = timeout_s
        self._max_bytes = max_bytes
        self._concurrency = concurrency
        self._resolve = resolve
        self._allowed = address_allowed
        self._next_address_after_s = next_address_after_s  # happy eyeballs: a silent address is given up

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
                runners = await self._fetch(client, orchestrator)
        except httpx.ConnectError as exc:
            return self._excluded(orchestrator, "untrusted_certificate" if _untrusted(exc) else "failed", exc)
        except _PrivateAddress as exc:
            return self._excluded(orchestrator, "private_address", exc)
        except Exception as exc:  # anything else is this orchestrator's failure; the pass goes on
            return self._excluded(orchestrator, "failed", exc)
        return Probe(
            orchestrator=orchestrator, entry=InventoryEntry(address=orchestrator.url, runners=tuple(runners))
        )

    async def _fetch(self, client: httpx.AsyncClient, orchestrator: Orchestrator) -> list[RawJson]:
        """Each target in order until one connects: a dead first address (often an AAAA) must not fail an
        orchestrator whose next one works, whether it refuses or stays silent. Each address but the last gets
        ``next_address_after_s`` to connect (TLS included), so a silent one cannot use up the probe (#46
        review). An untrusted certificate is an answer, not a dead address."""
        targets = await self._targets(orchestrator)
        quick = httpx.Timeout(self._timeout_s, connect=self._next_address_after_s)
        for url, headers, extensions in targets[:-1]:
            try:
                return await self._runners(client, url, headers, extensions, limits=quick)
            except httpx.ConnectTimeout:
                continue  # not a ConnectError in httpx: a silent address
            except httpx.ConnectError as exc:
                if _untrusted(exc):
                    raise
        return await self._runners(client, *targets[-1])

    async def _targets(self, orchestrator: Orchestrator) -> list[tuple[str, dict[str, str], dict[str, Any]]]:
        """The URLs to try, with the headers and request extensions that go with them. A public-only URL is
        resolved once; every address must be public, and only those addresses are tried."""
        url = orchestrator.url + "/discovery"
        if not orchestrator.public_only:
            return [(url, {}, {})]
        parts = urlsplit(url)
        host, port = parts.hostname or "", parts.port or (443 if parts.scheme == "https" else 80)
        addresses = await self._resolve(host, port)
        if not addresses:
            raise OSError(f"{host} has no address")
        refused = [address for address in addresses if not self._allowed(address)]
        if refused:
            raise _PrivateAddress(f"{host} resolves to a non-public address")
        targets = []
        for address in addresses:
            literal = f"[{address}]" if ":" in address else address
            pinned = parts._replace(netloc=f"{literal}:{port}").geturl()
            targets.append((pinned, {"Host": parts.netloc}, {"sni_hostname": host}))  # TLS verifies the name
        return targets

    async def _runners(
        self,
        client: httpx.AsyncClient,
        url: str,
        headers: dict[str, str],
        extensions: dict[str, Any],
        *,
        limits: httpx.Timeout | None = None,
    ) -> list[RawJson]:
        request = client.build_request(
            "GET",
            url,
            headers={"Accept": "application/json", **headers},
            extensions=extensions,
            timeout=client.timeout if limits is None else limits,
        )
        response = await client.send(request, stream=True)
        async with contextlib.aclosing(response):
            if response.status_code != 200:
                raise ValueError(f"HTTP {response.status_code}")
            body = await read_capped(response.aiter_bytes(), self._max_bytes)
        return runner_records(body)

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
