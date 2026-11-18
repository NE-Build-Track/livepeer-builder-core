"""The upstream-pin checker reads the SHAs this repository actually installs and builds.

A missed pattern would silently stop watching a dependency; a second SHA in compose.yaml would
mean the signer and discovery are not the same node. Failure messages say which file to fix.
"""

from __future__ import annotations

import runpy
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check_upstream.py"
CHECK = runpy.run_path(str(SCRIPT))


class FakeGitHub:
    """In-memory GitHub: maps GET paths, records POST/PATCH, serves issues."""

    def __init__(self) -> None:
        self.gets: dict[str, Any] = {}
        self.missing: set[str] = set()
        self.posted: list[tuple[str, dict[str, Any]]] = []
        self.patched: list[tuple[str, dict[str, Any]]] = []
        self.issues: list[dict[str, Any]] = []
        self.next_number = 1

    def get(
        self,
        path: str,
        params: dict[str, str] | None = None,
    ) -> Any:
        if path.endswith("/issues"):
            return list(self.issues)
        if path in self.missing:
            raise CHECK["GitHubError"]("GET", path, 404, "Not Found")
        assert path in self.gets, f"unexpected GET {path} {params}"
        return self.gets[path]

    def post(
        self,
        path: str,
        body: dict[str, Any],
    ) -> Any:
        self.posted.append((path, body))
        if path.endswith("/issues"):
            issue = {
                "number": self.next_number,
                "title": body["title"],
                "body": body["body"],
                "labels": body.get("labels", []),
            }
            self.next_number += 1
            self.issues.append(issue)
            return issue
        return {}

    def patch(
        self,
        path: str,
        body: dict[str, Any],
    ) -> Any:
        self.patched.append((path, body))
        if "/issues/" in path and not path.endswith("/comments"):
            number = int(path.rsplit("/", 1)[-1])
            for issue in self.issues:
                if issue["number"] == number:
                    issue.update(body)
                    if body.get("state") == "closed":
                        self.issues.remove(issue)
                    break
        return {}


def _pin(key: str, sha: str, occurrences: int = 1) -> Any:
    upstream = next(item for item in CHECK["UPSTREAMS"] if item.key == key)
    return CHECK["Pin"](
        upstream=upstream,
        sha=sha,
        occurrences=occurrences,
    )


def _wired(
    github: FakeGitHub,
    repo: str,
    pin: str,
    head: str,
    ahead: int,
    branch: str = "main",
    release_tag: str | None = None,
    release_sha: str | None = None,
    release_ahead: int = 0,
) -> None:
    github.gets[f"/repos/{repo}"] = {"default_branch": branch}
    github.gets[f"/repos/{repo}/commits/{pin}"] = {"sha": pin}
    github.gets[f"/repos/{repo}/commits/{branch}"] = {"sha": head}
    github.gets[f"/repos/{repo}/compare/{pin}...{head}"] = {"ahead_by": ahead}
    if release_tag is None:
        github.missing.add(f"/repos/{repo}/releases/latest")
        return
    github.gets[f"/repos/{repo}/releases/latest"] = {
        "tag_name": release_tag,
        "html_url": f"https://github.com/{repo}/releases/tag/{release_tag}",
    }
    tag_sha = release_sha or head
    github.gets[f"/repos/{repo}/commits/{release_tag}"] = {"sha": tag_sha}
    github.gets[f"/repos/{repo}/compare/{pin}...{tag_sha}"] = {"ahead_by": release_ahead}


def test_the_workflow_compares_pins_daily_and_on_demand() -> None:
    text = (ROOT / ".github" / "workflows" / "upstream.yml").read_text()
    docs = (ROOT / "docs" / "references" / "upstream-baseline.md").read_text()
    assert "cron:" in text
    assert "workflow_dispatch:" in text
    assert "scripts/check_upstream.py" in text
    assert "UPSTREAM_GITHUB_TOKEN" in text
    assert CHECK["LABEL"] in docs


