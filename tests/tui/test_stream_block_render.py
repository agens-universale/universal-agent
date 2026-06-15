"""StreamBlock — эфемерный live-рендер текущего chat_completions-шага.

Pure-helper гварды (без Textual) + интеграция через App.run_test():
sink → poll-тик → смонтирован StreamBlock с ожидаемым текстом;
каноничная assistant-строка → блока нет, sink сброшен.
"""
import json
from pathlib import Path

import pytest

from tui.services.stream_sink import StreamSink, format_stream_block


def test_block_text_from_sink():
    s = StreamSink()
    s.push("think", "читаю README")
    s.push("answer", "Это проект.")
    think, answer = s.snapshot()
    assert format_stream_block(think=think, answer=answer) == (
        "⠿ думаю… читаю README\nЭто проект."
    )


def test_sink_reset_clears_block():
    s = StreamSink()
    s.push("answer", "partial")
    s.reset()
    think, answer = s.snapshot()
    assert format_stream_block(think=think, answer=answer) == ""


def _write_task(tmp_path, tid):
    meta = {"thread_id": tid, "status": "running",
            "updated_at": "2026-06-15T12:00:00+00:00", "messages_count": 0}
    (Path(tmp_path) / f"meta_cli_{tid}.json").write_text(
        json.dumps(meta), encoding="utf-8",
    )
    (Path(tmp_path) / f"data_cli_{tid}.jsonl").write_text("", encoding="utf-8")


@pytest.mark.asyncio
async def test_stream_block_mounts_then_clears_on_canonical_row(tmp_path):
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.widgets.stream_block import StreamBlock

    class _FakeRunner:
        def is_running(self) -> bool: return True
        def stop(self) -> None: pass

    tid = "stream1"
    _write_task(tmp_path, tid)
    app = TuiApp(threads_dir=str(tmp_path), system="cli",
                 config={"model": "M", "backend_api_mode": "chat_completions"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        pilot.app.active_runner = _FakeRunner()
        # Кормим sink как worker-поток через on_stream_delta.
        pilot.app._stream_sink.push("think", "читаю README")
        pilot.app._stream_sink.push("answer", "Это проект.")
        # Poll-тик (set_interval 250ms) монтирует StreamBlock на UI-потоке;
        # mount асинхронный — текст проставляется на следующем тике (≥2 тика).
        await pilot.pause(0.7)

        blocks = pilot.app.query(StreamBlock)
        assert len(blocks) == 1
        assert str(blocks[0].render()) == "⠿ думаю… читаю README\nЭто проект."

        # Каноничная assistant-строка пришла из thread store — блок снимается,
        # sink сбрасывается. Рендерим запись напрямую (live-путь _render_record).
        scr._render_record({
            "type": "message",
            "payload": {"id": "a1", "role": "assistant", "content": "Это проект."},
        }, live=True)
        await pilot.pause()

        assert len(pilot.app.query(StreamBlock)) == 0
        assert pilot.app._stream_sink.snapshot() == ("", "")


@pytest.mark.asyncio
async def test_reset_render_state_clears_stream_block_and_sink(tmp_path):
    """Task-switch path: _reset_render_state must remove the StreamBlock AND reset the sink."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.widgets.stream_block import StreamBlock

    class _FakeRunner:
        def is_running(self) -> bool: return True
        def stop(self) -> None: pass

    tid = "stream3"
    _write_task(tmp_path, tid)
    app = TuiApp(threads_dir=str(tmp_path), system="cli",
                 config={"model": "M", "backend_api_mode": "chat_completions"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        pilot.app.active_runner = _FakeRunner()
        # Push content so sink is non-empty and a StreamBlock is mounted.
        pilot.app._stream_sink.push("answer", "partial answer")
        await pilot.pause(0.7)
        assert len(pilot.app.query(StreamBlock)) == 1

        # Simulate task-switch by calling _reset_render_state directly.
        scr._reset_render_state()
        await pilot.pause()

        assert len(pilot.app.query(StreamBlock)) == 0
        assert pilot.app._stream_sink.snapshot() == ("", "")


@pytest.mark.asyncio
async def test_stream_block_cleared_when_run_not_running(tmp_path):
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.widgets.stream_block import StreamBlock

    class _FakeRunner:
        def __init__(self) -> None:
            self._running = True
        def is_running(self) -> bool: return self._running
        def stop(self) -> None: self._running = False

    tid = "stream2"
    _write_task(tmp_path, tid)
    app = TuiApp(threads_dir=str(tmp_path), system="cli",
                 config={"model": "M", "backend_api_mode": "chat_completions"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        runner = _FakeRunner()
        pilot.app.active_runner = runner
        pilot.app._stream_sink.push("answer", "partial")
        await pilot.pause(0.7)
        assert len(pilot.app.query(StreamBlock)) == 1

        # Run завершился → не-running ветка poll'а снимает блок и сбрасывает sink.
        runner.stop()
        await pilot.pause(0.7)
        assert len(pilot.app.query(StreamBlock)) == 0
        assert pilot.app._stream_sink.snapshot() == ("", "")
