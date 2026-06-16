"""StreamBlock — эфемерный live-рендер текущего chat_completions-шага.

Pure-helper гварды (без Textual) + интеграция через App.run_test():
sink → poll-тик → смонтирован StreamBlock с ожидаемым текстом;
каноничная assistant-строка → блока нет, sink сброшен.
"""
import json
from pathlib import Path

import pytest

from tui.services.stream_sink import StreamSink, format_stream_block, format_reasoning_line


def test_block_text_from_sink_answer_wins():
    """Answer present: drain shows answer text, thinking=False."""
    s = StreamSink()
    s.push("think", "читаю README")
    s.push("answer", "Это проект.")
    think, answer = s.snapshot()
    ans = format_stream_block(answer=answer)
    assert ans == "Это проект."
    # Drain logic: answer present → use answer, not reasoning line
    assert ans  # truthy → thinking=False path


def test_block_text_from_sink_reasoning_only():
    """No answer yet: drain shows reasoning line (thinking=True)."""
    s = StreamSink()
    s.push("think", "анализирую структуру проекта")
    think, answer = s.snapshot()
    ans = format_stream_block(answer=answer)
    assert ans == ""  # no answer → empty
    # Drain logic: ans falsy → reasoning line
    line = format_reasoning_line(think=think)
    assert line == "⠇ анализирую структуру проекта"


def test_sink_reset_clears_block():
    s = StreamSink()
    s.push("answer", "partial")
    s.reset()
    _think, answer = s.snapshot()
    assert format_stream_block(answer=answer) == ""


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
        assert str(blocks[0].render()) == "Это проект."

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


