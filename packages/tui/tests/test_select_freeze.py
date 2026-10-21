"""Шаг 5: Freeze UI на время выделения (SPEC 2026-07-25 §3.2 F1–F6).

Пока Screen._selecting=True: _poll_active пропускает stick/scroll/stream/anim;
timeout ~5с → clear_selection(); runner не стопается.
"""

from __future__ import annotations

import pytest
from gc_testkit.tui_support import tui_settings

pytest.importorskip("textual")


@pytest.mark.asyncio
async def test_trace_screen_has_freeze_field(tmp_path):
    """F1: TraceScreen имеет _select_freeze_since (None по умолчанию)."""
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        assert hasattr(pilot.app.screen, "_select_freeze_since")
        assert pilot.app.screen._select_freeze_since is None


@pytest.mark.asyncio
async def test_ui_frozen_for_select_predicate(tmp_path):
    """F1: _ui_frozen_for_select() отражает Screen._selecting."""
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._selecting = False
        assert scr._ui_frozen_for_select() is False
        scr._selecting = True
        assert scr._ui_frozen_for_select() is True


@pytest.mark.asyncio
async def test_poll_freeze_skips_stick(tmp_path, monkeypatch):
    """F2: при _selecting — _update_stick_state и _scroll_trace_end не вызываются."""
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        calls = {"stick": 0, "scroll": 0}

        def fake_stick():
            calls["stick"] += 1

        def fake_scroll():
            calls["scroll"] += 1

        monkeypatch.setattr(scr, "_update_stick_state", fake_stick)
        monkeypatch.setattr(scr, "_scroll_trace_end", fake_scroll)
        # Freeze ON
        scr._selecting = True
        scr._poll_active()
        assert calls["stick"] == 0
        assert calls["scroll"] == 0
        # Freeze OFF → возобновляется
        scr._selecting = False
        scr._poll_active()
        assert calls["stick"] >= 1


@pytest.mark.asyncio
async def test_poll_freeze_sets_since(tmp_path):
    """F1: при первом тике freeze ставит _select_freeze_since; при снятии — None."""
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._selecting = True
        scr._poll_active()
        assert scr._select_freeze_since is not None
        scr._selecting = False
        scr._poll_active()
        assert scr._select_freeze_since is None


@pytest.mark.asyncio
async def test_poll_freeze_timeout_clears(tmp_path, monkeypatch):
    """F5: freeze > 5с → clear_selection() вызывается, since сбрасывается."""
    import time as _time

    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        cleared = {"count": 0}

        def fake_clear():
            cleared["count"] += 1
            scr._selecting = False  # Textual так и делает при реальном MouseUp

        monkeypatch.setattr(scr, "clear_selection", fake_clear)
        scr._selecting = True
        scr._poll_active()  # ставит since
        # имитируем что прошло 6 секунд
        scr._select_freeze_since = _time.monotonic() - 6.0
        scr._poll_active()
        assert cleared["count"] == 1
        assert scr._select_freeze_since is None


@pytest.mark.asyncio
async def test_thinking_indicator_tick_gated_by_selecting(
    tmp_path, monkeypatch
):
    """F4: ThinkingIndicator._tick early-return при screen._selecting."""
    from gc_tui.app import TuiApp
    from gc_tui.widgets.thinking_indicator import ThinkingIndicator

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        ind = ThinkingIndicator()
        await pilot.app.mount(ind)
        refreshed = {"n": 0}
        monkeypatch.setattr(
            ind,
            "refresh",
            lambda: refreshed.__setitem__("n", refreshed["n"] + 1),
        )
        # freeze ON → _tick не должен refresh
        pilot.app.screen._selecting = True
        ind._tick()
        assert refreshed["n"] == 0
        # freeze OFF → _tick refresh
        pilot.app.screen._selecting = False
        ind._tick()
        assert refreshed["n"] >= 1


