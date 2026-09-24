"""Ctrl+клик по строке tool-вызова открывает полный payload в Viewer (двойной
клик больше не открывает — см. tests/tui/test_trace_click_model.py)."""

import json
from pathlib import Path

import pytest


def _write(tmp_path, tid):
    (Path(tmp_path) / f"meta_cli_{tid}.json").write_text(
        json.dumps(
            {
                "thread_id": tid,
                "status": "stopped",
                "updated_at": "2026-05-22T12:00:00+00:00",
                "messages_count": 2,
            }
        ),
        encoding="utf-8",
    )
    lines = [
        {
            "type": "message",
            "seq": 1,
            "ts_utc": "2026-05-22T12:00:00+00:00",
            "payload": {
                "id": "u1",
                "role": "user",
                "content": "прочитай файл",
            },
        },
        {
            "type": "message",
            "seq": 2,
            "ts_utc": "2026-05-22T12:00:01+00:00",
            "payload": {
                "type": "function_call",
                "name": "read_file",
                "arguments": {"path": "x.py"},
                "call_id": "c1",
            },
        },
    ]
    (Path(tmp_path) / f"data_cli_{tid}.jsonl").write_text(
        "\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8"
    )


@pytest.mark.asyncio
async def test_ctrl_click_tool_row_opens_detail_screen(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.tool_call_detail import ToolCallDetailScreen
    from gc_tui.widgets.tool_call_row import ToolCallRow
    from textual import events

    tid = "t1"
    _write(tmp_path, tid)

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", config={"model": "M"}
    )
    async with app.run_test() as pilot:
        pilot.app.screen.attach_active_task(tid)
        await pilot.pause(0.2)

        row = pilot.app.screen.query_one(ToolCallRow)
        click = events.Click(
            widget=row,
            x=0,
            y=0,
            delta_x=0,
            delta_y=0,
            button=1,
            shift=False,
            meta=False,
            ctrl=True,
            chain=1,
        )
        row.on_click(click)
        await pilot.pause(0.2)

        assert isinstance(pilot.app.screen, ToolCallDetailScreen)


@pytest.mark.asyncio
async def test_enter_on_focused_row_opens_detail_screen(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.tool_call_detail import ToolCallDetailScreen
    from gc_tui.widgets.tool_call_row import ToolCallRow

    tid = "t3"
    _write(tmp_path, tid)

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", config={"model": "M"}
    )
    async with app.run_test() as pilot:
        pilot.app.screen.attach_active_task(tid)
        await pilot.pause(0.2)

        row = pilot.app.screen.query_one(ToolCallRow)
        row.focus()
        await pilot.pause(0.1)
        await pilot.press("enter")
        await pilot.pause(0.2)

        assert isinstance(pilot.app.screen, ToolCallDetailScreen)


