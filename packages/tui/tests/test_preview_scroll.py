"""Регресс: #trace на read-only экранах (PreviewScreen и т.п., НЕ TraceScreen)
должен заполнять экран и скроллиться при переполнении.

#trace стал height:auto ради follows-content на TraceScreen (потолок ставит код).
Но id="trace" переиспользуют 7 экранов; вне TraceScreen потолка нет → auto рос бы
до высоты контента → max_scroll_y=0 → ни скроллбара, ни прокрутки. Дефолт #trace
должен оставаться 1fr; auto — только на TraceScreen."""
import pytest


@pytest.mark.asyncio
async def test_preview_trace_scrolls_when_overflow(tmp_path):
    pytest.importorskip("textual")
    from textual.containers import VerticalScroll
    from gc_tui.app import TuiApp
    from gc_tui.screens.preview import PreviewScreen

    msgs = [{"payload": {"id": f"m{i}", "role": "assistant",
                         "content": f"строка ответа номер {i}"}} for i in range(60)]
    segments = [{"messages": msgs, "index": 0, "compacted_by": None}]
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test(size=(80, 24)) as pilot:
        screen = PreviewScreen(segments=segments, thread_id="abc12345", project_path="/p")
        await pilot.app.push_screen(screen)
        await pilot.pause(0.4)
        trace = screen.query_one("#trace", VerticalScroll)
        assert trace.max_scroll_y > 0          # заполняет экран и скроллится


@pytest.mark.asyncio
async def test_main_trace_still_follows_content(tmp_path):
    """TraceScreen сохраняет follows-content (#trace auto): короткий контент не
    скроллится (хром следует за концом)."""
    pytest.importorskip("textual")
    import json
    from pathlib import Path
    from textual.containers import VerticalScroll
    from gc_tui.app import TuiApp

    tid = "pv1"
    meta = {"thread_id": tid, "status": "running",
            "updated_at": "2026-06-15T12:00:00+00:00", "messages_count": 0}
    (Path(tmp_path) / f"meta_cli_{tid}.json").write_text(json.dumps(meta), encoding="utf-8")
    (Path(tmp_path) / f"data_cli_{tid}.jsonl").write_text("", encoding="utf-8")
    app = TuiApp(threads_dir=str(tmp_path), system="cli",
                 config={"model": "M", "backend_api_mode": "chat_completions"})
    async with app.run_test(size=(80, 40)) as pilot:  # выше — короткий контент влезает под бренд-карточку
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        scr._render_record({"payload": {"id": "a1", "role": "assistant",
                                        "content": "короткий"}}, live=True)
        await pilot.pause(0.4)
        footer = pilot.app.query_one("#footer")
        assert footer.region.bottom < pilot.app.size.height  # хром следует за коротким контентом
