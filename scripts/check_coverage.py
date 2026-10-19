"""Per-layer branch-coverage floors (docs/QUALITY_SCORE.md#test-coverage).

Run after ``pytest --cov --cov-report=json``. The total floor lives in pyproject.toml
(``[tool.coverage.report] fail_under``). This script holds the layers that guard money and
correctness to a higher bar, and says exactly which files and lines to cover when one falls short.
"""

import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "src/livepeer_builder/"

# Layer -> minimum branch coverage (%). Layers not listed are held only by the total floor.
FLOORS: dict[str, float] = {
    "services": 95.0,  # business rules: failover, idempotency, provisioning, cost classification
    "store": 95.0,  # persistence, migrations, backup, retention
    "contracts": 95.0,  # the public data contracts
    "adapters": 90.0,  # SDK, Batteries, auth, secrets
    "runtime": 90.0,  # engine wiring and lifecycle
    "http": 90.0,  # REST routes and error mapping
}


def layer_of(path: str) -> str:
    relative = path.split(PACKAGE, 1)[-1]
    head = relative.split("/", 1)[0]
    return head.removesuffix(".py") if "/" in relative else "entry"


def main(report: Path = ROOT / "coverage.json") -> int:
    if not report.exists():
        sys.stderr.write(f"{report} not found: run `uv run pytest --cov --cov-report=json` first\n")
        return 2
    files = json.loads(report.read_text())["files"]
    totals: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    gaps: dict[str, list[tuple[float, str, list[int]]]] = defaultdict(list)
    for path, data in files.items():
        summary = data["summary"]
        covered = summary["covered_lines"] + summary.get("covered_branches", 0)
        total = summary["num_statements"] + summary.get("num_branches", 0)
        layer = layer_of(path)
        totals[layer][0] += covered
        totals[layer][1] += total
        if total and covered < total:
            gaps[layer].append((100.0 * covered / total, path, data.get("missing_lines", [])))
    failed = False
    for layer, floor in FLOORS.items():
        covered, total = totals.get(layer, [0, 0])
        percent = 100.0 * covered / total if total else 100.0
        status = "ok" if percent >= floor else "BELOW FLOOR"
        sys.stdout.write(f"coverage: {layer:<10} {percent:6.2f}% (floor {floor:.0f}%) {status}\n")
        if percent < floor:
            failed = True
            for file_percent, path, missing in sorted(gaps[layer])[:5]:
                lines = ", ".join(str(n) for n in missing[:15]) or "partial branches"
                sys.stderr.write(f"  cover {path} ({file_percent:.0f}%): missing lines {lines}\n")
    if failed:
        sys.stderr.write(
            "Add tests that assert the behavior on those lines (see docs/QUALITY_SCORE.md#test-coverage). "
            "Do not lower a floor to make a change pass.\n"
        )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
