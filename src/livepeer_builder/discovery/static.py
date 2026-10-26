"""A fixed list of orchestrators: the simplest ``OrchestratorDirectory``, and a template for your own."""

from pathlib import Path

from livepeer_builder.discovery.directory import Orchestrator, base_url, unique


class StaticDirectory:
    def __init__(self, urls: list[str]) -> None:
        self._orchestrators = unique([Orchestrator(url=base_url(url)) for url in urls])
        if not self._orchestrators:
            raise ValueError("a static orchestrator directory needs at least one orchestrator")

    @classmethod
    def parse(cls, value: str) -> "StaticDirectory":
        """Comma-separated base URLs, or the path of a file with one per line (``#`` starts a comment)."""
        path = Path(value.strip())
        if value.strip() and "://" not in value and path.is_file():
            lines = (line.split("#", 1)[0].strip() for line in path.read_text().splitlines())
            return cls([line for line in lines if line])
        return cls([item for item in value.split(",") if item.strip()] or [value])

    async def orchestrators(self) -> tuple[Orchestrator, ...]:
        return self._orchestrators