@pytest.mark.asyncio
async def test_tool_call_row_pulse_gated_by_selecting(tmp_path, monkeypatch):
    """F4: ToolCallRow._pulse_tick early-return при screen._selecting."""
    from gc_tui.app import TuiApp
    from gc_tui.widgets.tool_call_row import ToolCallRow

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        row = ToolCallRow(call_id="c1", tool_name="tool", args_summary="x=1")
        await pilot.app.mount(row)
        updated = {"n": 0}
        if row._prefix_static is not None:
            monkeypatch.setattr(
                row._prefix_static,
                "update",
                lambda *a, **k: updated.__setitem__("n", updated["n"] + 1),
            )
        # freeze ON → _pulse_tick не апдейтит prefix
        pilot.app.screen._selecting = True
        row._pulse_tick()
        assert updated["n"] == 0
        # freeze OFF → _pulse_tick апдейтит
        pilot.app.screen._selecting = False
        row._pulse_tick()
        assert updated["n"] >= 1


@pytest.mark.asyncio
async def test_refresh_stream_content_gated_by_selecting(
    tmp_path, monkeypatch
):
    """F3: _refresh_stream_content early-return при _selecting — закрывает
    событийный путь _stream_signal → call_from_thread (set_content/scroll_end
    не зовутся). Это главная дыра: poll-гейт бесполезен, если стрим красит
    экран в обход poll."""
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        # Подложим фейковый sink — иначе метод и так вернётся по `sink is None`.
        monkeypatch.setattr(
            app,
            "_stream_sink",
            type(
                "S",
                (),
                {
                    "snapshot": lambda self: ("think", "answer"),
                    "has_content": lambda self: True,
                },
            )(),
            raising=False,
        )
        # Заглушим UI-мутации, которые метод мог бы позвать.
        mutated = {"set_content": 0, "stick": 0, "scroll": 0}
        # _stream_answer_row должен быть не None, чтобы дошло до set_content.
        monkeypatch.setattr(
            scr,
            "_stream_answer_row",
            type(
                "R",
                (),
                {
                    "set_content": staticmethod(
                        lambda *a, **k: mutated.__setitem__(
                            "set_content", mutated["set_content"] + 1
                        )
                    )
                },
            )(),
            raising=False,
        )
        monkeypatch.setattr(
            scr,
            "_update_stick_state",
            lambda: mutated.__setitem__("stick", mutated["stick"] + 1),
        )
        monkeypatch.setattr(
            scr,
            "_scroll_trace_end",
            lambda: mutated.__setitem__("scroll", mutated["scroll"] + 1),
        )
        # Freeze ON → никаких мутаций
        scr._selecting = True
        scr._refresh_stream_content()
        assert mutated["set_content"] == 0
        assert mutated["stick"] == 0
        assert mutated["scroll"] == 0
        # Freeze OFF → set_content/stick/scroll зовутся
        scr._selecting = False
        scr._refresh_stream_content()
        assert mutated["set_content"] >= 1
        assert mutated["stick"] >= 1


@pytest.mark.asyncio
async def test_poll_freeze_does_not_cut_footer_and_tail(tmp_path, monkeypatch):
    """F6: freeze глушит только stick/scroll/stream/wipe, но НЕ весь poll.
    Footer/title/orphan/MCP-indicator продолжают обновляться — UI не тупеет
    целиком на 5с timeout. Это регрессия на прежний полный `return`."""
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        calls = {"footer": 0, "title": 0, "orphan": 0, "mcp": 0}
        monkeypatch.setattr(
            scr,
            "_refresh_footer",
            lambda: calls.__setitem__("footer", calls["footer"] + 1),
        )
        monkeypatch.setattr(
            scr,
            "_update_terminal_title",
            lambda: calls.__setitem__("title", calls["title"] + 1),
        )
        monkeypatch.setattr(
            scr,
            "_run_orphan_cleanups",
            lambda: calls.__setitem__("orphan", calls["orphan"] + 1),
        )
        monkeypatch.setattr(
            scr,
            "_update_mcp_indicator",
            lambda: calls.__setitem__("mcp", calls["mcp"] + 1),
        )
        # Без активной сессии poll доходит до ветки `_active_task_id is None`,
        # которая зовёт footer+title и return — это и доказывает, что хвост
        # poll не отрезан ранним return при freeze.
        scr._active_task_id = None
        # Freeze ON → poll всё равно доходит до footer/title (раньше return их резал)
        scr._selecting = True
        scr._poll_active()
        assert calls["footer"] >= 1
        assert calls["title"] >= 1
        # orphan/MCP тоже должны выполняться (до ранней ветки None)
        assert calls["orphan"] >= 1
        assert calls["mcp"] >= 1


