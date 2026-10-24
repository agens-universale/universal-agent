"""Регресс: #trace на read-only экранах (PreviewScreen и т.п., НЕ TraceScreen)
должен заполнять экран и скроллиться при переполнении.

id="trace" переиспользуют 7 экранов; с height:auto без потолка #trace рос бы
до высоты контента → max_scroll_y=0 → ни скроллбара, ни прокрутки. Дефолт
#trace — 1fr. Раскладку главного экрана (ввод у низа) стережёт
test_input_pinned_bottom.py."""

from gc_testkit.tui_support import tui_settings
import pytest


@pytest.mark.asyncio
async def test_preview_trace_scrolls_when_overflow(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.preview import PreviewScreen
    from textual.containers import VerticalScroll

    msgs = [
        {
            "payload": {
                "id": f"m{i}",
                "role": "assistant",
                "content": f"строка ответа номер {i}",
            }
        }
        for i in range(60)
    ]
    segments = [{"messages": msgs, "index": 0, "compacted_by": None}]
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test(size=(80, 24)) as pilot:
        screen = PreviewScreen(
            segments=segments, thread_id="abc12345", project_path="/p"
        )
        await pilot.app.push_screen(screen)
        await pilot.pause(0.4)
        trace = screen.query_one("#trace", VerticalScroll)
        assert trace.max_scroll_y > 0  # заполняет экран и скроллится
