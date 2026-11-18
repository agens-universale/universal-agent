"""Патч выделения Textual и протяжка ниже ленты (спека P3 §2.7).

Стенды `repro.py` и `repro2.py` (решения захода TUI 2026-10-20) — здесь
pilot-тестами. Страж версии падает при обновлении Textual.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import asynccontextmanager
import functools
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
def _fresh_version_check():
    """Проверка версии запоминает ответ: сброс до и после каждого теста."""
    check = textual_compat.textual_is_patched_version
    check.cache_clear()
    yield
    check.cache_clear()


def _other_version(monkeypatch) -> None:
    """Иная версия Textual — с той же минуты, и для запомненной проверки."""
    monkeypatch.setattr(textual, "__version__", "9.0.0")
    textual_compat.textual_is_patched_version.cache_clear()


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


def test_version_check_reads_version_once(monkeypatch):
    """Модуль Textual на каждое обращение к `__version__` разбирает
    METADATA пакета; проверка зовётся на каждую строку ленты — версия
    читается один раз."""
    import importlib.metadata

    reads = []
    real = importlib.metadata.version

    def version(name):
        reads.append(name)
        return real(name)

    monkeypatch.setattr(importlib.metadata, "version", version)
    if "__version__" in vars(textual):
        monkeypatch.delattr(textual, "__version__")
    assert textual_compat.textual_is_patched_version() is True
    assert textual_compat.textual_is_patched_version() is True
    assert len(reads) <= 1


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
    from textual._styles_cache import StylesCache

    original = property(_upstream_pointer_start_offset)
    monkeypatch.setattr(
        selection.SelectStart, "pointer_start_offset", original
    )
    inner_outer = _upstream_inner_outer()
    monkeypatch.setattr(StylesCache, "get_inner_outer", inner_outer)
    _other_version(monkeypatch)
    assert apply_textual_patches() is False
    assert selection.SelectStart.pointer_start_offset is original
    assert StylesCache.get_inner_outer is inner_outer
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
    _other_version(monkeypatch)
    importlib.import_module("gc_tui.app")
    fresh = importlib.import_module("gc_tui.textual_compat")
    assert fresh.apply_textual_patches() is False
    assert issubclass(fresh.AnchoredScroll, VerticalScroll)


class _Anchored(App):
    """Якорная лента ниже окна: три строки в окне на десять."""

    CSS = "#a {height: 10;} Static {height: 1;}"

    def compose(self):
        with textual_compat.AnchoredScroll(id="a"):
            for i in range(3):
                yield Static(f"line {i}")

    def on_mount(self) -> None:
        self.query_one("#a").anchor()


async def _anchored_scroll_y() -> float:
    app = _Anchored()
    async with app.run_test(size=(40, 20)) as pilot:
        await pilot.pause()
        return app.query_one("#a").scroll_y


@pytest.mark.asyncio
async def test_anchored_scroll_not_negative():
    assert await _anchored_scroll_y() == 0


@pytest.mark.asyncio
async def test_other_version_anchored_scroll_is_plain_anchor(monkeypatch):
    """На чужой версии Textual поправки якоря не ставятся: лента ведёт
    себя как обычный якорь Textual (здесь — его прокрутка в минус)."""
    _other_version(monkeypatch)
    assert await _anchored_scroll_y() < 0


def test_other_version_scroll_skips_private_anchor_state(monkeypatch):
    """На чужой версии прокрутка не трогает приватное состояние якоря:
    его может не быть, и тогда падала бы каждая прокрутка ленты."""
    from textual.widget import Widget

    _other_version(monkeypatch)
    scroll = textual_compat.AnchoredScroll()
    del scroll._anchored, scroll._anchor_released

    scroll.set_reactive(Widget.scroll_y, 5)

    assert scroll.scroll_y == 5


class _TallAnchored(App):
    """Якорная лента выше окна: тридцать строк в окне на десять."""

    CSS = "#a {height: 10;} Static {height: 1;}"

    def compose(self):
        with textual_compat.AnchoredScroll(id="a"):
            for i in range(30):
                yield Static(f"line {i}")


async def _tall_anchor_y() -> int:
    app = _TallAnchored()
    async with app.run_test(size=(40, 20)) as pilot:
        await pilot.pause()
        return app.query_one("#a")._anchor_y


@pytest.mark.asyncio
async def test_anchored_arrange_counts_anchor():
    assert await _tall_anchor_y() == 20


@pytest.mark.asyncio
async def test_anchor_layout_repaints_scrollbar_after_stale_frame():
    from textual.geometry import Region

    app = _TallAnchored()
    async with app.run_test(size=(40, 20)) as pilot:
        await pilot.pause()
        scroll = app.query_one("#a")
        scroll.anchor()
        await pilot.pause()
        bar = scroll.vertical_scrollbar
        crop = Region(0, 0, bar.size.width, bar.size.height)

        def painted():
            return [list(line) for line in bar.render_lines(crop)]

        expected = painted()
        # Кадр между двумя раскладками: ползунок ещё по прежней высоте окна.
        bar.position = scroll.scroll_y - 2
        await pilot.pause()
        assert painted() != expected
        scroll.refresh(layout=True)
        await pilot.pause()

        assert bar.position == scroll.scroll_y == scroll.max_scroll_y
        assert painted() == expected


@pytest.mark.asyncio
async def test_other_version_anchored_arrange_is_plain(monkeypatch):
    """На чужой версии раскладка якорную прокрутку не считает."""
    _other_version(monkeypatch)
    assert await _tall_anchor_y() == 0


async def _selecting_seen() -> tuple[bool, list[bool]]:
    app = _Lines()
    seen: list[bool] = []
    async with app.run_test(size=(40, 30)) as pilot:
        watched = textual_compat.watch_selecting(
            app.screen, lambda selecting: seen.append(selecting)
        )
        await pilot.mouse_down("#t", offset=(0, 2))
        await pilot.hover("#t", offset=(5, 10))
        await pilot.pause()
        await pilot.mouse_up("#t", offset=(5, 10))
        await pilot.pause()
    return watched, seen


@pytest.mark.asyncio
async def test_watch_selecting_reports_start_and_end():
    assert await _selecting_seen() == (True, [False, True, False])


@pytest.mark.asyncio
async def test_other_version_watch_selecting_off(monkeypatch):
    _other_version(monkeypatch)
    assert await _selecting_seen() == (False, [])


class _BelowScreenApp(_Lines):
    """Стенд repro2.py с протяжкой ниже ленты, как у TraceScreen."""

    def on_mount(self) -> None:
        textual_compat.hook_auto_scroll(
            self.screen, lambda: self.query_one("#t")
        )


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
async def test_horizontal_move_keeps_running_scroll_below():
    """Протяжка ниже ленты уже идёт — горизонтальное движение над полем
    её не гасит (у Textual идущая прокрутка тоже продолжается)."""
    app = _BelowScreenApp()
    async with app.run_test(size=(40, 30)) as pilot:
        screen = app.screen
        trace = app.query_one("#t")
        footer = app.query_one("#f")
        await pilot.mouse_down("#t", offset=(0, 2))
        await pilot.hover("#t", offset=(5, 10))
        await pilot.pause()
        below = (5, footer.region.y + 1)
        textual_compat.check_auto_scroll(screen, trace, footer, below, 1)
        assert textual_compat.auto_scroll_running(screen) is True
        aside = (9, footer.region.y + 1)
        textual_compat.check_auto_scroll(screen, trace, footer, aside, 0)
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
        _other_version(monkeypatch)
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


class _Host(App):
    """Пустой экран с контейнером для строк `paint_after_mount`."""

    def compose(self):
        yield VerticalScroll(id="host")


def _hold(row: Static) -> int:
    """Отдать строку `mount()` и придержать экран; счёт пакета сразу
    после."""
    app = row.app if row.parent else None
    textual_compat.paint_after_mount(row)
    return app._batch_count if app else 0


async def _in_app(pilot, act: Callable[[], object]) -> object:
    """Выполнить ``act`` в обработчике приложения, как в продукте:
    активное приложение — своё, а не прошлого теста."""
    out: list = []
    pilot.app.call_later(lambda: out.append(act()))
    await wait_until(pilot, lambda: bool(out), "выполнено")
    return out[0]


async def _released(pilot) -> int:
    await wait_until(
        pilot, lambda: not textual_compat._PAINT_HOLDS, "отпущено"
    )
    return pilot.app._batch_count


def _mount_and_hold(app: App) -> tuple[Static, int]:
    row = Static("row")
    app.query_one("#host").mount(row)
    return row, _hold(row)


@pytest.mark.asyncio
async def test_paint_after_mount_releases_after_mount():
    app = _Host()
    async with app.run_test() as pilot:
        row, held = await _in_app(pilot, lambda: _mount_and_hold(app))
        assert (held, await _released(pilot)) == (1, 0)
        assert row.is_mounted


@pytest.mark.asyncio
async def test_paint_after_mount_releases_on_timeout(monkeypatch):
    async def never(parent, widgets):
        await asyncio.Event().wait()

    monkeypatch.setattr(textual_compat, "mounted", never)
    monkeypatch.setattr(textual_compat, "_MOUNT_WAIT_LIMIT", 0.3)
    monkeypatch.setattr(textual_compat, "_PAINT_HOLD_LIMIT", 0.15)
    app = _Host()
    async with app.run_test() as pilot:
        _, held = await _in_app(pilot, lambda: _mount_and_hold(app))
        assert (held, await _released(pilot)) == (1, 0)


@pytest.mark.asyncio
async def test_hold_at_series_end_task_still_waits_mount(monkeypatch):
    """Остаток серии почти ноль: удержания нет, а задача кончается только
    монтажом — продолжение после монтажа (кусок окна ленты встаёт по
    новой раскладке) не зависит от срока удержания."""
    real = textual_compat.mounted
    mounted: list[bool] = []

    async def slow(parent, widgets):
        await asyncio.sleep(0.3)
        await real(parent, widgets)
        mounted.append(True)

    monkeypatch.setattr(textual_compat, "mounted", slow)
    app = _Host()

    def hold_at_series_end():
        row = Static("row")
        app.query_one("#host").mount(row)
        series = textual_compat._HOLD_SERIES.setdefault(
            app, textual_compat._HoldSeries()
        )
        series.count += 1  # серия идёт: держит другой кусок
        series.until = asyncio.get_running_loop().time() + 0.01
        task = textual_compat.paint_after_mount(row)
        task.add_done_callback(lambda _t: done.append(bool(mounted)))
        return app._batch_count

    done: list[bool] = []
    async with app.run_test() as pilot:
        held = await _in_app(pilot, hold_at_series_end)
        await wait_until(pilot, lambda: bool(done), "задача кончилась")
        assert (held, done) == (0, [True])


@pytest.mark.asyncio
async def test_hold_expires_before_mount_task_waits_mount(monkeypatch):
    """Главная ветка: удержание заведено, его срок истёк раньше монтажа —
    экран отпущен, а задача (и settle куска за ней) кончается только
    монтажом."""
    real = textual_compat.mounted
    mounted: list[bool] = []

    async def slow(parent, widgets):
        await asyncio.sleep(0.6)
        await real(parent, widgets)
        mounted.append(True)

    monkeypatch.setattr(textual_compat, "mounted", slow)
    monkeypatch.setattr(textual_compat, "_PAINT_HOLD_LIMIT", 0.2)
    app = _Host()
    done: list[bool] = []

    def hold() -> int:
        _row, held = _mount_and_hold(app)
        task = next(iter(textual_compat._PAINT_HOLDS))
        task.add_done_callback(lambda _t: done.append(bool(mounted)))
        return held

    async with app.run_test() as pilot:
        held = await _in_app(pilot, hold)
        await wait_until(pilot, lambda: app._batch_count == 0, "отпущено")
        assert (held, done) == (1, [])
        await wait_until(pilot, lambda: bool(done), "задача кончилась")
        assert done == [True]


@pytest.mark.asyncio
async def test_overlapping_paint_holds_let_a_frame_through(monkeypatch):
    """Удержания подряд внахлёст (непрерывный подъём ленты) не держат
    экран дольше потолка: иначе кадра нет, а `Screen._dirty_widgets`
    копит каждый перерисованный виджет, в том числе снятые, с их
    отрисовкой."""

    async def never(parent, widgets):
        await asyncio.Event().wait()

    monkeypatch.setattr(textual_compat, "mounted", never)
    monkeypatch.setattr(textual_compat, "_MOUNT_WAIT_LIMIT", 0.3)
    monkeypatch.setattr(textual_compat, "_PAINT_HOLD_LIMIT", 0.2)
    app = _Host()
    async with app.run_test() as pilot:
        counts = []
        for _ in range(16):
            counts.append(app._batch_count)
            await _in_app(pilot, lambda: _mount_and_hold(app))
            await asyncio.sleep(0.05)
        assert 0 in counts[1:]
        await _released(pilot)


@pytest.mark.asyncio
async def test_back_to_back_paint_holds_let_a_frame_through(monkeypatch):
    """Новое удержание сразу по отпусканию прежнего (кусок за куском при
    подъёме) ждёт кадра: иначе серии смыкаются и экран не рисует."""
    from textual.screen import Screen

    async def never(parent, widgets):
        await asyncio.Event().wait()

    frames: list[int] = []
    refresh = Screen._compositor_refresh

    def counted(self) -> None:
        frames.append(1)
        refresh(self)

    monkeypatch.setattr(textual_compat, "mounted", never)
    monkeypatch.setattr(textual_compat, "_MOUNT_WAIT_LIMIT", 0.3)
    monkeypatch.setattr(textual_compat, "_PAINT_HOLD_LIMIT", 0.1)
    app = _Host()
    async with app.run_test() as pilot:
        going = [True]

        def next_hold(_task=None) -> None:
            if not going[0]:
                return
            row = Static("row")
            app.query_one("#host").mount(row)
            textual_compat.paint_after_mount(row).add_done_callback(next_hold)

        await _in_app(pilot, next_hold)
        await asyncio.sleep(0.15)
        monkeypatch.setattr(Screen, "_compositor_refresh", counted)
        await asyncio.sleep(0.5)
        going[0] = False
        assert frames
        await _released(pilot)


@pytest.mark.asyncio
async def test_lost_frame_callback_does_not_disable_holds(monkeypatch):
    """Сигнал кадра после серии потерялся (его принял снятый экран) —
    удержания возвращаются по сроку, а не выключаются навсегда."""

    async def never(parent, widgets):
        await asyncio.Event().wait()

    monkeypatch.setattr(textual_compat, "mounted", never)
    monkeypatch.setattr(textual_compat, "_MOUNT_WAIT_LIMIT", 0.3)
    # Потолок серии — с запасом над остатком без удержания
    # (`_PAINT_HOLD_MIN`): равный ему остаток `until - now` в float бывает
    # чуть меньше, и первое удержание не ставится вовсе.
    limit = textual_compat._PAINT_HOLD_MIN * 1.5
    monkeypatch.setattr(textual_compat, "_PAINT_HOLD_LIMIT", limit)
    app = _Host()
    async with app.run_test() as pilot:
        monkeypatch.setattr(app, "call_after_refresh", lambda *_a: True)
        _, held = await _in_app(pilot, lambda: _mount_and_hold(app))
        await _released(pilot)
        # Срок возврата удержаний (потолок серии от потери сигнала) прошёл.
        await asyncio.sleep(limit + 0.1)
        _, again = await _in_app(pilot, lambda: _mount_and_hold(app))
        assert (held, again) == (1, 1)
        await _released(pilot)


@pytest.mark.asyncio
async def test_paint_after_mount_noop_when_mounted_or_orphan():
    app = _Host()
    async with app.run_test() as pilot:
        row = Static("row")
        await app.query_one("#host").mount(row)
        held = await _in_app(pilot, lambda: _hold(row))
        orphan = await _in_app(pilot, lambda: _hold(Static("orphan")))
        assert (held, orphan) == (0, 0)
        assert not textual_compat._PAINT_HOLDS


@pytest.mark.asyncio
async def test_paint_after_mount_releases_when_removed_before_mount():
    app = _Host()

    def mount_hold_remove() -> int:
        row, held = _mount_and_hold(app)
        row.remove()
        return held

    async with app.run_test() as pilot:
        held = await _in_app(pilot, mount_hold_remove)
        assert (held, await _released(pilot)) == (1, 0)


@pytest.mark.asyncio
async def test_paint_after_mount_failure_leaves_no_hold(monkeypatch):
    """Не удалось завести отпускание — экран не замирает, даже пока
    ошибка с её кадрами жива (журнал, обработчик выше)."""
    app = _Host()

    def broken_loop():
        raise RuntimeError("loop")

    def mount_hold_fail() -> int:
        row = Static("row")
        app.query_one("#host").mount(row)
        with monkeypatch.context() as patch:
            patch.setattr(
                textual_compat.asyncio, "get_running_loop", broken_loop
            )
            with pytest.raises(RuntimeError) as failure:
                textual_compat.paint_after_mount(row)
        assert failure.value.__traceback__ is not None
        return app._batch_count

    async with app.run_test() as pilot:
        assert await _in_app(pilot, mount_hold_fail) == 0
        assert not textual_compat._PAINT_HOLDS


def _trace_app(tmp_path, tid):
    from gc_tui.app import TuiApp

    _write_task(tmp_path, tid)
    return TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings(
            {"agent.model": "M", "backend.api_mode": "chat_completions"}
        ),
    )


@pytest.mark.asyncio
async def test_trace_screen_hooks_auto_scroll_on_patched_version(tmp_path):
    from textual.screen import Screen

    async with _trace_app(tmp_path, "sel3").run_test(size=(80, 24)) as pilot:
        hook = pilot.app.screen._check_auto_scroll
        assert getattr(hook, "__func__", None) is not Screen._check_auto_scroll


@pytest.mark.asyncio
async def test_other_version_trace_screen_keeps_stock_auto_scroll(
    tmp_path, monkeypatch
):
    """Чужая версия Textual: хук не ставится, выделение идёт через
    автопрокрутку Textual как есть — её сигнатура экрану не важна."""
    from textual.screen import Screen

    stock = Screen._check_auto_scroll
    calls = []

    def recorded(self, *args, **kwargs):
        calls.append(args)
        return stock(self, *args, **kwargs)

    monkeypatch.setattr(Screen, "_check_auto_scroll", recorded)
    _other_version(monkeypatch)
    async with _trace_app(tmp_path, "sel4").run_test(size=(80, 24)) as pilot:
        scr = await _tall_trace(pilot, "sel4")
        assert scr._check_auto_scroll.__func__ is recorded
        await pilot.mouse_down("#trace", offset=(4, 2))
        await pilot.hover("#trace", offset=(6, 5))
        await pilot.hover("#cmdinput", offset=(4, 0))
        await pilot.pause()
        await pilot.mouse_up("#cmdinput", offset=(4, 0))
        await pilot.pause()
        assert calls


class _Scrolled(App):
    """Строка с отступами наверху длинной прокрутки — после `scroll_end`
    она за экраном."""

    def compose(self):
        with VerticalScroll(id="host"):
            yield Static("первая", id="first", classes="padded")
            for i in range(200):
                yield Static(f"строка {i}")

    CSS = ".padded { padding: 0 2 0 3; }"


async def _scrolled_away(pilot) -> Static:
    first = pilot.app.query_one("#first", Static)
    pilot.app.query_one("#host", VerticalScroll).scroll_end(animate=False)
    await pilot.pause()
    return first


@pytest.mark.asyncio
async def test_is_visible_on_screen_and_scrolled_away():
    async with _Scrolled().run_test(size=(60, 20)) as pilot:
        first = pilot.app.query_one("#first", Static)
        assert textual_compat.is_visible(first) is True
        await _scrolled_away(pilot)
        assert textual_compat.is_visible(first) is False


@pytest.mark.asyncio
async def test_is_visible_none_and_unmounted():
    async with _Scrolled().run_test(size=(60, 20)):
        assert textual_compat.is_visible(None) is False
        assert textual_compat.is_visible(Static("не смонтирован")) is False


@pytest.mark.asyncio
async def test_is_visible_does_not_rebuild_full_map():
    """`is_visible` смотрит готовую карту видимых; публичный
    `is_on_screen` в том же состоянии карту пересобирает (контроль)."""
    from arrange_counter import count_arranges

    async with _Scrolled().run_test(size=(60, 20)) as pilot:
        first = await _scrolled_away(pilot)
        with count_arranges() as ours:
            textual_compat.is_visible(first)
        with count_arranges() as public:
            first.is_on_screen  # карту пересобирает
        assert (ours.full_map, public.full_map) == (0, 1)


@pytest.mark.asyncio
async def test_other_version_is_visible_answers_visible(monkeypatch):
    monkeypatch.setattr(
        textual_compat, "textual_is_patched_version", lambda: False
    )
    async with _Scrolled().run_test(size=(60, 20)) as pilot:
        first = await _scrolled_away(pilot)
        assert textual_compat.is_visible(first) is True


@pytest.mark.asyncio
async def test_laid_out_widths_equal_region_widths():
    """Ширины по последней раскладке — те же, что из карты экрана:
    контент без отступов, у прокрутки — ещё без полосы."""
    async with _Scrolled().run_test(size=(60, 20)) as pilot:
        first = pilot.app.query_one("#first", Static)
        host = pilot.app.query_one("#host", VerticalScroll)
        assert host.scrollbar_size_vertical > 0
        assert textual_compat.laid_out_width(first) == first.size.width
        assert textual_compat.laid_out_scrollable_width(host) == (
            host.scrollable_content_region.width
        )
        assert textual_compat.laid_out_width(Static("нет")) == 0


@pytest.mark.asyncio
async def test_laid_out_widths_offscreen_no_full_map():
    from arrange_counter import count_arranges

    async with _Scrolled().run_test(size=(60, 20)) as pilot:
        first = await _scrolled_away(pilot)
        with count_arranges() as counts:
            width = textual_compat.laid_out_width(first)
        with count_arranges() as control:
            assert first.size.width == width
        assert (counts.full_map, control.full_map) == (0, 1)


@pytest.mark.asyncio
async def test_hidden_widget_laid_out_width_is_zero():
    """Скрытый не в раскладке: его записанный размер устарел — 0, как у
    `size`."""
    async with _Scrolled().run_test(size=(60, 20)) as pilot:
        first = pilot.app.query_one("#first", Static)
        first.display = False
        await pilot.pause()
        assert first.outer_size.width > 0
        assert textual_compat.laid_out_width(first) == first.size.width == 0


@pytest.mark.asyncio
async def test_other_version_laid_out_width_is_size(monkeypatch):
    monkeypatch.setattr(
        textual_compat, "textual_is_patched_version", lambda: False
    )
    async with _Scrolled().run_test(size=(60, 20)) as pilot:
        first = pilot.app.query_one("#first", Static)
        assert textual_compat.laid_out_width(first) == first.size.width > 0


def test_arrange_counter_refuses_renamed_caller(monkeypatch):
    """Счётчик раскладок считает по именам вызывающих Textual: пропажа
    имени — ошибка, а не молчаливые нули."""
    import arrange_counter
    from textual._compositor import Compositor

    with arrange_counter.count_arranges() as counts:
        assert counts.full_map == 0
    monkeypatch.delattr(Compositor, "reflow_visible")
    with pytest.raises(RuntimeError, match="reflow_visible"):
        with arrange_counter.count_arranges():
            pass


def test_static_markup_reads_flag():
    assert textual_compat.static_markup(Static(markup=False)) is False
    assert textual_compat.static_markup(Static()) is True


def test_other_version_static_markup_is_on(monkeypatch):
    _other_version(monkeypatch)
    assert textual_compat.static_markup(Static(markup=False)) is True


# ---- удержание первой видимой строки (спека шага 2 окна, §5.3) ----------


class _Held(App):
    """Отлистанная якорная лента: строки, зависящие от ширины."""

    CSS = "#a {height: 10; width: 60;} Static {height: auto;}"

    def compose(self):
        with textual_compat.AnchoredScroll(id="a"):
            for i in range(60):
                text = f"line {i} " + "слово " * (i % 7)
                yield Static(text, id=f"l{i}")


async def _scrolled_held(pilot, y: int):
    scroll = pilot.app.query_one("#a")
    await pilot.pause()
    scroll.scroll_to(y=y, animate=False)
    await pilot.pause()
    held = scroll.first_visible()
    scroll.hold_first_visible(held)
    return scroll, held, held.region.y


@asynccontextmanager
async def _prepend_and_layout(app, scroll, *, width: int | None = None):
    """Домонтировать сверху под удержанием отрисовки и разложить экран,
    как компоновщик перед кадром, — без `pilot.pause()`."""
    news = [Static(f"new {i} " + "x " * i) for i in range(25)]
    with app.batch_update():
        if width is not None:
            scroll.styles.width = width
        await scroll.mount_all(news, before=scroll.children[0])
        app.screen._refresh_layout(app.screen.size)
        yield


@pytest.mark.asyncio
async def test_prepend_keeps_visible_row_no_frame_jump():
    app = _Held()
    async with app.run_test(size=(80, 20)) as pilot:
        scroll, held, row = await _scrolled_held(pilot, 30)
        before = scroll.scroll_y
        async with _prepend_and_layout(app, scroll):
            assert held.region.y == row
            assert scroll.scroll_y > before


@pytest.mark.asyncio
async def test_prepend_keeps_row_after_width_change():
    app = _Held()
    async with app.run_test(size=(80, 20)) as pilot:
        scroll, held, row = await _scrolled_held(pilot, 40)
        async with _prepend_and_layout(app, scroll, width=24):
            assert held.region.y == row


@pytest.mark.asyncio
async def test_hold_off_and_anchored_keep_textual_behaviour():
    """Без удержания домонтирование сдвигает строку (контроль); якорь
    низа удержанию не уступает."""
    app = _Held()
    async with app.run_test(size=(80, 20)) as pilot:
        scroll, held, row = await _scrolled_held(pilot, 30)
        scroll.hold_first_visible(None)
        async with _prepend_and_layout(app, scroll):
            assert held.region.y != row
        scroll.hold_first_visible(held)
        scroll.anchor()
        await pilot.pause()
        assert scroll.scroll_y == scroll.max_scroll_y


@pytest.mark.asyncio
async def test_other_version_hold_is_off(monkeypatch):
    _other_version(monkeypatch)
    app = _Held()
    async with app.run_test(size=(80, 20)) as pilot:
        scroll = app.query_one("#a")
        await pilot.pause()
        assert scroll.first_visible() is None


class _Rows(App):
    """Лента строк: снятие строк, как окно ленты."""

    def compose(self):
        with VerticalScroll(id="a"):
            for i in range(20):
                yield Static(f"line {i}")


def _upstream_inner_outer():
    """Копия `StylesCache.get_inner_outer` Textual 8.2.8: `lru_cache` на
    методе экземпляра. Свежий кеш на каждый вызов — не живёт весь
    прогон набора."""
    from textual.style import Style

    @functools.lru_cache(1024)
    def get_inner_outer(cls, base_background, background):
        return (
            Style(background=base_background + background),
            Style(background=base_background),
        )

    return get_inner_outer


async def _removed_line_caches() -> int:
    """Сколько кешей строк снятых и отрисованных виджетов переживают
    полную сборку мусора."""
    import gc
    import weakref

    app = _Rows()
    async with app.run_test(size=(40, 30)) as pilot:
        await pilot.pause()
        rows = list(app.query(Static))
        caches = [weakref.ref(row._styles_cache) for row in rows]
        await app.query_one("#a").remove_children()
        await pilot.pause()
        del rows
    gc.collect()
    return sum(ref() is not None for ref in caches)


@pytest.mark.asyncio
async def test_removed_widget_line_cache_is_freed():
    assert apply_textual_patches() is True
    assert await _removed_line_caches() == 0


@pytest.mark.asyncio
async def test_upstream_inner_outer_keeps_removed_line_caches(monkeypatch):
    """Контроль стенда: с копией формулы Textual 8.2.8 (`lru_cache` на
    методе экземпляра) кеши строк снятых виджетов переживают сборку —
    тест выше умеет падать. О починке в апстриме этот тест не скажет:
    на неё укажет страж версии."""
    from textual._styles_cache import StylesCache

    inner_outer = _upstream_inner_outer()
    monkeypatch.setattr(StylesCache, "get_inner_outer", inner_outer)
    try:
        assert await _removed_line_caches() > 0
    finally:
        inner_outer.cache_clear()