@pytest.mark.asyncio
async def test_tail_under_selection_drawn_after_release(tmp_path, monkeypatch):
    """F3: записи, пришедшие во время выделения, рисуются первым тиком после
    отпускания мыши — под заморозкой хвост не читается, курсор стоит."""
    import json

    from gc_testkit.develop_files import append_new_thread_messages
    from gc_tui.app import TuiApp

    def _append(seq: int, *msgs: dict) -> None:
        append_new_thread_messages(
            str(tmp_path), "cli", "t1", list(msgs), set(), start_seq=seq
        )

    _append(0, {"id": "u1", "role": "user", "content": "первая"})
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task("t1")
        await pilot.pause(0.05)
        frozen = [True]
        monkeypatch.setattr(scr, "_ui_frozen_for_select", lambda: frozen[0])
        _append(
            1,
            {"id": "u2", "role": "user", "content": "под выделением"},
            {
                "id": "fc1",
                "type": "function_call",
                "name": "fs_read_file",
                "call_id": "c1",
                "arguments": json.dumps({"path": "a.py"}),
            },
            {
                "id": "fo1",
                "type": "function_call_output",
                "call_id": "c1",
                "output": "ok",
            },
        )
        tail_events = scr._store.tail_events
        reads = []

        def _counted(*args, **kwargs):
            reads.append(frozen[0])
            return tail_events(*args, **kwargs)

        monkeypatch.setattr(scr._store, "tail_events", _counted)
        scr._poll_active()
        scr._poll_active()
        frozen[0] = False
        scr._poll_active()
        await pilot.pause(0.05)
        ids = [e.message_id for e in scr._task_events]

    assert True not in reads
    assert {"u2", "fc1", "fo1"} <= set(ids)
    assert ids.count("u2") == 1
    assert scr._cursor.ordering_key == 4


@pytest.mark.asyncio
async def test_stream_answer_under_selection_drawn_once(tmp_path, monkeypatch):
    """Ответ из стрима дописан в файл под выделением: после отпускания —
    одна строка ответа, стрим-блок снят."""
    from gc_testkit.develop_files import append_new_thread_messages
    from gc_tui.app import TuiApp
    from gc_tui.widgets.stream_block import StreamBlock

    def _append(seq: int, msg: dict) -> None:
        append_new_thread_messages(
            str(tmp_path), "cli", "t1", [msg], set(), start_seq=seq
        )

    _append(0, {"id": "u1", "role": "user", "content": "вопрос"})
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task("t1")
        await pilot.pause(0.05)
        block = StreamBlock()
        scr._stream_block = block
        scr._trace_mount(block)
        frozen = [True]
        monkeypatch.setattr(scr, "_ui_frozen_for_select", lambda: frozen[0])
        _append(1, {"id": "a1", "role": "assistant", "content": "ответ"})
        scr._poll_active()
        assert scr._stream_block is block
        frozen[0] = False
        scr._poll_active()
        await pilot.pause(0.05)
        answers = [e for e in scr._task_events if e.message_id == "a1"]
        mounted = block in scr.query_one("#trace").children

    assert len(answers) == 1
    assert scr._stream_block is None and not mounted
