"""TUI test fixtures."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest


@pytest.fixture(scope="session", autouse=True)
def _copy_raw_snapshots_to_svg():
    """After tests, copy every __snapshots__/**/*.raw to a sibling .svg.

    Lets humans double-click .svg to view in browser (Windows Photos doesn't
    handle SVG-content .raw files).
    """
    yield  # run tests first
    snapshots = Path(__file__).parent / "__snapshots__"
    if not snapshots.exists():
        return
    for raw in snapshots.rglob("*.raw"):
        svg = raw.with_suffix(".svg")
        # Always overwrite — keeps .svg in sync with .raw
        shutil.copy2(raw, svg)
