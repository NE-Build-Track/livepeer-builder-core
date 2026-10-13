"""StaticDiscoverySource: a fixed runner list. Used when no signer is configured, and in tests."""

from collections.abc import Iterable

from livepeer_builder.contracts import Runner


class StaticDiscoverySource:
    def __init__(self, runners: Iterable[Runner] = ()) -> None:
        self.runners = tuple(runners)

    async def fetch(self) -> tuple[Runner, ...]:
        return self.runners
