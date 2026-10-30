"""ISS-23: a traceability file cites checks by name (``tests/...::test_name``), so every name it cites must
exist. scripts/check_docs.py runs the check on every traceability file; these pin what it catches."""

import runpy
from collections.abc import Callable
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_docs.py"
Check = Callable[[str], list[str]]


def missing_citations() -> Check:
    check: Check = runpy.run_path(str(SCRIPT))["missing_test_citations"]
    return check


def test_a_cited_test_that_does_not_exist_is_reported() -> None:
    text = "| Revoked | ... | `tests/unit/test_access.py::test_no_such_check` | U | fixture | ISS-23 |"
    assert missing_citations()(text) == ["tests/unit/test_access.py::test_no_such_check"]


def test_a_cited_file_that_does_not_exist_is_reported() -> None:
    assert missing_citations()("`tests/unit/test_nowhere.py::test_x`") == [
        "tests/unit/test_nowhere.py::test_x"
    ]


def test_existing_tests_pass_whether_async_or_parametrized() -> None:
    text = (
        "`tests/unit/test_admin_route_sweep.py::"
        "test_every_admin_route_is_denied_to_actor_and_auditor_keys_and_needs_a_credential`, "
        "`tests/structure/test_traceability.py::test_a_cited_test_that_does_not_exist_is_reported`, "
        "`tests/unit/test_ownership.py::test_a_missing_permission_is_403_before_any_lookup[get]`"
    )
    assert missing_citations()(text) == []
