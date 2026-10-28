"""Migrations must ship inside the package (the livepeer-core prototype lost them when installed)."""

import zipfile
from pathlib import Path

import pytest

from livepeer_builder.store.migrate import MIGRATIONS_DIR, head_revision


def test_migrations_live_inside_the_package() -> None:
    assert MIGRATIONS_DIR.is_relative_to(
        Path(__file__).resolve().parents[2] / "src" / "livepeer_builder"
    ) or ("site-packages" in str(MIGRATIONS_DIR))
    assert (MIGRATIONS_DIR / "env.py").exists()
    assert head_revision() == "0006"


def test_built_wheel_contains_migrations() -> None:
    wheels = sorted(Path(__file__).resolve().parents[2].glob("dist/*.whl"))
    if not wheels:
        pytest.skip("no wheel in dist/ (CI builds one before running this check)")
    names = zipfile.ZipFile(wheels[-1]).namelist()
    assert "livepeer_builder/store/migrations/env.py" in names
    assert any(name.startswith("livepeer_builder/store/migrations/versions/0005") for name in names)