def test_the_checker_reads_the_sdk_node_and_batteries_pins() -> None:
    pins = {pin.upstream.key: pin for pin in CHECK["load_pins"](ROOT)}
    pyproject = (ROOT / "pyproject.toml").read_text()
    compose = (ROOT / "deploy" / "dev-stack" / "compose.yaml").read_text()
    assert pins["sdk"].sha in pyproject
    assert "livepeer/livepeer-python-gateway" in pins["sdk"].upstream.repo
    assert pins["node"].sha in compose
    assert pins["node"].occurrences == 2  # signer and discovery share the image
    assert pins["node"].upstream.repo == "livepeer/node"
    assert "go-livepeer" not in pins["node"].upstream.repo
    assert pins["batteries"].sha in compose
    assert pins["batteries"].upstream.repo == "livepeer/clearinghouse-batteries"


def test_compose_image_tags_are_the_git_sha_prefix() -> None:
    pins = {pin.upstream.key: pin for pin in CHECK["load_pins"](ROOT)}
    compose = (ROOT / "deploy" / "dev-stack" / "compose.yaml").read_text()
    assert f"node:{pins['node'].sha[:7]}" in compose
    assert f"clearinghouse:{pins['batteries'].sha[:7]}" in compose


def test_upstream_baseline_names_each_pin_sha() -> None:
    pins = CHECK["load_pins"](ROOT)
    baseline = (ROOT / "docs" / "references" / "upstream-baseline.md").read_text()
    missing = [f"{pin.upstream.repo} {pin.sha}" for pin in pins if pin.sha not in baseline]
    assert not missing, f"docs/references/upstream-baseline.md must name the pin SHAs: {missing}"


def test_split_node_shas_fail_loudly(tmp_path: Path) -> None:
    compose = tmp_path / "deploy" / "dev-stack"
    compose.mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text(
        "livepeer-gateway @ git+https://github.com/livepeer/livepeer-python-gateway.git@aaaaaaaa"
    )
    (compose / "compose.yaml").write_text(
        "https://github.com/livepeer/node.git#bbbbbbb\n"
        "https://github.com/livepeer/node.git#ccccccc\n"
        "https://github.com/livepeer/clearinghouse-batteries.git#ddddddd\n"
    )
    with pytest.raises(SystemExit, match="more than one SHA"):
        CHECK["load_pins"](tmp_path)


def test_a_newer_default_branch_is_an_update() -> None:
    github = FakeGitHub()
    pin = _pin("node", "f45d83fd68f73d195dffe8b45f6cac296d023c49")
    _wired(
        github,
        pin.upstream.repo,
        pin.sha,
        head="eee09c41f46d64a34bfd9ed3c9cb649536490f13",
        ahead=12,
        release_tag="v1.0.7",
        release_sha="eee09c41f46d64a34bfd9ed3c9cb649536490f13",
        release_ahead=12,
    )
    report = CHECK["probe"](github, pin)
    assert report.has_update
    assert report.head.ahead == 12
    assert report.release.label == "v1.0.7"


def test_an_older_release_is_not_an_update() -> None:
    """The SDK pin is past PyPI/GitHub v1.0.0; that tag must not open an issue by itself."""
    github = FakeGitHub()
    pin = _pin("sdk", "2017a80c740af5fcb0fef4ee84659af9b07fb90c")
    _wired(
        github,
        pin.upstream.repo,
        pin.sha,
        head=pin.sha,
        ahead=0,
        release_tag="v1.0.0",
        release_sha="44df06157fcdb864e37d971e8caba86b2a7dc92e",
        release_ahead=0,
    )
    report = CHECK["probe"](github, pin)
    assert not report.has_update
    assert report.release is not None
    assert report.release.ahead == 0


