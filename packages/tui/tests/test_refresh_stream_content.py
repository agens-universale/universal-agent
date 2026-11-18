import json
from pathlib import Path

import pytest
from gc_testkit.tui_support import tui_settings


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


class _FakeRunner:
    def is_running(self) -> bool:
        return True

    def stop(self) -> None:
        pass


@pytest.mark.asyncio
async def test_refresh_updates_mounted_answer_row(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from textual.widgets import Static

    tid = "ref1"
    _write_task(tmp_path, tid)
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings(
            {
                "agent.model": "M",
                "backend.api_mode": "chat_completions",
                "tui.stream_display": "answer",
            }
        ),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        pilot.app.active_runner = _FakeRunner()
        pilot.app._stream_sink.push("answer", "первая часть")
        await pilot.pause(0.7)  # poll смонтировал answer-row
        assert scr._stream_answer_row is not None
        pilot.app._stream_sink.push("answer", " и вторая")
        scr._refresh_stream_content()  # прямой вызов (как событие)
        content = scr._stream_answer_row.query_one(".trace-content", Static)
        assert "и вторая" in str(content.render())


@pytest.mark.asyncio
async def test_refresh_noop_when_nothing_mounted(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    tid = "ref2"
    _write_task(tmp_path, tid)
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings(
            {"agent.model": "M", "backend.api_mode": "chat_completions"}
        ),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        scr._refresh_stream_content()  # ни виджета — без исключений
        assert scr._stream_answer_row is None


def _stream_app(tmp_path, tid, display="answer"):
    from gc_tui.app import TuiApp

    _write_task(tmp_path, tid)
    return TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings(
            {
                "agent.model": "M",
                "backend.api_mode": "chat_completions",
                "tui.stream_display": display,
            }
        ),
    )


async def _drawn_answer_row(pilot, tid):
    """Опрос смонтировал строку ответа и нарисовал текущую версию."""
    scr = pilot.app.screen
    scr.attach_active_task(tid)
    pilot.app.active_runner = _FakeRunner()
    pilot.app._stream_sink.push("answer", "первая часть")
    await pilot.pause(0.7)
    assert scr._stream_answer_row is not None
    return scr


def _spy_set_content(monkeypatch, row):
    calls = []
    original = row.set_content

    def spy(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(row, "set_content", spy)
    return calls


@pytest.mark.asyncio
async def test_refresh_without_new_data_skips_set_content(
    tmp_path, monkeypatch
):
    pytest.importorskip("textual")
    app = _stream_app(tmp_path, "ref3")
    async with app.run_test() as pilot:
        scr = await _drawn_answer_row(pilot, "ref3")
        calls = _spy_set_content(monkeypatch, scr._stream_answer_row)
        pilot.app._stream_sink.push("answer", " и вторая")
        scr._refresh_stream_content()
        scr._refresh_stream_content()
        assert len(calls) == 1


@pytest.mark.asyncio
async def test_refresh_after_new_delta_redraws(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    app = _stream_app(tmp_path, "ref4")
    async with app.run_test() as pilot:
        scr = await _drawn_answer_row(pilot, "ref4")
        calls = _spy_set_content(monkeypatch, scr._stream_answer_row)
        pilot.app._stream_sink.push("answer", " и вторая")
        scr._refresh_stream_content()
        pilot.app._stream_sink.push("answer", " и третья")
        scr._refresh_stream_content()
        assert len(calls) == 2


@pytest.mark.asyncio
async def test_refresh_after_widget_switch_redraws(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    from gc_tui.widgets.trace_row import TraceRow

    app = _stream_app(tmp_path, "ref5")
    async with app.run_test() as pilot:
        scr = await _drawn_answer_row(pilot, "ref5")
        scr._stream_answer_row.remove()
        row = TraceRow(prefix="● ", content="", kind_class="line-agent")
        scr._stream_answer_row = row
        calls = _spy_set_content(monkeypatch, row)
        scr._refresh_stream_content()
        assert len(calls) == 1


@pytest.mark.asyncio
async def test_frozen_select_does_not_record_drawn(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    app = _stream_app(tmp_path, "ref6")
    async with app.run_test() as pilot:
        scr = await _drawn_answer_row(pilot, "ref6")
        calls = _spy_set_content(monkeypatch, scr._stream_answer_row)
        frozen = [True]
        monkeypatch.setattr(scr, "_ui_frozen_for_select", lambda: frozen[0])
        pilot.app._stream_sink.push("answer", " и вторая")
        scr._refresh_stream_content()
        assert calls == []
        frozen[0] = False
        scr._refresh_stream_content()
        assert len(calls) == 1


@pytest.mark.asyncio
async def test_stream_block_mounted_same_tick_gets_text_next_call(tmp_path):
    """Опрос монтирует StreamBlock и в том же обработчике зовёт
    перерисовку — у несмонтированного блока текст не ложится; следующий
    вызов без новой дельты обязан его положить."""
    pytest.importorskip("textual")
    app = _stream_app(tmp_path, "ref7", display="answer_reasoning")
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task("ref7")
        pilot.app.active_runner = _FakeRunner()
        pilot.app._stream_sink.push("think", "обдумываю план")
        await pilot.pause(0.7)
        block = scr._stream_block
        assert block is not None
        scr._refresh_stream_content()
        assert "обдумываю план" in str(block.render())


@pytest.mark.asyncio
async def test_clear_stream_block_resets_drawn(tmp_path):
    pytest.importorskip("textual")
    app = _stream_app(tmp_path, "ref8")
    async with app.run_test() as pilot:
        scr = await _drawn_answer_row(pilot, "ref8")
        scr._clear_stream_block()
        assert scr._stream_drawn is None
