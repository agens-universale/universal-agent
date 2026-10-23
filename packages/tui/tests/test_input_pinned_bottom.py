"""Поле ввода прижато к низу экрана, список из поля раскрывается ВВЕРХ.

Правило раскладки главного экрана: строка ввода и подвал всегда внизу, даже
в пустой новой сессии; палитра и пикеры (`/`, `/skill`, значения ручек, `@`)
встают в раскладку прямо над вводом и поднимают ленту, а не накрывают её.
Ввод не двигается ни при открытии списка, ни при фильтре, ни при закрытии;
лента прижата к низу — её последние строки видны над списком.
"""

import json
from pathlib import Path

import pytest
from gc_testkit.tui_support import tui_settings

_SIZE = (80, 30)


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


def _app(tmp_path):
    from gc_tui.app import TuiApp

    return TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings(
            {"agent.model": "M", "backend.api_mode": "chat_completions"}
        ),
    )


async def _with_trace(pilot, tmp_path, lines):
    scr = pilot.app.screen
    _write_task(tmp_path, "pin")
    scr.attach_active_task("pin")
    body = "\n".join(f"строка {i}" for i in range(lines))
    scr._render_record(
        {"payload": {"id": "a1", "role": "assistant", "content": body}},
        live=True,
    )
    await pilot.pause(0.4)
    return scr


def _input_y(pilot):
    return pilot.app.query_one("#input-row").region.y


@pytest.mark.asyncio
async def test_empty_session_input_at_bottom(tmp_path):
    pytest.importorskip("textual")
    async with _app(tmp_path).run_test(size=_SIZE) as pilot:
        await pilot.pause(0.4)
        inp = pilot.app.query_one("#input-row")
        footer = pilot.app.query_one("#footer")
        assert footer.region.bottom == _SIZE[1]
        assert inp.region.bottom == footer.region.y


@pytest.mark.asyncio
@pytest.mark.parametrize("lines", [1, 80], ids=["short", "full"])
async def test_input_still_while_palette_opens_filters_closes(tmp_path, lines):
    pytest.importorskip("textual")
    async with _app(tmp_path).run_test(size=_SIZE) as pilot:
        await _with_trace(pilot, tmp_path, lines)
        y0 = _input_y(pilot)
        assert pilot.app.query_one("#footer").region.bottom == _SIZE[1]
        await pilot.press("/")
        await pilot.pause(0.4)
        assert pilot.app.screen._palette is not None
        assert _input_y(pilot) == y0
        for key in "compa":  # → ровно /compact
            await pilot.press(key)
        await pilot.pause(0.4)
        palette = pilot.app.screen._palette
        assert len(palette._visible_rows()) == 1
        assert _input_y(pilot) == y0
        await pilot.press("escape")
        await pilot.pause(0.4)
        assert pilot.app.screen._palette is None
        assert _input_y(pilot) == y0


@pytest.mark.asyncio
async def test_palette_above_input_trace_tail_visible(tmp_path):
    pytest.importorskip("textual")
    from textual.containers import VerticalScroll

    async with _app(tmp_path).run_test(size=_SIZE) as pilot:
        scr = await _with_trace(pilot, tmp_path, 80)
        scr._open_palette(prefix="")
        await pilot.pause(0.4)
        trace = pilot.app.query_one("#trace", VerticalScroll)
        palette = scr._palette.region
        assert palette.bottom == _input_y(pilot)  # прямо над вводом
        assert trace.region.bottom <= palette.y  # лента выше, не под ним
        assert trace.scroll_offset.y == trace.max_scroll_y  # хвост виден
        scr._palette.set_filter("compa")  # список сжался — хвост виден
        await pilot.pause(0.4)
        assert trace.scroll_offset.y == trace.max_scroll_y
        scr._close_palette()
        await pilot.pause(0.4)
        assert trace.scroll_offset.y == trace.max_scroll_y
        assert scr._stick_bottom is True


async def _open_skill(scr):
    scr._skills_cache = [
        {"id": f"skill-{i}", "description": "навык"} for i in range(6)
    ]
    await scr._open_skill_picker(prefix="")
    return scr._skill_picker


async def _open_arg(scr):
    scr._open_arg_picker(
        options=[("light", "лёгкая"), ("default", "обычная"), ("max", "max")],
        prefix="",
    )
    return scr._arg_picker


