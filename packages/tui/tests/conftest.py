"""Фикстуры и хуки TUI-тестов.

Импортируемые хелперы (wait_until, open_settings_section) живут в
``gc_testkit.tui_support``: conftest не импортируют из тестов напрямую.
"""
from __future__ import annotations

import shutil
from pathlib import Path

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


@pytest.fixture(autouse=True)
def _isolate_agent_state(tmp_path, monkeypatch):
    """I3: TraceScreen._maybe_toast_warnings (и другой upgrade_gate-код) читает/пишет
    agent_state.json на КАЖДОМ буте TraceScreen, включая любой `TuiApp(...).run_test()` —
    без редиректа тесты гадят в реальный `~/.gc/agent_state.json`. Редиректим на tmp_path
    для всех TUI-тестов; локальный monkeypatch внутри теста (если есть) просто
    переопределяет этот же путь позже — конфликта нет."""
    from gc_core import upgrade_gate
    state_path = tmp_path / "agent_state.json"
    monkeypatch.setattr(upgrade_gate, "_state_path", lambda: state_path)


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


@pytest.fixture
def tui_app_factory():
    """Мини-хост с реальными стилями приложения — для проверки CSS-контракта.

    styles.tcss ссылается на кастомные переменные ($bg, $ink, ...), которые
    в проде инжектит TuiApp.get_css_variables() из gc_tui.palette — без этого
    переопределения обычный App падает на парсинге стилшита
    (UnresolvedVariableError), поэтому мини-хост его тоже переопределяет.

    Путь к стилям берётся из gc_testkit.paths.TUI_PKG, а не считается от
    __file__: набор TUI лежит в packages/tui/tests, а стили — в
    packages/tui/src/gc_tui, и арифметика уровней здесь уже ломалась.
    """
    from textual.app import App, ComposeResult

    from gc_testkit.paths import TUI_PKG
    from gc_tui import palette

    def _factory(widget):
        class _StyledHost(App):
            CSS_PATH = str(TUI_PKG / "styles.tcss")

            def get_css_variables(self) -> dict:
                variables = super().get_css_variables()
                variables.update(palette.css_variables())
                return variables

            def compose(self) -> ComposeResult:
                yield widget

        return _StyledHost()

    return _factory
