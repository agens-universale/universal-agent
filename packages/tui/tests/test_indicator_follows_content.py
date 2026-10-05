"""Индикатор/ввод/футер следуют за концом контента (Claude-style).

Модель: #trace `auto` сжимается до контента, хром ниже (indicator-bar/input/footer)
идёт сразу за ним, пустота — внизу под футером. Потолок #trace (= экран минус хром)
лишь включает скролл при заполнении экрана. Спейсера НЕТ — рост контента двигает
хром нативно (Textual), без покадрового замера высоты → без «прыжка» раскладки.
"""

from gc_testkit.tui_support import tui_settings
import json
from pathlib import Path

import pytest


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


@pytest.mark.asyncio
async def test_short_content_chrome_follows_content(tmp_path):
    """Короткий контент: #trace не скроллится, хром идёт сразу за контентом, а под
    футером — пустота (футер НЕ прижат к низу экрана)."""
    pytest.importorskip("textual")
    from textual.containers import VerticalScroll

    tid = "fc1"
    _write_task(tmp_path, tid)
    # 80x40: бренд-карточка (после редизайна 06-28 стала выше) + короткий ответ
    # умещаются под потолком #trace, поэтому follows-content не скроллит.
    async with _app(tmp_path).run_test(size=(80, 40)) as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        scr._render_record(
            {
                "payload": {
                    "id": "a1",
                    "role": "assistant",
                    "content": "короткий ответ",
                }
            },
            live=True,
        )
        await pilot.pause(0.4)
        trace = pilot.app.query_one("#trace", VerticalScroll)
        footer = pilot.app.query_one("#footer")
        assert trace.max_scroll_y == 0  # контент влез — нет скролла
        assert (
            footer.region.bottom < pilot.app.size.height
        )  # хром следует за контентом → пустота снизу


@pytest.mark.asyncio
async def test_tall_content_caps_chrome_at_bottom(tmp_path):
    """Высокий контент: #trace упирается в потолок (скроллится), хром у низа экрана,
    футер на самом низу (не выдавлен)."""
    pytest.importorskip("textual")
    from textual.containers import VerticalScroll

    tid = "fc2"
    _write_task(tmp_path, tid)
    async with _app(tmp_path).run_test(size=(80, 24)) as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        big = "\n".join(f"строка {i}" for i in range(60))
        scr._render_record(
            {"payload": {"id": "a1", "role": "assistant", "content": big}},
            live=True,
        )
        await pilot.pause(0.4)
        trace = pilot.app.query_one("#trace", VerticalScroll)
        footer = pilot.app.query_one("#footer")
        assert trace.max_scroll_y > 0  # контент не влез — скроллится
        assert footer.region.bottom == 24  # хром у низа, футер на самом низу


@pytest.mark.asyncio
async def test_chrome_follows_as_content_grows(tmp_path):
    """Пока контент короткий, рост контента двигает футер ВНИЗ (хром следует) —
    нативно, без замера высоты контента в коде."""
    pytest.importorskip("textual")
    tid = "fc3"
    _write_task(tmp_path, tid)
    async with _app(tmp_path).run_test(
        size=(80, 40)
    ) as pilot:  # выше — короткий контент влезает под бренд-карточку
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        scr._render_record(
            {
                "payload": {
                    "id": "a1",
                    "role": "assistant",
                    "content": "первая строка",
                }
            },
            live=True,
        )
        await pilot.pause(0.4)
        footer = pilot.app.query_one("#footer")
        b1 = footer.region.bottom
        scr._render_record(
            {
                "payload": {
                    "id": "a2",
                    "role": "assistant",
                    "content": "\n".join(f"ещё {i}" for i in range(4)),
                }
            },
            live=True,
        )
        await pilot.pause(0.4)
        b2 = footer.region.bottom
        assert b1 < pilot.app.size.height  # короткий контент → футер не у низа
        assert b2 > b1  # контент вырос → футер поехал вниз (следует)


@pytest.mark.asyncio
async def test_sync_max_height_guard_stable(tmp_path):
    pytest.importorskip("textual")
    tid = "fc4"
    _write_task(tmp_path, tid)
    async with _app(tmp_path).run_test(size=(80, 24)) as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        await pilot.pause(0.4)
        first = scr._trace_max_height
        scr._sync_trace_max_height()  # без изменения размеров
        assert scr._trace_max_height == first  # стабильно (гард)
        assert first is not None and first > 0


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
