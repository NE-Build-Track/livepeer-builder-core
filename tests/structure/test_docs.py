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


def test_a_failure_row_that_may_follow_failover_ties_a_repeat_to_the_closed_list() -> None:
    """A job's failure is its last attempt's. After failover an earlier attempt may be ``unreachable`` or an
    unpaid ``timeout``, and then the same reference is 409, whatever the last kind (D57, #81 review). So a
    row past dispatch that promises "the same ``operation_ref`` runs again" must point at the closed list."""
    engine_md = SCRIPT.parents[1] / "docs" / "contracts" / "engine.md"
    section = engine_md.read_text().split("### Failures", 1)[1].split("\n## ", 1)[0]
    before_dispatch = ("`no_offering`", "`no_capacity`", "`not_provisioned`")
    rows = [line for line in section.splitlines() if line.startswith("| `")]
    loose = [
        row.split("|")[1].strip() + " " + row.split("|")[2].strip()
        for row in rows
        if not row.split("|")[1].strip().startswith(before_dispatch)
        and (
            ("runs again" in row and "same `operation_ref`" in row) or "with the same `operation_ref`" in row
        )
        and "[the closed list](#idempotency)" not in row
    ]
    assert not loose, f"tie the repeat advice of {loose} to the closed list (engine.md#idempotency)"


def test_the_runner_error_row_keeps_a_paid_502_or_504_uncertain() -> None:
    """D52: a paid 500 ran and is ``failed_after_payment``; a paid 502 or 504 may still be running behind
    the proxy and stays ``uncertain`` until usage settles it (#81 review)."""
    engine_md = SCRIPT.parents[1] / "docs" / "contracts" / "engine.md"
    row = next(line for line in engine_md.read_text().splitlines() if line.startswith("| `runner_error`"))
    advice = row.split("|")[-2]
    assert "502" in advice and "504" in advice and "`uncertain`" in advice
    # Every runner_error answers HTTP 502, so the split is the runner's own status, and the job's state
    # says whether to wait for the cost (#81 review)
    assert "`failure.status_code`" in advice and "`job.state`" in advice


def test_the_not_authorized_row_says_what_happens_once_the_key_is_fixed() -> None:
    """An unpaid signer refusal is on the closed list: once an operator fixes the key, the same reference
    runs again, unless an earlier attempt was unreachable or an unpaid timeout (#81 review)."""
    engine_md = SCRIPT.parents[1] / "docs" / "contracts" / "engine.md"
    row = next(
        line
        for line in engine_md.read_text().splitlines()
        if line.startswith("| `payment` | `not_authorized`")
    )
    advice = row.split("|")[-2]
    assert "retrying does not help" not in advice and "never reached a runner" in advice


def test_a_fixed_length_tuple_keeps_every_element_in_the_generated_docs() -> None:
    """``tuple[A, B]`` rendered as ``A[]`` hid ``JobCost`` and ``list[Job]`` from a custom store's author
    (#82 review). A fixed tuple shows each element in Mermaid's generic notation; a ``tuple[X, ...]``
    and a list stay ``X[]``."""
    from livepeer_builder.contracts import Job, JobCost
    from livepeer_builder.docs_diagrams import type_name

    assert type_name(tuple[list[tuple[str, JobCost]], list[Job]]) == "tuple~tuple~str JobCost~[] Job[]~"
    assert type_name(tuple[str, ...]) == type_name(list[str]) == "str[]"
    assert "," not in type_name(tuple[str, int, JobCost])
