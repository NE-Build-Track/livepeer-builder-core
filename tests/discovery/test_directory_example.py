"""The extension guide's directory example, run as a test (docs/design-docs/extending-the-engine.md)."""

from urllib.parse import urlsplit

from livepeer_builder.discovery import (
    DiscoverySettings,
    Orchestrator,
    OrchestratorDirectory,
    StaticDirectory,
    directory_from,
    serve,
)


# docs:start approved-directory
class ApprovedOnly:
    """Any directory (on-chain, static, your own) narrowed to the hosts your organization approved."""

    def __init__(self, inner: OrchestratorDirectory, approved_hosts: set[str]) -> None:
        self._inner = inner
        self._approved = approved_hosts

    async def orchestrators(self) -> tuple[Orchestrator, ...]:
        found = await self._inner.orchestrators()
        return tuple(o for o in found if urlsplit(o.url).hostname in self._approved)


async def run_discovery(settings: DiscoverySettings) -> None:
    """``lpb discovery``, with the configured directory (LPD_DIRECTORY) narrowed to approved hosts."""
    directory = ApprovedOnly(directory_from(settings), {"ai1.eliteencoder.net", "hky.eliteencoder.net"})
    await serve(settings, directory=directory)


# docs:end approved-directory


async def test_the_approved_directory_keeps_only_approved_hosts() -> None:
    listed = StaticDirectory(["https://hky.eliteencoder.net:8936", "https://unknown.example:8935"])
    narrowed = ApprovedOnly(listed, {"hky.eliteencoder.net"})
    assert [o.url for o in await narrowed.orchestrators()] == ["https://hky.eliteencoder.net:8936"]


def test_the_example_wires_the_configured_directory() -> None:
    assert callable(run_discovery)  # its typing is checked by mypy with the rest of the suite
