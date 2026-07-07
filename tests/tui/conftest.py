"""TUI test fixtures."""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Callable

import pytest

_TUI_DIR = Path(__file__).parent.resolve()


def pytest_collection_modifyitems(items):
    """Mark every TUI test as known-flaky → auto-rerun via pytest-rerunfailures.

    Textual async-pilot tests are green in their own process (``pytest tests/tui``)
    but time-flake when mixed into the full ~4600-test run: a *different* TUI test
    fails per run due to cumulative process load/scheduler timing, not a fixable
    leak (a deterministic thread-leak hunt found none). Reruns are scoped to
    tests/tui only, so the rest of the suite is untouched.
    """
    flaky = pytest.mark.flaky(reruns=2, reruns_delay=0)
    for item in items:
        try:
            path = Path(str(item.fspath)).resolve()
        except Exception:
            continue
        if _TUI_DIR == path or _TUI_DIR in path.parents:
            item.add_marker(flaky)


async def wait_until(
    pilot,
    cond: Callable[[], bool],
    what: str,
    *,
    steps: int = 200,
    step_s: float = 0.05,
) -> None:
    """Ждать наблюдаемое условие (до ~10 с при дефолтах), не слепой pause."""
    for _ in range(steps):
        if cond():
            return
        await pilot.pause(step_s)
    raise AssertionError(f"timeout: {what}")


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
