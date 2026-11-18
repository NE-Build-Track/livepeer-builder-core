"""Watch the SDK, livepeer/node and Batteries pins for newer default-branch commits and releases.

Pins are read from ``pyproject.toml`` (the SDK git SHA) and ``deploy/dev-stack/compose.yaml``
(node and Batteries git SHAs). Dependabot does not understand those git build contexts, so this
script is the check. The scheduled workflow opens or updates one ``upstream-update`` issue per
dependency that has moved, and refreshes that issue instead of opening another.

    python scripts/check_upstream.py
    python scripts/check_upstream.py --notify   # GITHUB_TOKEN + GITHUB_REPOSITORY
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
API = "https://api.github.com"
LABEL = "upstream-update"
LABEL_COLOR = "fbca04"
LABEL_DESCRIPTION = "An upstream pin (SDK, node, or Batteries) has a newer commit or release"
USER_AGENT = "livepeer-builder-upstream-check"
MARKER = "<!-- upstream-update: {repo} -->"
FINGERPRINT = "<!-- upstream-fingerprint: {value} -->"
TITLE = "Upstream update: {repo}"

RequestFn = Callable[[str, str, dict[str, str] | None, Any | None], Any]


class GitHubError(RuntimeError):
    def __init__(
        self,
        method: str,
        path: str,
        status: int,
        body: str,
    ) -> None:
        self.method = method
        self.path = path
        self.status = status
        super().__init__(f"{method} {path} -> {status}: {body[:300]}")


@dataclass(frozen=True)
class Upstream:
    key: str
    repo: str
    title: str
    pin_file: str
    pin_pattern: str
    why: str


# The node this engine pays is livepeer/node (D2), not go-livepeer. Each pattern captures the SHA
# the compose file or pyproject.toml actually builds or installs.
UPSTREAMS: tuple[Upstream, ...] = (
    Upstream(
        key="sdk",
        repo="livepeer/livepeer-python-gateway",
        title="Python gateway SDK",
        pin_file="pyproject.toml",
        pin_pattern=(
            r"livepeer-gateway @ git\+https://github.com/livepeer/livepeer-python-gateway\.git"
            r"@([0-9a-f]{7,40})"
        ),
        why="Installed by git SHA (PyPI 1.0.0 lacks on_payment_session; tech debt T1)",
    ),
    Upstream(
        key="node",
        repo="livepeer/node",
        title="livepeer/node (signer and discovery)",
        pin_file="deploy/dev-stack/compose.yaml",
        pin_pattern=r"https://github.com/livepeer/node\.git#([0-9a-f]{7,40})",
        why="Dev-stack signer and livepeer-discovery; not go-livepeer (D2)",
    ),
    Upstream(
        key="batteries",
        repo="livepeer/clearinghouse-batteries",
        title="Clearinghouse Batteries",
        pin_file="deploy/dev-stack/compose.yaml",
        pin_pattern=r"https://github.com/livepeer/clearinghouse-batteries\.git#([0-9a-f]{7,40})",
        why="Dev-stack clearinghouse image, built from the git SHA",
    ),
)


@dataclass(frozen=True)
class Pin:
    upstream: Upstream
    sha: str
    occurrences: int


@dataclass(frozen=True)
class Ref:
    sha: str
    ahead: int
    label: str
    url: str


@dataclass(frozen=True)
class Report:
    pin: Pin
    default_branch: str
    head: Ref
    release: Ref | None

    @property
    def repo(self) -> str:
        return self.pin.upstream.repo

    @property
    def has_update(self) -> bool:
        if self.head.ahead > 0:
            return True
        return self.release is not None and self.release.ahead > 0

    @property
    def fingerprint(self) -> str:
        release = self.release.sha if self.release and self.release.ahead > 0 else "none"
        return f"head={self.head.sha}|release={release}"


class GitHub:
    def __init__(
        self,
        token: str | None = None,
        request: RequestFn | None = None,
    ) -> None:
        self._token = token
        self._request = request or self._http

    def get(
        self,
        path: str,
        params: dict[str, str] | None = None,
    ) -> Any:
        return self._request("GET", path, params, None)

    def post(
        self,
        path: str,
        body: Mapping[str, Any],
    ) -> Any:
        return self._request("POST", path, None, body)

    def patch(
        self,
        path: str,
        body: Mapping[str, Any],
    ) -> Any:
        return self._request("PATCH", path, None, body)

    def _http(
        self,
        method: str,
        path: str,
        params: dict[str, str] | None,
        body: Any | None,
    ) -> Any:
        if not path.startswith("/"):
            raise ValueError(f"GitHub path must be absolute: {path!r}")
        url = API + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": USER_AGENT,
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https" or parsed.netloc != "api.github.com":
            raise ValueError(f"refusing to open {url}")
        request = urllib.request.Request(  # noqa: S310
            url,
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
                raw = response.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise GitHubError(
                method=method,
                path=path,
                status=exc.code,
                body=detail,
            ) from None


def load_pins(root: Path = ROOT) -> list[Pin]:
    pins: list[Pin] = []
    for upstream in UPSTREAMS:
        path = root / upstream.pin_file
        text = path.read_text()
        found = re.findall(upstream.pin_pattern, text)
        if not found:
            raise SystemExit(f"{upstream.pin_file} has no {upstream.repo} git SHA: {upstream.pin_pattern}")
        unique = set(found)
        if len(unique) != 1:
            raise SystemExit(
                f"{upstream.pin_file} pins {upstream.repo} to more than one SHA: {sorted(unique)}"
            )
        (sha,) = unique
        pins.append(
            Pin(
                upstream=upstream,
                sha=sha,
                occurrences=len(found),
            )
        )
    return pins


def _ref(
    github: GitHub,
    repo: str,
    pin_sha: str,
    target_sha: str,
    label: str,
    url: str,
) -> Ref:
    compare = github.get(f"/repos/{repo}/compare/{pin_sha}...{target_sha}")
    return Ref(
        sha=target_sha,
        ahead=int(compare["ahead_by"]),
        label=label,
        url=url,
    )


def probe(github: GitHub, pin: Pin) -> Report:
    repo = pin.upstream.repo
    info = github.get(f"/repos/{repo}")
    default_branch = str(info["default_branch"])
    pin_sha = str(github.get(f"/repos/{repo}/commits/{pin.sha}")["sha"])
    head_sha = str(github.get(f"/repos/{repo}/commits/{default_branch}")["sha"])
    head = _ref(
        github,
        repo,
        pin_sha,
        head_sha,
        f"{default_branch} HEAD",
        f"https://github.com/{repo}/commit/{head_sha}",
    )
    release: Ref | None = None
    try:
        latest = github.get(f"/repos/{repo}/releases/latest")
    except GitHubError as exc:
        if exc.status != 404:
            raise
    else:
        tag = str(latest["tag_name"])
        tag_sha = str(github.get(f"/repos/{repo}/commits/{tag}")["sha"])
        release = _ref(
            github,
            repo,
            pin_sha,
            tag_sha,
            tag,
            str(latest.get("html_url") or f"https://github.com/{repo}/releases/tag/{tag}"),
        )
    return Report(
        pin=Pin(
            upstream=pin.upstream,
            sha=pin_sha,
            occurrences=pin.occurrences,
        ),
        default_branch=default_branch,
        head=head,
        release=release,
    )


def compare_url(report: Report) -> str:
    return f"https://github.com/{report.repo}/compare/{report.pin.sha}...{report.head.sha}"


def issue_title(report: Report) -> str:
    return TITLE.format(repo=report.repo)


def issue_body(report: Report) -> str:
    pin = report.pin
    release_row = "_none_"
    if report.release is not None:
        rel = report.release
        release_row = f"[{rel.label}]({rel.url}) `{rel.sha}` ({rel.ahead} commit(s) ahead of the pin)"
    return "\n".join(
        [
            MARKER.format(repo=report.repo),
            FINGERPRINT.format(value=report.fingerprint),
            "",
            f"{pin.upstream.title} (`{report.repo}`) has moved past this repository's pin.",
            "",
            f"- **Pinned in:** `{pin.upstream.pin_file}`",
            f"- **Why it is pinned this way:** {pin.upstream.why}",
            f"- **Current pin:** `{pin.sha}`",
            f"- **Default branch (`{report.default_branch}`):** [`{report.head.sha}`]({report.head.url})"
            f" ({report.head.ahead} commit(s) ahead)",
            f"- **Latest release:** {release_row}",
            f"- **Compare:** {compare_url(report)}",
            "",
            "This is a notification only. Do not bump the pin in this issue: a pin change needs a",
            "reviewed PR (adapter tests, and for node the discovery fixture). Close the issue when",
            "the pin catches up; this check will reopen or refresh it if upstream moves again.",
            "",
        ]
    )


def _marker_in(body: str, repo: str) -> bool:
    return MARKER.format(repo=repo) in (body or "")


def _fingerprint_in(body: str) -> str | None:
    match = re.search(r"<!-- upstream-fingerprint: (.+?) -->", body or "")
    return match.group(1) if match else None


def _open_issues(github: GitHub, repository: str) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    page = 1
    while page <= 10:
        batch = github.get(
            f"/repos/{repository}/issues",
            {"state": "open", "per_page": "100", "page": str(page)},
        )
        if not isinstance(batch, list) or not batch:
            break
        issues.extend(item for item in batch if "pull_request" not in item)
        if len(batch) < 100:
            break
        page += 1
    return issues


def _ensure_label(github: GitHub, repository: str) -> None:
    try:
        github.post(
            f"/repos/{repository}/labels",
            {
                "name": LABEL,
                "color": LABEL_COLOR,
                "description": LABEL_DESCRIPTION,
            },
        )
    except GitHubError as exc:
        if exc.status not in {409, 422}:
            raise


def notify(
    github: GitHub,
    repository: str,
    reports: Sequence[Report],
) -> list[str]:
    """Open, update or close one tracking issue per dependency. Returns one line per action."""
    _ensure_label(github, repository)
    open_issues = _open_issues(github, repository)
    actions: list[str] = []
    for report in reports:
        existing = next(
            (issue for issue in open_issues if _marker_in(str(issue.get("body") or ""), report.repo)),
            None,
        )
        if not report.has_update:
            if existing is not None:
                number = int(existing["number"])
                github.post(
                    f"/repos/{repository}/issues/{number}/comments",
                    {"body": f"The pin now matches `{report.pin.sha}` (`{report.default_branch}`). Closing."},
                )
                github.patch(
                    f"/repos/{repository}/issues/{number}",
                    {"state": "closed", "state_reason": "completed"},
                )
                actions.append(f"closed #{number} for {report.repo} (pin caught up)")
            else:
                actions.append(f"ok {report.repo} at {report.pin.sha}")
            continue
        body = issue_body(report)
        title = issue_title(report)
        if existing is None:
            created = github.post(
                f"/repos/{repository}/issues",
                {"title": title, "body": body, "labels": [LABEL]},
            )
            actions.append(f"opened #{created['number']} for {report.repo}")
            continue
        number = int(existing["number"])
        if _fingerprint_in(str(existing.get("body") or "")) == report.fingerprint:
            actions.append(f"unchanged #{number} for {report.repo}")
            continue
        github.patch(
            f"/repos/{repository}/issues/{number}",
            {"title": title, "body": body, "labels": [LABEL]},
        )
        github.post(
            f"/repos/{repository}/issues/{number}/comments",
            {
                "body": (
                    f"Upstream moved again. Default branch is now `{report.head.sha}`"
                    f" ({report.head.ahead} commit(s) ahead). Compare: {compare_url(report)}"
                )
            },
        )
        actions.append(f"updated #{number} for {report.repo}")
    return actions


def render(reports: Sequence[Report], actions: Sequence[str] | None = None) -> str:
    lines = [
        "| Dependency | Pin | Default branch | Ahead | Latest release | Update |",
        "| --- | --- | --- | ---: | --- | --- |",
    ]
    for report in reports:
        release = report.release.label if report.release else "—"
        if report.release is not None and report.release.ahead > 0:
            release += f" (+{report.release.ahead})"
        lines.append(
            "| "
            + " | ".join(
                [
                    report.repo,
                    f"`{report.pin.sha[:12]}`",
                    f"`{report.head.sha[:12]}`",
                    str(report.head.ahead),
                    release,
                    "yes" if report.has_update else "no",
                ]
            )
            + " |"
        )
    if actions:
        lines.append("")
        lines.extend(f"- {action}" for action in actions)
    return "\n".join(lines) + "\n"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--notify",
        action="store_true",
        help="Open or update one tracking issue per dependency that has moved",
    )
    parser.add_argument(
        "--repository",
        default=os.environ.get("GITHUB_REPOSITORY", ""),
        help="owner/name that receives tracking issues (default: GITHUB_REPOSITORY)",
    )
    return parser.parse_args(argv)


def token_from_env(env: Mapping[str, str] | None = None) -> str | None:
    """GITHUB_TOKEN for public upstreams. UPSTREAM_GITHUB_TOKEN if a private upstream needs a PAT."""
    source = os.environ if env is None else env
    return source.get("UPSTREAM_GITHUB_TOKEN") or source.get("GITHUB_TOKEN") or None


def main(argv: Sequence[str] | None = None, github: GitHub | None = None) -> int:
    args = parse_args(argv)
    pins = load_pins()
    client = github or GitHub(
        token=token_from_env(),
    )
    reports = [probe(client, pin) for pin in pins]
    actions: list[str] | None = None
    if args.notify:
        if not args.repository:
            sys.stderr.write("check_upstream: --notify needs --repository or GITHUB_REPOSITORY\n")
            return 2
        if token_from_env() is None and github is None:
            sys.stderr.write("check_upstream: --notify needs GITHUB_TOKEN or UPSTREAM_GITHUB_TOKEN\n")
            return 2
        actions = notify(client, args.repository, reports)
    text = render(reports, actions)
    sys.stdout.write(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with Path(summary).open("a", encoding="utf-8") as handle:
            handle.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
