import json
from pathlib import Path

import pytest


def _write_task(tmp_path, tid):
    meta = {"thread_id": tid, "status": "running",
            "updated_at": "2026-06-15T12:00:00+00:00", "messages_count": 0}
    (Path(tmp_path) / f"meta_cli_{tid}.json").write_text(json.dumps(meta), encoding="utf-8")
    (Path(tmp_path) / f"data_cli_{tid}.jsonl").write_text("", encoding="utf-8")


@pytest.mark.asyncio
async def test_short_content_trace_shrinks_indicator_follows(tmp_path):
    pytest.importorskip("textual")
    from textual.containers import Vertical, VerticalScroll
    from tui.app import TuiApp

    tid = "fc1"
    _write_task(tmp_path, tid)
    app = TuiApp(threads_dir=str(tmp_path), system="cli",
                 config={"model": "M", "backend_api_mode": "chat_completions"})
    async with app.run_test(size=(80, 24)) as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        scr._render_record({"payload": {"id": "a1", "role": "assistant",
                                        "content": "короткий ответ"}}, live=True)
        await pilot.pause(0.4)                  # poll-тик → _sync_trace_max_height
        trace = pilot.app.query_one("#trace", VerticalScroll)
        spacer = pilot.app.query_one("#spacer", Vertical)
        assert trace.max_scroll_y == 0          # контент влез — нет скролла
        assert spacer.size.height > 0           # спейсер забрал низ → индикатор НЕ у низа


@pytest.mark.asyncio
async def test_tall_content_trace_caps_indicator_at_bottom(tmp_path):
    pytest.importorskip("textual")
    from textual.containers import Vertical, VerticalScroll
    from tui.app import TuiApp

    tid = "fc2"
    _write_task(tmp_path, tid)
    app = TuiApp(threads_dir=str(tmp_path), system="cli",
                 config={"model": "M", "backend_api_mode": "chat_completions"})
    async with app.run_test(size=(80, 24)) as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        big = "\n".join(f"строка {i}" for i in range(60))
        scr._render_record({"payload": {"id": "a1", "role": "assistant",
                                        "content": big}}, live=True)
        await pilot.pause(0.4)
        trace = pilot.app.query_one("#trace", VerticalScroll)
        spacer = pilot.app.query_one("#spacer", Vertical)
        footer = pilot.app.query_one("#footer")
        assert trace.max_scroll_y > 0           # контент не влез — скроллится
        assert spacer.size.height == 0          # слака нет → индикатор у низа
        assert footer.region.bottom == 24       # футер на самом низу (не выдавлен)


@pytest.mark.asyncio
async def test_sync_max_height_guard_stable(tmp_path):
    pytest.importorskip("textual")
    from tui.app import TuiApp

    tid = "fc3"
    _write_task(tmp_path, tid)
    app = TuiApp(threads_dir=str(tmp_path), system="cli",
                 config={"model": "M", "backend_api_mode": "chat_completions"})
    async with app.run_test(size=(80, 24)) as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        await pilot.pause(0.4)
        first = scr._trace_max_height
        scr._sync_trace_max_height()            # без изменения размеров
        assert scr._trace_max_height == first   # стабильно (гард)
        assert first is not None and first > 0


@pytest.mark.asyncio
async def test_spacer_shrinks_as_content_grows(tmp_path):
    """Динамика «следования»: по мере роста контента (как при стриме) спейсер
    сжимается → индикатор едет за низом контента. Спейсер обновляется на каждом
    poll-тике (вне гарда max_height), а не только при ресайзе."""
    pytest.importorskip("textual")
    from textual.containers import Vertical
    from tui.app import TuiApp

    tid = "fc4"
    _write_task(tmp_path, tid)
    app = TuiApp(threads_dir=str(tmp_path), system="cli",
                 config={"model": "M", "backend_api_mode": "chat_completions"})
    async with app.run_test(size=(80, 24)) as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        scr._render_record({"payload": {"id": "a1", "role": "assistant",
                                        "content": "первая строка"}}, live=True)
        await pilot.pause(0.4)
        spacer = pilot.app.query_one("#spacer", Vertical)
        h1 = spacer.size.height                 # короткий контент → большой спейсер
        scr._render_record({"payload": {"id": "a2", "role": "assistant",
                                        "content": "\n".join(f"ещё {i}" for i in range(8))}},
                           live=True)
        await pilot.pause(0.4)
        h2 = spacer.size.height
        assert h1 > 0                            # был запас (индикатор не у низа)
        assert h2 < h1                           # контент вырос → спейсер сжался (индикатор поехал вниз)
