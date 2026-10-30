"""The knowledge base stays consistent: runs scripts/check_docs.py as part of pytest, and the failure
table covers the contract."""

import runpy
import typing
from pathlib import Path

from livepeer_builder.contracts import FailureKind, PaymentReason

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_docs.py"


def test_docs_are_linked_indexed_and_well_formed() -> None:
    main = runpy.run_path(str(SCRIPT))["main"]
    assert main() == 0, "scripts/check_docs.py reported problems (see stderr for how to fix each one)"


def test_the_failure_table_lists_every_kind_and_reason() -> None:
    """docs/contracts/engine.md#failures is what a builder reads to branch on a failure (D58): every
    ``Failure.kind`` and every ``Failure.reason`` has a row there."""
    engine_md = SCRIPT.parents[1] / "docs" / "contracts" / "engine.md"
    section = engine_md.read_text().split("### Failures", 1)[1].split("\n## ", 1)[0]
    rows = [line for line in section.splitlines() if line.startswith("| `")]
    first_cells = " ".join(row.split("|")[1] for row in rows)
    reason_cells = " ".join(row.split("|")[2] for row in rows)
    missing = [kind for kind in typing.get_args(FailureKind) if f"`{kind}`" not in first_cells]
    missing += [reason for reason in typing.get_args(PaymentReason) if f"`{reason}`" not in reason_cells]
    assert not missing, f"add rows for {missing} to the failure table in docs/contracts/engine.md#failures"


def test_every_failure_row_says_what_it_means_and_what_to_do() -> None:
    """ISS-15 (D59): the table is the builder's answer to "what now?", so no row leaves that cell empty."""
    engine_md = SCRIPT.parents[1] / "docs" / "contracts" / "engine.md"
    section = engine_md.read_text().split("### Failures", 1)[1].split("\n## ", 1)[0]
    header = next(line for line in section.splitlines() if line.startswith("| `Failure.kind`"))
    assert header.rstrip(" |").endswith("What it means · what to do"), "keep the advice in the last column"
    rows = [line for line in section.splitlines() if line.startswith("| `")]
    silent = [
        row.split("|")[1].strip() for row in rows if not row.rstrip().rstrip("|").split("|")[-1].strip()
    ]
    assert not silent, f"say what {silent} means and what to do in docs/contracts/engine.md#failures"
