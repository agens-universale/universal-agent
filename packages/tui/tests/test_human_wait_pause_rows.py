"""Тик строк тулов: пауза на человеко-ожидании и elapsed — и у вложенных."""

import pytest
from gc_testkit.tui_support import tui_settings
from gc_tui.widgets.tool_call_row import ToolCallRow


def _app(tmp_path):
    from gc_tui.app import TuiApp

    return TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )


@pytest.mark.asyncio
async def test_human_wait_pauses_nested_rows(tmp_path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        root = ToolCallRow(
            call_id="r", tool_name="subagent_x", args_summary=""
        )
        nested = ToolCallRow(
            call_id="n", tool_name="exec_run", args_summary="", level=1
        )
        await scr.mount(root)
        await scr.mount(nested)
        # Дальше без await: живой тик опроса без раннера чистит строки.
        scr._active_calls["r"] = root
        scr._nested_tool_rows["n"] = nested
        scr._apply_human_wait(True)
        assert nested._pulse_timer is None
        assert "‖" in nested._prefix_static.content.plain
        assert root._pulse_timer is None

        scr._apply_human_wait(False)
        assert nested._pulse_timer is not None
        assert "▸" in nested._prefix_static.content.plain
        assert root._pulse_timer is not None


@pytest.mark.asyncio
async def test_human_wait_ticks_nested_elapsed(tmp_path, monkeypatch):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        nested = ToolCallRow(
            call_id="n", tool_name="exec_run", args_summary="", level=1
        )
        await scr.mount(nested)
        ticks = []
        monkeypatch.setattr(
            nested, "refresh_running_elapsed", lambda: ticks.append(1)
        )
        scr._nested_tool_rows["n"] = nested
        scr._apply_human_wait(False)
        assert ticks == [1]


@pytest.mark.asyncio
async def test_terminal_state_unhides_indicator(tmp_path):
    import json

    from gc_tui.widgets.thinking_indicator import ThinkingIndicator

    tid = "humanwait00000000000000000000001"
    meta = {
        "thread_id": tid,
        "system_id": "cli",
        "tui": {
            "status": "completed",
            "last_indicator": {"kind": "done", "elapsed": 3},
        },
    }
    (tmp_path / f"meta_cli_{tid}.json").write_text(
        json.dumps(meta), encoding="utf-8"
    )
    (tmp_path / f"data_cli_{tid}.jsonl").write_text("", encoding="utf-8")
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        scr._show_thinking_indicator()
        await pilot.pause()
        # Стоп во время подтверждения: индикатор скрыт, ожидание снято.
        scr._hide_thinking_indicator()
        scr._poll_active()
        ind = pilot.app.screen.query_one(ThinkingIndicator)
        assert ind._state == "done"
        assert not ind.has_class("hidden")