async def _open_file(scr):
    scr._apply_file_entries(["a.py", "b/"], truncated=False)
    return scr._file_picker


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "opener",
    [_open_skill, _open_arg, _open_file],
    ids=["skill", "arg", "file"],
)
async def test_pickers_open_above_input(tmp_path, opener):
    pytest.importorskip("textual")
    async with _app(tmp_path).run_test(size=_SIZE) as pilot:
        scr = await _with_trace(pilot, tmp_path, 80)
        y0 = _input_y(pilot)
        picker = await opener(scr)
        await pilot.pause(0.4)
        assert _input_y(pilot) == y0
        assert picker.region.bottom == y0


@pytest.mark.asyncio
async def test_footer_stays_put_as_content_grows(tmp_path):
    """Короткий контент не скроллится, но подвал уже у низа; рост контента
    его не двигает (раньше хром шёл за концом контента)."""
    pytest.importorskip("textual")
    from textual.containers import VerticalScroll

    async with _app(tmp_path).run_test(size=_SIZE) as pilot:
        scr = await _with_trace(pilot, tmp_path, 1)
        trace = pilot.app.query_one("#trace", VerticalScroll)
        footer = pilot.app.query_one("#footer")
        assert trace.max_scroll_y == 0
        assert footer.region.bottom == _SIZE[1]
        scr._render_record(
            {"payload": {"id": "a2", "role": "assistant", "content": "ещё"}},
            live=True,
        )
        await pilot.pause(0.4)
        assert footer.region.bottom == _SIZE[1]


@pytest.mark.asyncio
async def test_tall_content_scrolls_footer_at_bottom(tmp_path):
    pytest.importorskip("textual")
    from textual.containers import VerticalScroll

    async with _app(tmp_path).run_test(size=(80, 24)) as pilot:
        await _with_trace(pilot, tmp_path, 60)
        trace = pilot.app.query_one("#trace", VerticalScroll)
        assert trace.max_scroll_y > 0  # контент не влез — скроллится
        assert pilot.app.query_one("#footer").region.bottom == 24


@pytest.mark.asyncio
async def test_ask_human_banner_keeps_footer_onscreen(tmp_path):
    """Регресс: баннер ask_human монтируется в #root (перед #input-row) → входит в
    хром. При высоком контенте потолок #trace учитывает баннер, футер остаётся на
    экране (не выдавлен)."""
    pytest.importorskip("textual")
    from types import SimpleNamespace

    tid = "ah1"
    _write_task(tmp_path, tid)
    async with _app(tmp_path).run_test(size=(80, 24)) as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        big = "\n".join(
            f"строка {i}" for i in range(60)
        )  # #trace упрётся в потолок
        scr._render_record(
            {"payload": {"id": "a1", "role": "assistant", "content": big}},
            live=True,
        )
        await pilot.pause(0.4)
        ev = SimpleNamespace(
            call_id="c1",
            summary="В какую часть углубиться?",
            no=99,
            kind="ask_human",
        )
        scr._on_ask_human_event(ev, live=False)  # баннер в #root перед input
        await pilot.pause(0.4)
        footer = pilot.app.query_one("#footer")
        assert footer.region.bottom <= 24  # футер на экране (не выдавлен)


@pytest.mark.asyncio
async def test_tall_ask_human_question_keeps_input_onscreen(tmp_path):
    """Регресс: длинный вопрос ask_human капается (≤ половины экрана) и скроллится
    внутри баннера — поле ввода и футер остаются на экране, не уезжают вниз."""
    pytest.importorskip("textual")
    from types import SimpleNamespace

    tid = "ah2"
    _write_task(tmp_path, tid)
    async with _app(tmp_path).run_test(size=(80, 24)) as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        await pilot.pause(0.2)
        tall_q = "\n".join(
            f"пункт {i}: очень подробный вопрос агента" for i in range(40)
        )
        ev = SimpleNamespace(
            call_id="c-tall", summary=tall_q, no=1, kind="ask_human"
        )
        scr._on_ask_human_event(ev, live=False)
        await pilot.pause(0.5)
        banner = scr._ask_human_banner
        inp = pilot.app.query_one("#input-row")
        footer = pilot.app.query_one("#footer")
        assert banner is not None
        assert banner.region.height <= 24 // 2  # потолок половины экрана
        assert inp.region.bottom <= 24  # ввод на экране
        assert footer.region.bottom <= 24  # футер на экране
        assert inp.region.height >= 3  # поле ввода видимо (не схлопнуто)
