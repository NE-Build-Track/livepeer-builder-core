"""The directory port: which orchestrators are eligible. Everything after it (probing, TLS, caching,
serving) is shared, so a directory is all an operator writes to change who is discovered (#45)."""

from typing import Protocol
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict


class Orchestrator(BaseModel):
    """An orchestrator's HTTP(S) base URL, and its Ethereum address when the directory knows it."""

    model_config = ConfigDict(frozen=True)

    url: str
    address: str | None = None


class OrchestratorDirectory(Protocol):
    async def orchestrators(self) -> tuple[Orchestrator, ...]:
        """The eligible orchestrators, in a stable order. Raise when the answer is unknown (the catalog
        then keeps the last directory it had)."""
        ...


def base_url(raw: str) -> str:
    """A normalized orchestrator base URL: http(s), a host, no credentials or fragment, no trailing slash."""
    candidate = raw.strip()
    parts = urlsplit(candidate)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        raise ValueError(f"not an orchestrator base URL: {candidate!r} (want http(s)://host[:port][/path])")
    if parts.fragment or parts.query:
        raise ValueError(f"an orchestrator base URL has no query or fragment: {candidate!r}")
    return candidate.rstrip("/")


def unique(orchestrators: list[Orchestrator]) -> tuple[Orchestrator, ...]:
    """The first of each URL, in order."""
    seen: set[str] = set()
    kept: list[Orchestrator] = []
    for orchestrator in orchestrators:
        if orchestrator.url not in seen:
            seen.add(orchestrator.url)
            kept.append(orchestrator)
    return tuple(kept)
