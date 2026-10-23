"""Keep the knowledge base honest (AGENTS.md). Run: ``uv run python scripts/check_docs.py``.

Checks:
1. Every relative Markdown link resolves to a file, and every ``#anchor`` to a heading.
2. Every doc under docs/ is reachable from AGENTS.md or docs/index.md (no orphans).
3. Exec plans have the required sections, in order (docs/PLANS.md).
4. Doc references in source code (``docs/...md#anchor``, ``ARCHITECTURE.md#anchor``) resolve.
5. Mermaid blocks contain no ``;`` (Mermaid treats it as a statement separator and fails to render).
6. A code block marked ``<!-- example: path#name -->`` equals the region between ``# docs:start name``
   and ``# docs:end name`` in that file, so documented examples are the tested ones.
Each failure says how to fix it.
"""

import re
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)\)")
CODE_REF = re.compile(r"(?<![\w./-])((?:docs/[\w./-]+|ARCHITECTURE)\.md)(#[\w-]+)?")
FENCE = re.compile(r"^```.*?^```", re.MULTILINE | re.DOTALL)
PLAN_SECTIONS = ["## Status", "## Goal", "## Acceptance", "## Progress", "## Decision log"]
ENTRY_POINTS = [ROOT / "AGENTS.md", ROOT / "docs" / "index.md"]


def slug(heading: str) -> str:
    text = heading.strip().lower().replace("`", "")
    text = re.sub(r"[^\w\s-]", "", text)
    return re.sub(r"\s", "-", text)


def anchors(path: Path) -> set[str]:
    body = FENCE.sub("", path.read_text())
    return {slug(m.group(1)) for m in re.finditer(r"^#{1,6}\s+(.+?)\s*$", body, re.MULTILINE)}


def markdown_files() -> list[Path]:
    files = list(ROOT.glob("*.md"))
    files += [p for p in (ROOT / "docs").rglob("*.md")]
    files += [p for p in (ROOT / "deploy").rglob("*.md")]
    return sorted(files)


def links(path: Path) -> list[str]:
    return LINK.findall(FENCE.sub("", path.read_text()))


def resolve(source: Path, target: str) -> tuple[Path | None, str | None]:
    file_part, _, anchor = target.partition("#")
    path = source if not file_part else (source.parent / file_part).resolve()
    return path, anchor or None


def check_links(errors: list[str]) -> dict[Path, set[Path]]:
    graph: dict[Path, set[Path]] = {}
    for doc in markdown_files():
        graph[doc] = set()
        for target in links(doc):
            if re.match(r"^[a-z][a-z0-9+.-]*:", target):
                continue  # external URL
            path, anchor = resolve(doc, target)
            assert path is not None
            where = f"{doc.relative_to(ROOT)} -> {target}"
            if not path.exists():
                errors.append(f"broken link {where}: create the file or fix the path")
                continue
            if path.is_file() and path.suffix == ".md":
                graph[doc].add(path)
                if anchor and anchor not in anchors(path):
                    errors.append(f"missing anchor {where}: add a '## …' heading or fix the anchor")
            elif path.is_dir():
                graph[doc].update(p for p in path.glob("*.md"))
    return graph


def check_reachability(graph: dict[Path, set[Path]], errors: list[str]) -> None:
    seen: set[Path] = set()
    stack = [p for p in ENTRY_POINTS if p.exists()]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        stack.extend(graph.get(current, ()))
    for doc in (ROOT / "docs").rglob("*.md"):
        if doc not in seen:
            errors.append(f"orphan doc {doc.relative_to(ROOT)}: link it from the nearest index.md")


def check_plans(errors: list[str]) -> None:
    for plan in sorted((ROOT / "docs" / "exec-plans").glob("*/*.md")):
        text = plan.read_text()
        positions = [text.find(section + "\n") for section in PLAN_SECTIONS]
        if -1 in positions or positions != sorted(positions):
            required = ", ".join(PLAN_SECTIONS)
            errors.append(
                f"exec plan {plan.relative_to(ROOT)} needs sections in order: {required} (docs/PLANS.md)"
            )


def check_code_references(errors: list[str]) -> None:
    for source in sorted((ROOT / "src").rglob("*.py")):
        for match in CODE_REF.finditer(source.read_text()):
            target = ROOT / match.group(1)
            anchor = (match.group(2) or "").lstrip("#")
            where = f"{source.relative_to(ROOT)} references {match.group(0)}"
            if not target.exists():
                errors.append(f"{where}, which does not exist: fix the reference or add the doc")
            elif anchor and anchor not in anchors(target):
                errors.append(f"{where}, but that heading is missing: add it or fix the reference")


MERMAID = re.compile(r"^```mermaid\n(.*?)^```", re.MULTILINE | re.DOTALL)


def check_mermaid(errors: list[str]) -> None:
    for doc in markdown_files():
        for block in MERMAID.findall(doc.read_text()):
            for line in block.splitlines():
                if ";" in line:
                    errors.append(
                        f"{doc.relative_to(ROOT)}: mermaid line '{line.strip()[:60]}' contains ';', "
                        "which ends a Mermaid statement. Use ',' instead"
                    )


EXAMPLE = re.compile(r"<!-- example: ([\w./-]+)#([\w-]+) -->\n```python\n(.*?)^```", re.MULTILINE | re.DOTALL)


def example_region(source: Path, name: str) -> str | None:
    lines = source.read_text().splitlines()
    start, end = f"# docs:start {name}", f"# docs:end {name}"
    try:
        first = next(i for i, line in enumerate(lines) if line.strip() == start)
        last = next(i for i, line in enumerate(lines) if line.strip() == end and i > first)
    except StopIteration:
        return None
    return textwrap.dedent("\n".join(lines[first + 1 : last])).strip()


def check_examples(errors: list[str]) -> None:
    for doc in markdown_files():
        for path, name, block in EXAMPLE.findall(doc.read_text()):
            source = ROOT / path
            where = f"{doc.relative_to(ROOT)} example {path}#{name}"
            region = example_region(source, name) if source.exists() else None
            if region is None:
                errors.append(f"{where}: no '# docs:start {name}' / '# docs:end {name}' region in {path}")
            elif textwrap.dedent(block).strip() != region:
                errors.append(f"{where}: the block differs from the tested region; copy the region in")


def main() -> int:
    errors: list[str] = []
    graph = check_links(errors)
    check_reachability(graph, errors)
    check_plans(errors)
    check_code_references(errors)
    check_mermaid(errors)
    check_examples(errors)
    for error in errors:
        sys.stderr.write(f"docs: {error}\n")
    if not errors:
        sys.stdout.write(f"docs: ok ({len(graph)} markdown files)\n")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
