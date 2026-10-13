"""The knowledge base stays consistent: runs scripts/check_docs.py as part of pytest."""

import runpy
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_docs.py"


def test_docs_are_linked_indexed_and_well_formed() -> None:
    main = runpy.run_path(str(SCRIPT))["main"]
    assert main() == 0, "scripts/check_docs.py reported problems (see stderr for how to fix each one)"
