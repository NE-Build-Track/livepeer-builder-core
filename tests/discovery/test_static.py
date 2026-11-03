"""The static directory: a configured list of orchestrator base URLs (#45)."""

from pathlib import Path

import pytest

from livepeer_builder.discovery import Orchestrator, StaticDirectory


async def test_a_comma_list_becomes_orchestrators_normalized_deduplicated_in_order() -> None:
    directory = StaticDirectory.parse(
        " https://b.example:8935/ ,https://a.example:8935,https://b.example:8935"
    )
    assert await directory.orchestrators() == (
        Orchestrator(url="https://b.example:8935"),
        Orchestrator(url="https://a.example:8935"),
    )


async def test_a_file_lists_one_url_per_line_skipping_blanks_and_comments(tmp_path: Path) -> None:
    listed = tmp_path / "orchestrators.txt"
    listed.write_text(
        "# approved\nhttps://a.example:8935\n\n  https://b.example:8936/path  # a deployment path\n"
    )
    directory = StaticDirectory.parse(str(listed))
    assert [o.url for o in await directory.orchestrators()] == [
        "https://a.example:8935",
        "https://b.example:8936/path",
    ]


@pytest.mark.parametrize(
    "bad", ["orch.example:8935", "ftp://orch.example", "https://user:pw@orch.example", "https://", ""]
)
def test_an_invalid_url_is_refused_when_the_directory_is_built(bad: str) -> None:
    with pytest.raises(ValueError, match="orchestrator"):
        StaticDirectory.parse(bad or ",")
