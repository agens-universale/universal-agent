"""Патч выделения Textual и протяжка ниже ленты (спека P3 §2.7).

Стенды `repro.py` и `repro2.py` (решения захода TUI 2026-10-20) — здесь
pilot-тестами. Страж версии падает при обновлении Textual.
"""

from __future__ import annotations

import json
from pathlib import Path

from gc_testkit.tui_support import tui_settings, wait_until
import pytest
import textual
from textual import selection
from textual.app import App
from textual.containers import VerticalScroll
from textual.geometry import Offset
from textual.widgets import Static

from gc_tui import textual_compat
from gc_tui.textual_compat import (
    PATCHED_TEXTUAL,
    apply_textual_patches,
    auto_scroll_below,
    keep_select_end_in,
)


@pytest.fixture(autouse=True)
def _pilot_mouse_deltas(monkeypatch):
    """Пилот шлёт движения мыши без сдвига, терминал — со сдвигом от
    прошлой точки: без него не отличить вертикальное движение от
    горизонтального (`check_auto_scroll`)."""
    from textual import pilot

    original = pilot._get_mouse_message_arguments
    last: list = []

    def with_deltas(*args, **kwargs):
        out = original(*args, **kwargs)
        if last:
            out["delta_x"] = out["x"] - last[0][0]
            out["delta_y"] = out["y"] - last[0][1]
        last[:] = [(out["x"], out["y"])]
        return out

    monkeypatch.setattr(pilot, "_get_mouse_message_arguments", with_deltas)


def test_textual_version_guard():
    assert textual.__version__ == PATCHED_TEXTUAL, (
        "Textual обновлён — пересмотреть textual_compat (патч знака "
        "SelectStart и протяжку ниже ленты), стенд repro.py"
    )


def _upstream_pointer_start_offset(self) -> Offset:
    """Формула Textual 8.2.8 как есть — контроль стенда."""
    return (
        self.container.region.offset
        + self.container_pointer_delta
        + (self.container.scroll_offset - self.container_initial_scroll_offset)
    )


class _Lines(App):
    CSS = "#t {height: 20;} Static {height: 1;} #f {height: 3;}"

    def compose(self):
        with VerticalScroll(id="t"):
            for i in range(100):
                yield Static(f"line {i:03d}")
        yield Static("FOOTER", id="f")


def _selected(app) -> list[str]:
    return (app.screen.get_selected_text() or "").splitlines()


async def _first_lines_while_scrolling() -> list[str]:
    """Стенд repro.py: выделение со 2-й строки, прокрутка под ним."""
    app = _Lines()
    firsts = []
    async with app.run_test(size=(40, 30)) as pilot:
        trace = app.query_one("#t")
        await pilot.mouse_down("#t", offset=(0, 2))
        await pilot.hover("#t", offset=(5, 10))
        await pilot.pause()
        for step in (4, 8, 12, 20, 40):
            trace.scroll_to(y=step, animate=False)
            await pilot.pause()
            app.screen._update_select()
            await pilot.pause()
            firsts.append(_selected(app)[0])
    return firsts


@pytest.mark.asyncio
async def test_patched_selection_start_stays_put():
    assert apply_textual_patches() is True
    assert await _first_lines_while_scrolling() == ["line 002"] * 5


@pytest.mark.asyncio
async def test_upstream_formula_drifts(monkeypatch):
    monkeypatch.setattr(
        selection.SelectStart,
        "pointer_start_offset",
        property(_upstream_pointer_start_offset),
    )
    firsts = await _first_lines_while_scrolling()
    assert firsts[0] != "line 002"


def test_apply_is_idempotent():
    assert apply_textual_patches() is True
    patched = selection.SelectStart.pointer_start_offset
    assert apply_textual_patches() is True
    assert selection.SelectStart.pointer_start_offset is patched