def test_issue_body_names_the_pin_the_new_sha_and_the_compare_url() -> None:
    github = FakeGitHub()
    pin = _pin("batteries", "ca09e47c067682fb48c3f15425546fb6aa5b6659")
    head = "fb76150f502574f7d0d649ab2f14bc33b03d60e0"
    _wired(
        github,
        pin.upstream.repo,
        pin.sha,
        head=head,
        ahead=20,
        release_tag="v0.0.3",
        release_sha="03bc9fb5e15b073e68986ec0261e68d7ecd9598b",
        release_ahead=15,
    )
    report = CHECK["probe"](github, pin)
    body = CHECK["issue_body"](report)
    assert pin.sha in body
    assert head in body
    assert "v0.0.3" in body
    assert "03bc9fb5e15b073e68986ec0261e68d7ecd9598b" in body
    assert f"https://github.com/{pin.upstream.repo}/compare/{pin.sha}...{head}" in body
    assert "<!-- upstream-update: livepeer/clearinghouse-batteries -->" in body


def test_notify_opens_one_issue_and_refreshes_it_instead_of_duplicating() -> None:
    github = FakeGitHub()
    pin = _pin("sdk", "2017a80c740af5fcb0fef4ee84659af9b07fb90c")
    _wired(github, pin.upstream.repo, pin.sha, head="7dc2a072e618fc526741ad22642e6034836edcc9", ahead=4)
    report = CHECK["probe"](github, pin)
    first = CHECK["notify"](github, "NE-Build-Track/livepeer-builder-core", [report])
    assert first == ["opened #1 for livepeer/livepeer-python-gateway"]
    assert github.issues[0]["labels"] == [CHECK["LABEL"]]
    second = CHECK["notify"](github, "NE-Build-Track/livepeer-builder-core", [report])
    assert second == ["unchanged #1 for livepeer/livepeer-python-gateway"]
    assert len(github.issues) == 1
    moved = CHECK["Report"](
        pin=report.pin,
        default_branch=report.default_branch,
        head=CHECK["Ref"](
            sha="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            ahead=5,
            label="main HEAD",
            url="https://github.com/livepeer/livepeer-python-gateway/commit/aaaaaaaa",
        ),
        release=report.release,
    )
    third = CHECK["notify"](github, "NE-Build-Track/livepeer-builder-core", [moved])
    assert third == ["updated #1 for livepeer/livepeer-python-gateway"]
    assert len(github.issues) == 1
    assert "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" in github.issues[0]["body"]


def test_notify_closes_the_issue_when_the_pin_has_caught_up() -> None:
    github = FakeGitHub()
    pin = _pin("node", "f45d83fd68f73d195dffe8b45f6cac296d023c49")
    github.issues.append(
        {
            "number": 9,
            "title": "Upstream update: livepeer/node",
            "body": "<!-- upstream-update: livepeer/node -->\n<!-- upstream-fingerprint: head=old release=none -->\n",
        }
    )
    github.next_number = 10
    _wired(github, pin.upstream.repo, pin.sha, head=pin.sha, ahead=0)
    report = CHECK["probe"](github, pin)
    actions = CHECK["notify"](github, "NE-Build-Track/livepeer-builder-core", [report])
    assert actions == ["closed #9 for livepeer/node (pin caught up)"]
    assert github.issues == []
    assert any(path.endswith("/issues/9") and body.get("state") == "closed" for path, body in github.patched)


def test_main_report_only_does_not_open_issues() -> None:
    github = FakeGitHub()
    for item in CHECK["load_pins"](ROOT):
        ahead = 1 if item.upstream.key == "sdk" else 0
        head = "7dc2a072e618fc526741ad22642e6034836edcc9" if ahead else item.sha
        _wired(
            github,
            item.upstream.repo,
            item.sha,
            head=head,
            ahead=ahead,
        )
    assert CHECK["main"]([], github=github) == 0
    assert github.issues == []
    assert github.posted == []