@pytest.mark.asyncio
async def test_reasoning_line_below_committed_rows(tmp_path):
    """StreamBlock (эфемерная reasoning-строка) монтируется НИЖЕ уже
    зафиксированных canonical-виджетов, а не поверх них."""
    pytest.importorskip("textual")
    from textual.containers import VerticalScroll
    from tui.app import TuiApp
    from tui.widgets.stream_block import StreamBlock
    from tui.widgets.thinking_indicator import ThinkingIndicator

    class _FakeRunner:
        def is_running(self) -> bool: return True
        def stop(self) -> None: pass

    tid = "stream4"
    _write_task(tmp_path, tid)
    app = TuiApp(threads_dir=str(tmp_path), system="cli",
                 config={"model": "M", "backend_api_mode": "chat_completions"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        pilot.app.active_runner = _FakeRunner()

        # Зафиксировать canonical tool-call строку (ToolCallRow) в #trace.
        scr._render_record({
            "payload": {
                "type": "function_call",
                "name": "read_file",
                "arguments": "{}",
                "call_id": "c1",
            }
        })
        await pilot.pause()

        # Подать reasoning-дельту — poll-тик смонтирует StreamBlock ниже.
        pilot.app._stream_sink.push("think", "новый шаг рассуждения")
        await pilot.pause(0.7)  # ≥2 тика по 250ms

        trace = pilot.app.query_one("#trace", VerticalScroll)
        kids = list(trace.children)

        # Должен быть ровно один StreamBlock.
        sb_indices = [i for i, w in enumerate(kids) if isinstance(w, StreamBlock)]
        assert sb_indices, "StreamBlock not mounted"
        sb_idx = sb_indices[0]

        # Зафиксированные строки — всё кроме StreamBlock и ThinkingIndicator.
        committed_indices = [
            i for i, w in enumerate(kids)
            if not isinstance(w, (StreamBlock, ThinkingIndicator))
        ]
        assert committed_indices, "no committed row found in #trace"

        # Reasoning-строка должна быть ниже последней canonical-строки.
        assert sb_idx > max(committed_indices), (
            "StreamBlock must be below committed rows, "
            f"but sb_idx={sb_idx} <= last committed={max(committed_indices)}"
        )


@pytest.mark.asyncio
async def test_reasoning_block_cleared_when_tool_row_renders(tmp_path):
    """Fix A: StreamBlock снимается когда рендерится canonical tool-call строка,
    чтобы не дрейфовать выше неё."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.widgets.stream_block import StreamBlock

    class _FakeRunner:
        def is_running(self) -> bool: return True
        def stop(self) -> None: pass

    tid = "stream5"
    _write_task(tmp_path, tid)
    app = TuiApp(threads_dir=str(tmp_path), system="cli",
                 config={"model": "M", "backend_api_mode": "chat_completions"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        pilot.app.active_runner = _FakeRunner()

        # Подаём reasoning — poll-тик монтирует StreamBlock.
        pilot.app._stream_sink.push("think", "анализирую")
        await pilot.pause(0.7)
        assert pilot.app.query(StreamBlock), "StreamBlock должен быть смонтирован"

        # Canonical tool-call строка рендерится → StreamBlock должен быть снят.
        scr._render_record({
            "payload": {
                "type": "function_call",
                "name": "fs_read_file",
                "call_id": "c1",
                "arguments": "{}",
            }
        })
        await pilot.pause()
        assert not pilot.app.query(StreamBlock), (
            "StreamBlock должен быть снят при рендере canonical tool-call строки"
        )


@pytest.mark.asyncio
async def test_no_reasoning_block_in_validating_phase(tmp_path):
    """Fix B: StreamBlock не показывается в фазе validating
    (после финального ответа, пока идёт intent_guard)."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.widgets.stream_block import StreamBlock

    class _FakeRunner:
        def is_running(self) -> bool: return True
        def stop(self) -> None: pass

    tid = "stream6"
    _write_task(tmp_path, tid)
    app = TuiApp(threads_dir=str(tmp_path), system="cli",
                 config={"model": "M", "backend_api_mode": "chat_completions"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        pilot.app.active_runner = _FakeRunner()

        # Переводим в фазу validating (финальный ответ уже виден).
        scr._last_event_kind = "_finalized"

        # Подаём reasoning от intent_guard — StreamBlock НЕ должен появиться.
        pilot.app._stream_sink.push("think", "пост-валидация рассуждение")
        await pilot.pause(0.7)
        assert not pilot.app.query(StreamBlock), (
            "StreamBlock не должен отображаться в фазе validating"
        )


@pytest.mark.asyncio
async def test_stream_answer_preview_false_hides_answer_block(tmp_path):
    """stream_answer_preview=False: answer delta → NO StreamBlock shown,
    but sink is NOT reset (counter preserved)."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.widgets.stream_block import StreamBlock

    class _FakeRunner:
        def is_running(self) -> bool: return True
        def stop(self) -> None: pass

    tid = "stream7"
    _write_task(tmp_path, tid)
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli",
        config={"model": "M", "backend_api_mode": "chat_completions",
                "stream_answer_preview": False},
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        pilot.app.active_runner = _FakeRunner()
        # Push answer delta — with preview disabled the block must NOT appear.
        pilot.app._stream_sink.push("answer", "секретный ответ")
        await pilot.pause(0.7)

        # No StreamBlock visible.
        assert not pilot.app.query(StreamBlock), (
            "StreamBlock не должен отображаться при stream_answer_preview=False"
        )
        # Sink NOT reset — counter keeps growing (indicator still alive).
        assert pilot.app._stream_sink.progress_units() > 0, (
            "Sink должен быть жив (reset_sink=False)"
        )


@pytest.mark.asyncio
async def test_stream_answer_preview_false_shows_reasoning_line(tmp_path):
    """stream_answer_preview=False: think-only delta → reasoning StreamBlock
    IS shown (flag only gates the answer-mode block)."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.widgets.stream_block import StreamBlock

    class _FakeRunner:
        def is_running(self) -> bool: return True
        def stop(self) -> None: pass

    tid = "stream8"
    _write_task(tmp_path, tid)
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli",
        config={"model": "M", "backend_api_mode": "chat_completions",
                "stream_answer_preview": False},
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        pilot.app.active_runner = _FakeRunner()
        # Push reasoning delta only (no answer).
        pilot.app._stream_sink.push("think", "обдумываю задачу")
        await pilot.pause(0.7)

        # Reasoning StreamBlock IS shown.
        blocks = pilot.app.query(StreamBlock)
        assert blocks, "StreamBlock с reasoning-строкой должен быть показан"
        assert "⠇" in str(blocks[0].render()), (
            "Reasoning-строка должна содержать ⠇"
        )
