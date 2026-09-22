"""Хоткеи, /back, /main, стек экранов."""

import pytest


@pytest.mark.asyncio
async def test_ctrl_s_opens_sessions(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    app = TuiApp(threads_dir=str(tmp_path))
    async with app.run_test() as pilot:
        await pilot.press("ctrl+s")
        await pilot.pause()
        from gc_tui.screens.sessions import SessionsScreen

        assert isinstance(pilot.app.screen, SessionsScreen)


@pytest.mark.asyncio
async def test_esc_pops_screen(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    app = TuiApp(threads_dir=str(tmp_path))
    async with app.run_test() as pilot:
        await pilot.press("ctrl+s")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        from gc_tui.screens.main import TraceScreen

        assert isinstance(pilot.app.screen, TraceScreen)


@pytest.mark.asyncio
async def test_f1_opens_help():
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    async with TuiApp().run_test() as pilot:
        await pilot.press("f1")
        await pilot.pause()
        from gc_tui.screens.help import HelpScreen

        assert isinstance(pilot.app.screen, HelpScreen)


@pytest.mark.asyncio
async def test_f2_opens_sessions(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    async with TuiApp(threads_dir=str(tmp_path)).run_test() as pilot:
        await pilot.press("f2")
        await pilot.pause()
        from gc_tui.screens.sessions import SessionsScreen

        assert isinstance(pilot.app.screen, SessionsScreen)


@pytest.mark.asyncio
async def test_ctrl_n_opens_new(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    async with TuiApp(threads_dir=str(tmp_path)).run_test() as pilot:
        await pilot.press("ctrl+n")
        await pilot.pause()
        from gc_tui.screens.main import TraceScreen

        assert isinstance(pilot.app.screen, TraceScreen)


@pytest.mark.asyncio
async def test_f5_opens_config():
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    async with TuiApp().run_test() as pilot:
        await pilot.press("f5")
        await pilot.pause()
        from gc_tui.screens.config import ConfigScreen

        assert isinstance(pilot.app.screen, ConfigScreen)


@pytest.mark.asyncio
async def test_ctrl_g_opens_config():
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    async with TuiApp().run_test() as pilot:
        await pilot.press("ctrl+g")
        await pilot.pause()
        from gc_tui.screens.config import ConfigScreen

        assert isinstance(pilot.app.screen, ConfigScreen)


@pytest.mark.asyncio
async def test_ctrl_d_opens_diff(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    async with TuiApp(config={"launch_root": str(tmp_path)}).run_test() as pilot:
        await pilot.press("ctrl+d")
        await pilot.pause()
        from gc_tui.screens.diff import DiffScreen

        assert isinstance(pilot.app.screen, DiffScreen)


@pytest.mark.asyncio
async def test_ctrl_o_opens_notices(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    async with TuiApp(threads_dir=str(tmp_path)).run_test() as pilot:
        await pilot.press("ctrl+o")
        await pilot.pause()
        from gc_tui.screens.notices import NoticesScreen

        assert isinstance(pilot.app.screen, NoticesScreen)


@pytest.mark.asyncio
async def test_ctrl_slash_opens_help():
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    async with TuiApp().run_test() as pilot:
        await pilot.press("ctrl+slash")
        await pilot.pause()
        from gc_tui.screens.help import HelpScreen

        assert isinstance(pilot.app.screen, HelpScreen)


@pytest.mark.asyncio
async def test_f3_opens_diff(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    async with TuiApp(config={"launch_root": str(tmp_path)}).run_test() as pilot:
        await pilot.press("f3")
        await pilot.pause()
        from gc_tui.screens.diff import DiffScreen

        assert isinstance(pilot.app.screen, DiffScreen)


@pytest.mark.asyncio
async def test_tab_visits_each_event_once(tmp_path):
    """Tab прыгает от события к событию, по одной Tab-остановке на каждое
    TraceEvent (no > 0). Continuation prose агентского ответа, `* note:`,
    code-block-панели после первой — НЕ Tab-стопы."""
    import json
    from pathlib import Path

    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.tool_call_row import ToolCallRow
    from gc_tui.widgets.trace_row import TraceRow
    from gc_tui.widgets.user_reply import UserReplyContainer

    tid = "tab1"
    # Сценарий: user-reply (ev1) → agent prose из 2 prose-блоков (ev2)
    # → tool-вызов (ev3) → framework [skill] (ev4).
    events = [
        {"type": "message", "seq": 1, "ts_utc": "...", "payload": {"id": "u1", "role": "user", "content": "поехали"}},
    ]
    (Path(tmp_path) / f"data_cli_{tid}.jsonl").write_text(
        "\n".join(json.dumps(ev) for ev in events) + "\n", encoding="utf-8"
    )
    (Path(tmp_path) / f"meta_cli_{tid}.json").write_text(
        json.dumps({"thread_id": tid, "updated_at": "...", "messages_count": 1}), encoding="utf-8"
    )

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        await pilot.pause(0.2)

        # Импитируем рендер событий, как делает _render_records.
        from gc_tui.models import TraceEvent

        scr._render_event(
            TraceEvent(
                no=2,
                event_ts_utc_iso="...",
                kind="agent",
                title=None,
                summary="первая prose-строка\n\nвторая prose-строка",
                tool_name=None,
                call_id=None,
                full_payload=None,
            )
        )
        scr._render_event(
            TraceEvent(
                no=3,
                event_ts_utc_iso="...",
                kind="call",
                title=None,
                summary="read_file(path='x')",
                tool_name="read_file",
                call_id="c1",
                full_payload=None,
            )
        )
        scr._mount_trace_row(prefix="[skill] ", content="x применится", kind_class="line-meta", event_no=4)
        await pilot.pause(0.1)

        # Cобираем focusable-виджеты трейса по порядку.
        focusables = [w for w in scr.query("UserReplyContainer, TraceRow, ToolCallRow") if w.can_focus]
        # Ожидаем: user reply (ev1) + первая prose агента (ev2)
        # + ToolCallRow (ev3) + framework TraceRow [skill] (ev4) = 4 стопа.
        assert len(focusables) == 4, (
            f"Ожидалось 4 Tab-стопа, получено {len(focusables)}: {[type(w).__name__ for w in focusables]}"
        )
        # Первый — UserReplyContainer.
        assert isinstance(focusables[0], UserReplyContainer)
        # Второй — TraceRow с line-agent (не line-agent-cont).
        assert isinstance(focusables[1], TraceRow)
        assert focusables[1].has_class("line-agent")
        assert not focusables[1].has_class("line-agent-cont")
        # Третий — ToolCallRow.
        assert isinstance(focusables[2], ToolCallRow)
        # Четвёртый — framework TraceRow.
        assert isinstance(focusables[3], TraceRow)
        assert focusables[3].has_class("line-meta")


@pytest.mark.asyncio
async def test_tab_skips_agent_continuation_rows(tmp_path):
    """Агентский ответ из 3 prose-блоков: фокусируется ТОЛЬКО первая prose-строка.
    Continuation `line-agent-cont` — НЕ Tab-стоп."""
    import json
    from pathlib import Path

    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.models import TraceEvent
    from gc_tui.widgets.trace_row import TraceRow

    tid = "tab2"
    (Path(tmp_path) / f"data_cli_{tid}.jsonl").write_text(
        json.dumps(
            {"type": "message", "seq": 1, "ts_utc": "...", "payload": {"id": "u1", "role": "user", "content": "ok"}}
        )
        + "\n",
        encoding="utf-8",
    )
    (Path(tmp_path) / f"meta_cli_{tid}.json").write_text(
        json.dumps({"thread_id": tid, "updated_at": "...", "messages_count": 1}), encoding="utf-8"
    )

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        await pilot.pause(0.2)

        # Проза режется на отдельные prose-строки только границей code/table
        # (пустые строки сохраняются внутри блока, 0ff36ad8) — разделяем code-блоками.
        scr._render_event(
            TraceEvent(
                no=2,
                event_ts_utc_iso="...",
                kind="agent",
                title=None,
                summary=("первый блок\n```text\na\n```\nвторой блок\n```text\nb\n```\nтретий блок"),
                tool_name=None,
                call_id=None,
                full_payload=None,
            )
        )
        await pilot.pause(0.1)

        agent_rows = [r for r in scr.query(TraceRow) if r.has_class("line-agent")]
        # Все три prose-блока смонтированы как TraceRow.
        assert len(agent_rows) == 3
        # Только первый focusable.
        assert agent_rows[0].can_focus is True
        assert not agent_rows[0].has_class("line-agent-cont")
        assert agent_rows[1].can_focus is False
        assert agent_rows[1].has_class("line-agent-cont")
        assert agent_rows[2].can_focus is False
        assert agent_rows[2].has_class("line-agent-cont")