def test_other_version_not_patched(monkeypatch, caplog):
    original = property(_upstream_pointer_start_offset)
    monkeypatch.setattr(
        selection.SelectStart, "pointer_start_offset", original
    )
    monkeypatch.setattr(textual, "__version__", "9.0.0")
    assert apply_textual_patches() is False
    assert selection.SelectStart.pointer_start_offset is original
    assert "пересмотреть" in caplog.text


def test_other_version_without_private_module_imports(monkeypatch):
    """На чужой версии Textual приватного модуля может не быть: импорт
    приложения не падает, патч не ставится."""
    import importlib
    import sys

    import gc_tui

    for name in ("textual_compat", "app"):
        monkeypatch.setattr(gc_tui, name, getattr(gc_tui, name))
        monkeypatch.delitem(sys.modules, f"gc_tui.{name}")
    monkeypatch.setitem(sys.modules, "textual._auto_scroll", None)
    monkeypatch.setattr(textual, "__version__", "9.0.0")
    importlib.import_module("gc_tui.app")
    fresh = importlib.import_module("gc_tui.textual_compat")
    assert fresh.apply_textual_patches() is False


class _BelowScreenApp(_Lines):
    """Стенд repro2.py с протяжкой ниже ленты, как у TraceScreen."""

    def on_mount(self) -> None:
        screen = self.screen

        def check(select_widget, mouse_coordinate, delta_y):
            textual_compat.check_auto_scroll(
                screen,
                self.query_one("#t"),
                select_widget,
                mouse_coordinate,
                delta_y,
            )

        screen._check_auto_scroll = check


@pytest.mark.asyncio
async def test_horizontal_move_below_does_not_start_scroll():
    """Чисто горизонтальное движение не запускает протяжку (как у
    Textual); вертикальное — запускает."""
    app = _BelowScreenApp()
    async with app.run_test(size=(40, 30)) as pilot:
        screen = app.screen
        trace = app.query_one("#t")
        footer = app.query_one("#f")
        await pilot.mouse_down("#t", offset=(0, 2))
        await pilot.hover("#t", offset=(5, 10))
        await pilot.pause()
        below = (5, footer.region.y + 1)
        textual_compat.check_auto_scroll(screen, trace, footer, below, 0)
        assert textual_compat.auto_scroll_running(screen) is False
        textual_compat.check_auto_scroll(screen, trace, footer, below, 1)
        assert textual_compat.auto_scroll_running(screen) is True


@pytest.mark.asyncio
async def test_drag_below_scrolls_to_end_and_stops():
    app = _BelowScreenApp()
    async with app.run_test(size=(40, 30)) as pilot:
        trace = app.query_one("#t")
        trace.scroll_to(y=30, animate=False)
        await pilot.pause()
        await pilot.mouse_down("#t", offset=(0, 2))
        await pilot.hover("#t", offset=(5, 10))
        await pilot.hover("#f", offset=(2, 1))
        await pilot.pause()
        assert app.screen._auto_select_scroll_timer is not None
        await wait_until(
            pilot,
            lambda: trace.scroll_y == trace.max_scroll_y,
            "лента доехала до конца",
        )
        await pilot.pause(0.1)
        assert trace.scroll_y == trace.max_scroll_y
        assert _selected(app)[0] == "line 032"
        assert _selected(app)[-1] == "line 099"
        await pilot.mouse_up("#f", offset=(2, 1))
        await pilot.pause()
        assert app.screen._auto_select_scroll_timer is None


@pytest.mark.asyncio
async def test_other_version_no_drag_below(monkeypatch):
    app = _BelowScreenApp()
    async with app.run_test(size=(40, 30)) as pilot:
        monkeypatch.setattr(textual, "__version__", "9.0.0")
        await pilot.mouse_down("#t", offset=(2, 2))
        await pilot.hover("#t", offset=(5, 10))
        await pilot.hover("#f", offset=(2, 1))
        await pilot.pause()
        assert textual_compat.auto_scroll_running(app.screen) is False
        assert app.query_one("#t").scroll_y == 0