@pytest.mark.asyncio
async def test_ctrl_click_on_agent_prose_opens_correct_event(tmp_path):
    """Regression: после multi-widget рендера агентских реплик Ctrl+клик по
    prose/code-panel должен открывать payload ИМЕННО этого события (не соседнего).
    До фикса: prose/code Static не имели event_no — клик буллил к соседу."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.viewer import ViewerScreen
    from gc_tui.widgets.block_panel import BlockPanel
    from gc_tui.widgets.trace_row import TraceRow
    from textual import events

    tid = "agent_click"
    # Сценарий: user → tool_call → agent (с кодом). Цель — кликнуть по prose
    # агентской реплики и проверить что Viewer открыл её payload (не tool_call).
    (Path(tmp_path) / f"meta_cli_{tid}.json").write_text(
        json.dumps(
            {
                "thread_id": tid,
                "status": "stopped",
                "updated_at": "2026-05-26T12:00:00+00:00",
                "messages_count": 3,
            }
        ),
        encoding="utf-8",
    )
    lines = [
        {
            "type": "message",
            "seq": 1,
            "ts_utc": "2026-05-26T12:00:00+00:00",
            "payload": {"id": "u1", "role": "user", "content": "запрос"},
        },
        {
            "type": "message",
            "seq": 2,
            "ts_utc": "2026-05-26T12:00:01+00:00",
            "payload": {
                "type": "function_call",
                "name": "read_file",
                "arguments": {"path": "x.py"},
                "call_id": "c1",
            },
        },
        {
            "type": "message",
            "seq": 3,
            "ts_utc": "2026-05-26T12:00:02+00:00",
            "payload": {
                "id": "a1",
                "role": "assistant",
                "content": "Готово:\n```python\nx = 1\n```\nКонец.",
            },
        },
    ]
    (Path(tmp_path) / f"data_cli_{tid}.jsonl").write_text(
        "\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8"
    )

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", config={"model": "M"}
    )
    async with app.run_test() as pilot:
        pilot.app.screen.attach_active_task(tid)
        await pilot.pause(0.3)

        scr = pilot.app.screen
        # Prose-блоки — TraceRow с event_no; code-block panel — BlockPanel.
        prose_rows = [
            r for r in scr.query(TraceRow) if r.has_class("line-agent")
        ]
        code_panels = list(scr.query(BlockPanel))
        # Минимум: 2 prose-блока ("Готово:" и "Конец.") + 1 code-panel.
        assert len(prose_rows) >= 2
        assert len(code_panels) >= 1
        # Все агентские виджеты имеют один и тот же event_no — событие №3.
        agent_no = prose_rows[0]._event_no
        assert all(w._event_no == agent_no for w in prose_rows)
        assert all(w._event_no == agent_no for w in code_panels)

        # Ctrl+клик по любому prose-блоку — открывает Viewer с заголовком,
        # содержащим именно этот event_no и «agent».
        target = prose_rows[0]
        click = events.Click(
            widget=target,
            x=0,
            y=0,
            delta_x=0,
            delta_y=0,
            button=1,
            shift=False,
            meta=False,
            ctrl=True,
            chain=1,
        )
        target.on_click(click)
        await pilot.pause(0.2)

        assert isinstance(pilot.app.screen, ViewerScreen)
        # Title должен явно содержать «agent», не «read_file» соседнего call.
        header_text = str(pilot.app.screen.query_one("#header-left").render())
        assert "agent" in header_text
        assert "read_file" not in header_text


@pytest.mark.asyncio
async def test_ctrl_click_on_agent_code_panel_opens_correct_event(tmp_path):
    """Тот же сценарий, но Ctrl+клик — реально по BlockPanel (code-panel),
    а не по соседнему TraceRow (prose)."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.viewer import ViewerScreen
    from gc_tui.widgets.block_panel import BlockPanel
    from textual import events

    tid = "agent_click_panel"
    (Path(tmp_path) / f"meta_cli_{tid}.json").write_text(
        json.dumps(
            {
                "thread_id": tid,
                "status": "stopped",
                "updated_at": "2026-05-26T12:00:00+00:00",
                "messages_count": 3,
            }
        ),
        encoding="utf-8",
    )
    lines = [
        {
            "type": "message",
            "seq": 1,
            "ts_utc": "2026-05-26T12:00:00+00:00",
            "payload": {"id": "u1", "role": "user", "content": "запрос"},
        },
        {
            "type": "message",
            "seq": 2,
            "ts_utc": "2026-05-26T12:00:01+00:00",
            "payload": {
                "type": "function_call",
                "name": "read_file",
                "arguments": {"path": "x.py"},
                "call_id": "c1",
            },
        },
        {
            "type": "message",
            "seq": 3,
            "ts_utc": "2026-05-26T12:00:02+00:00",
            "payload": {
                "id": "a1",
                "role": "assistant",
                "content": "Готово:\n```python\nx = 1\n```\nКонец.",
            },
        },
    ]
    (Path(tmp_path) / f"data_cli_{tid}.jsonl").write_text(
        "\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8"
    )

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", config={"model": "M"}
    )
    async with app.run_test() as pilot:
        pilot.app.screen.attach_active_task(tid)
        await pilot.pause(0.3)

        scr = pilot.app.screen
        code_panels = list(scr.query(BlockPanel))
        assert len(code_panels) >= 1
        target = code_panels[0]
        agent_no = target._event_no

        click = events.Click(
            widget=target,
            x=0,
            y=0,
            delta_x=0,
            delta_y=0,
            button=1,
            shift=False,
            meta=False,
            ctrl=True,
            chain=1,
        )
        target.on_click(click)
        await pilot.pause(0.2)

        assert isinstance(pilot.app.screen, ViewerScreen)
        header_text = str(pilot.app.screen.query_one("#header-left").render())
        assert "agent" in header_text
        assert "read_file" not in header_text
        assert agent_no != 0


@pytest.mark.asyncio
async def test_single_click_does_not_open_viewer(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.viewer import ViewerScreen
    from gc_tui.widgets.tool_call_row import ToolCallRow
    from textual import events

    tid = "t2"
    _write(tmp_path, tid)

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", config={"model": "M"}
    )
    async with app.run_test() as pilot:
        pilot.app.screen.attach_active_task(tid)
        await pilot.pause(0.2)

        row = pilot.app.screen.query_one(ToolCallRow)
        click = events.Click(
            widget=row,
            x=0,
            y=0,
            delta_x=0,
            delta_y=0,
            button=1,
            shift=False,
            meta=False,
            ctrl=False,
            chain=1,
        )
        row.on_click(click)
        await pilot.pause(0.2)

        assert not isinstance(pilot.app.screen, ViewerScreen)
