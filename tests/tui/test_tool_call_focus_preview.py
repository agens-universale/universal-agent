"""Focus-only preview под ToolCallRow: появляется при focus, скрывается при blur."""
import json
from pathlib import Path

import pytest


def _write_thread(tmp_path, tid, *, result_output=None):
    (Path(tmp_path) / f"meta_cli_{tid}.json").write_text(
        json.dumps({"thread_id": tid, "status": "stopped",
                    "updated_at": "2026-05-27T12:00:00+00:00", "messages_count": 3}),
        encoding="utf-8")
    lines = [
        {"type": "message", "seq": 1, "ts_utc": "2026-05-27T12:00:00+00:00",
         "payload": {"id": "u1", "role": "user", "content": "запрос"}},
        {"type": "message", "seq": 2, "ts_utc": "2026-05-27T12:00:01+00:00",
         "payload": {"type": "function_call", "name": "read_file",
                     "arguments": {"path": "x.py"}, "call_id": "c1"}},
    ]
    if result_output is not None:
        lines.append({"type": "message", "seq": 3, "ts_utc": "2026-05-27T12:00:02+00:00",
                      "payload": {"type": "function_call_output", "call_id": "c1",
                                  "output": result_output}})
    (Path(tmp_path) / f"data_cli_{tid}.jsonl").write_text(
        "\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")


@pytest.mark.asyncio
async def test_preview_widget_appears_on_focus_and_hides_on_blur(tmp_path):
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.widgets.tool_call_row import ToolCallRow

    tid = "focus_test"
    _write_thread(tmp_path, tid, result_output=json.dumps(
        {"ok": True, "content": "def main():\n    pass\n    return 0"}))

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        pilot.app.screen.attach_active_task(tid)
        await pilot.pause(0.3)

        # Preview-widget смонтирован, но с .hidden пока row не в фокусе.
        previews = list(pilot.app.screen.query(".tool-result-preview"))
        assert len(previews) == 1
        assert "hidden" in previews[0].classes

        # Фокусируем row → preview становится видимым.
        row = pilot.app.screen.query_one(ToolCallRow)
        row.focus()
        await pilot.pause(0.2)
        previews = list(pilot.app.screen.query(".tool-result-preview"))
        assert "hidden" not in previews[0].classes
        rendered = str(previews[0].render())
        assert "def main():" in rendered

        # Снимаем фокус с row, фокусируя командное поле ввода (всегда есть на TraceScreen).
        pilot.app.query_one("#cmdinput").focus()
        await pilot.pause(0.2)
        previews = list(pilot.app.screen.query(".tool-result-preview"))
        assert "hidden" in previews[0].classes


@pytest.mark.asyncio
async def test_write_style_result_no_preview_widget(tmp_path):
    """Result-only {ok: true} → extract_preview_lines возвращает ([], 0)
    → preview-widget вовсе не монтируется."""
    pytest.importorskip("textual")
    from tui.app import TuiApp

    tid = "write_test"
    _write_thread(tmp_path, tid, result_output=json.dumps({"ok": True}))

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        pilot.app.screen.attach_active_task(tid)
        await pilot.pause(0.3)

        previews = list(pilot.app.screen.query(".tool-result-preview"))
        assert len(previews) == 0


@pytest.mark.asyncio
async def test_tab_between_rows_swaps_preview_visibility(tmp_path):
    """Focus row A → preview A видна; tab/focus на row B → A hidden, B видна."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.widgets.tool_call_row import ToolCallRow

    tid = "tab_test"
    # Два tool-call'а в одном треде, оба с непустым output (есть preview).
    (Path(tmp_path) / f"meta_cli_{tid}.json").write_text(
        json.dumps({"thread_id": tid, "status": "stopped",
                    "updated_at": "2026-05-27T12:00:00+00:00", "messages_count": 5}),
        encoding="utf-8")
    lines = [
        {"type": "message", "seq": 1, "ts_utc": "2026-05-27T12:00:00+00:00",
         "payload": {"id": "u1", "role": "user", "content": "запрос"}},
        {"type": "message", "seq": 2, "ts_utc": "2026-05-27T12:00:01+00:00",
         "payload": {"type": "function_call", "name": "read_file",
                     "arguments": {"path": "a.py"}, "call_id": "cA"}},
        {"type": "message", "seq": 3, "ts_utc": "2026-05-27T12:00:02+00:00",
         "payload": {"type": "function_call_output", "call_id": "cA",
                     "output": json.dumps({"ok": True, "content": "A-line1\nA-line2"})}},
        {"type": "message", "seq": 4, "ts_utc": "2026-05-27T12:00:03+00:00",
         "payload": {"type": "function_call", "name": "read_file",
                     "arguments": {"path": "b.py"}, "call_id": "cB"}},
        {"type": "message", "seq": 5, "ts_utc": "2026-05-27T12:00:04+00:00",
         "payload": {"type": "function_call_output", "call_id": "cB",
                     "output": json.dumps({"ok": True, "content": "B-line1\nB-line2"})}},
    ]
    (Path(tmp_path) / f"data_cli_{tid}.jsonl").write_text(
        "\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        pilot.app.screen.attach_active_task(tid)
        await pilot.pause(0.3)

        rows = list(pilot.app.screen.query(ToolCallRow))
        assert len(rows) == 2
        # Оба preview смонтированы, оба .hidden.
        previews = list(pilot.app.screen.query(".tool-result-preview"))
        assert len(previews) == 2

        # Focus row A → его preview visible, B остаётся hidden.
        rows[0].focus()
        await pilot.pause(0.2)
        previews = list(pilot.app.screen.query(".tool-result-preview"))
        # Порядок preview в DOM повторяет порядок rows: сначала A, потом B.
        assert "hidden" not in previews[0].classes
        assert "hidden" in previews[1].classes
        assert "A-line1" in str(previews[0].render())

        # Focus row B → A hidden, B visible.
        rows[1].focus()
        await pilot.pause(0.2)
        previews = list(pilot.app.screen.query(".tool-result-preview"))
        assert "hidden" in previews[0].classes
        assert "hidden" not in previews[1].classes
        assert "B-line1" in str(previews[1].render())


@pytest.mark.asyncio
async def test_pending_row_no_preview_widget(tmp_path):
    """Result ещё не пришёл → preview-widget не существует."""
    pytest.importorskip("textual")
    from tui.app import TuiApp

    tid = "pending_test"
    _write_thread(tmp_path, tid, result_output=None)

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        pilot.app.screen.attach_active_task(tid)
        await pilot.pause(0.3)

        previews = list(pilot.app.screen.query(".tool-result-preview"))
        assert len(previews) == 0