def _write_task(tmp_path, tid):
    meta = {
        "thread_id": tid,
        "status": "running",
        "updated_at": "2026-06-15T12:00:00+00:00",
        "messages_count": 0,
    }
    (Path(tmp_path) / f"meta_cli_{tid}.json").write_text(
        json.dumps(meta), encoding="utf-8"
    )
    (Path(tmp_path) / f"data_cli_{tid}.jsonl").write_text("", encoding="utf-8")


async def _tall_trace(pilot, tid):
    scr = pilot.app.screen
    scr.attach_active_task(tid)
    big = "\n".join(f"строка {i}" for i in range(80))
    scr._render_record(
        {"payload": {"id": "a1", "role": "assistant", "content": big}},
        live=True,
    )
    await pilot.pause(0.4)
    return scr


@pytest.mark.asyncio
async def test_trace_screen_drag_onto_input_scrolls_trace(tmp_path):
    from gc_tui.app import TuiApp

    _write_task(tmp_path, "sel1")
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings(
            {"agent.model": "M", "backend.api_mode": "chat_completions"}
        ),
    )
    async with app.run_test(size=(80, 24)) as pilot:
        scr = await _tall_trace(pilot, "sel1")
        trace = scr.query_one("#trace", VerticalScroll)
        scr._stick_bottom = False
        trace.scroll_to(y=trace.max_scroll_y - 30, animate=False)
        await pilot.pause()
        await pilot.mouse_down("#trace", offset=(4, 2))
        await pilot.hover("#trace", offset=(6, 5))
        await pilot.hover("#cmdinput", offset=(4, 0))
        await pilot.pause()
        assert textual_compat.auto_scroll_running(scr) is True
        await wait_until(
            pilot,
            lambda: trace.scroll_y == trace.max_scroll_y,
            "лента доехала до конца",
        )
        lines = (scr.get_selected_text() or "").splitlines()
        # Начало — в строке ответа, не баннер с первой строки ленты;
        # конец — последняя строка ленты, не поле ввода.
        assert "трока" in lines[0]
        assert lines[-1] == "строка 79"
        await pilot.mouse_up("#cmdinput", offset=(4, 0))
        await pilot.pause()
        assert textual_compat.auto_scroll_running(scr) is False


@pytest.mark.asyncio
async def test_trace_screen_drag_over_end_pill_scrolls_trace(tmp_path):
    """Плашка «в конец» лежит на нижней строке ленты поверх неё: протяжка
    по ней прокручивает ленту, конец выделения — в ленте."""
    from gc_tui.app import TuiApp

    _write_task(tmp_path, "sel2")
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings(
            {"agent.model": "M", "backend.api_mode": "chat_completions"}
        ),
    )
    async with app.run_test(size=(80, 24)) as pilot:
        scr = await _tall_trace(pilot, "sel2")
        trace = scr.query_one("#trace", VerticalScroll)
        scr._stick_bottom = False
        trace.scroll_to(y=trace.max_scroll_y - 30, animate=False)
        pill = scr.query_one("#trace-end-pill")
        await wait_until(pilot, lambda: pill.display, "плашка видна")
        await pilot.mouse_down("#trace", offset=(4, 2))
        await pilot.hover("#trace", offset=(6, 5))
        await pilot.hover("#trace-end-pill", offset=(1, 0))
        await pilot.pause()
        assert textual_compat.auto_scroll_running(scr) is True
        await wait_until(
            pilot,
            lambda: trace.scroll_y == trace.max_scroll_y,
            "лента доехала до конца",
        )
        lines = (scr.get_selected_text() or "").splitlines()
        assert "трока" in lines[0]
        assert lines[-1] == "строка 79"
        await pilot.mouse_up("#trace-end-pill", offset=(1, 0))
