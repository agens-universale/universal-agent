"""Tool-пара рендерится как один widget, который мутирует от running к ok."""
import json

import pytest


@pytest.mark.asyncio
async def test_call_then_output_mutates_in_place(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.tool_call_row import ToolCallRow

    tid = "t1"
    data = tmp_path / f"data_cli_{tid}.jsonl"
    meta = tmp_path / f"meta_cli_{tid}.json"
    # status=running — «внешний writer жив» (server/swarm-семантика): незакрытый
    # вызов легитимно остаётся running, инвариант поллинга его не финализирует.
    meta.write_text(
        json.dumps({"thread_id": tid, "updated_at": "2026-05-21T12:00:00+00:00",
                    "messages_count": 1, "status": "running"}),
        encoding="utf-8",
    )
    # 1) user msg + function_call (без output)
    data.write_text(
        json.dumps({"type": "message", "seq": 1, "ts_utc": "2026-05-21T12:00:00+00:00",
                    "payload": {"id": "u1", "role": "user", "content": "go"}}) + "\n"
        + json.dumps({"type": "message", "seq": 2, "ts_utc": "2026-05-21T12:00:01+00:00",
                      "payload": {"id": "fc1", "type": "function_call", "call_id": "c1",
                                  "name": "read_file",
                                  "arguments": json.dumps({"path": "src/x.py"})}}) + "\n",
        encoding="utf-8",
    )

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        await pilot.pause()

        rows = pilot.app.query(ToolCallRow)
        assert len(rows) == 1
        # running: глиф ▸ в prefix, content несёт имя + elapsed (без "[..]")
        assert "read_file" in str(rows[0].query_one(".trace-content").render())
        assert "▸" in str(rows[0].query_one(".trace-prefix").render())

        # 2) добавляем output, ждём polling (>= 1s)
        with open(data, "a", encoding="utf-8") as f:
            f.write(json.dumps({"type": "message", "seq": 3,
                                "ts_utc": "2026-05-21T12:00:02+00:00",
                                "payload": {"id": "fco1", "type": "function_call_output",
                                            "call_id": "c1", "name": "read_file",
                                            "output": json.dumps({"tool": "read_file", "ok": True,
                                                                  "total_lines": 312})}}) + "\n")
        meta.write_text(json.dumps({"thread_id": tid,
                                    "updated_at": "2026-05-21T12:00:03+00:00",
                                    "messages_count": 3, "status": "running"}),
                        encoding="utf-8")
        await pilot.pause(0.4)  # > 250 ms polling interval

        rows = pilot.app.query(ToolCallRow)
        assert len(rows) == 1  # тот же widget, не появился новый
        rendered = str(rows[0].query_one(".trace-content").render())
        prefix = str(rows[0].query_one(".trace-prefix").render())
        assert "✓" in prefix    # ok-глиф в prefix
        assert "▸" not in prefix


@pytest.mark.asyncio
async def test_active_calls_cleared_on_task_switch(tmp_path):
    """Switching tasks via attach_active_task must clear _active_calls dict."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    # Setup task A with one pending function_call (no output)
    (tmp_path / "meta_cli_A.json").write_text(
        json.dumps({"thread_id": "A", "updated_at": "2026-05-21T12:00:00+00:00",
                    "messages_count": 1}),
        encoding="utf-8",
    )
    (tmp_path / "data_cli_A.jsonl").write_text(
        json.dumps({"type": "message", "seq": 1, "ts_utc": "2026-05-21T12:00:00+00:00",
                    "payload": {"id": "fc1", "type": "function_call", "call_id": "callA",
                                "name": "read_file",
                                "arguments": json.dumps({"path": "a.py"})}}) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "meta_cli_B.json").write_text(
        json.dumps({"thread_id": "B", "updated_at": "2026-05-21T12:00:00+00:00",
                    "messages_count": 0}),
        encoding="utf-8",
    )
    (tmp_path / "data_cli_B.jsonl").write_text("", encoding="utf-8")

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    app.active_runner = _StubLiveRunner()  # жив — инвариант поллинга не гасит callA
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task("A")
        await pilot.pause()
        assert "callA" in scr._active_calls  # task A's call registered

        # Switch to task B
        scr.attach_active_task("B")
        await pilot.pause()
        assert "callA" not in scr._active_calls  # cleared
        assert len(scr._active_calls) == 0
        assert len(scr._submit_solution_call_ids) == 0


class _StubLiveRunner:
    """Минимальный runner-stub, который представляется живым, чтобы
    Fix D recovery-блок в attach_active_task НЕ снял banner."""

    def is_running(self) -> bool:
        return True


@pytest.mark.asyncio
async def test_ask_human_placeholder_shows_question(tmp_path):
    """ask_human event mounts a banner (Phase 5) carrying the question."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.ask_human_banner import AskHumanBanner

    (tmp_path / "meta_cli_t.json").write_text(
        json.dumps({"thread_id": "t", "updated_at": "2026-05-21T12:00:00+00:00",
                    "messages_count": 1}),
        encoding="utf-8",
    )
    (tmp_path / "data_cli_t.jsonl").write_text(
        json.dumps({"type": "message", "seq": 1, "ts_utc": "2026-05-21T12:00:00+00:00",
                    "payload": {"id": "fc1", "type": "function_call", "call_id": "c1",
                                "name": "ask_human",
                                "arguments": json.dumps({"question": "Какого типа отчёт?"})}}) + "\n",
        encoding="utf-8",
    )

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    # Fix D: без живого runner-а recovery-блок снял бы banner. Здесь мы тестируем
    # именно рендер banner-а с вопросом → подсовываем live-stub.
    app.active_runner = _StubLiveRunner()
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task("t")
        await pilot.pause()
        banner = pilot.app.query(AskHumanBanner)
        assert len(banner) == 1
        from gc_tui.widgets.trace_row import TraceRow
        rows = list(banner[0].query(TraceRow))
        assert rows, "баннер должен смонтировать блок-виджеты"
        joined = " ".join(
            (r._content.plain if hasattr(r._content, "plain") else str(r._content))
            for r in rows
        )
        assert "Какого типа отчёт" in joined


# ---- инвариант: running-строки живут только пока задача жива (spec 2026-06-10) ----
# Критерий «жива» — тот же, что у статуса в шапке (_task_status_badge):
# локальный runner / ask_human / внешний writer с meta.status=running.


@pytest.mark.asyncio
async def test_resume_orphan_call_finalized_as_interrupted(tmp_path):
    """Resume треда с function_call без output и без живого run:
    строка финализируется «прервано», секунды не тикают."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.tool_call_row import ToolCallRow

    tid = "orphan"
    (tmp_path / f"meta_cli_{tid}.json").write_text(
        json.dumps({"thread_id": tid, "updated_at": "2026-05-21T12:00:00+00:00",
                    "messages_count": 1}),   # БЕЗ status=running — тред брошен
        encoding="utf-8",
    )
    (tmp_path / f"data_cli_{tid}.jsonl").write_text(
        json.dumps({"type": "message", "seq": 1, "ts_utc": "2026-05-21T12:00:00+00:00",
                    "payload": {"id": "fc1", "type": "function_call", "call_id": "c1",
                                "name": "subagent_critic",
                                "arguments": json.dumps({"task": "анализ"})}}) + "\n",
        encoding="utf-8",
    )

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        await pilot.pause()
        scr._poll_active()
        await pilot.pause()
        rows = pilot.app.query(ToolCallRow)
        assert len(rows) == 1
        rendered = str(rows[0].query_one(".trace-content").render())
        assert "прервано" in rendered
        assert "running" not in rendered
        assert scr._active_calls == {}


@pytest.mark.asyncio
async def test_local_stop_finalizes_running_rows(tmp_path):
    """Локальный стоп: runner перестал быть живым → на следующем тике поллинга
    открытые tool-строки финализируются «прервано»."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.tool_call_row import ToolCallRow
    from textual.containers import VerticalScroll

    class _ToggleRunner:
        def __init__(self): self.live = True
        def is_running(self): return self.live

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    runner = _ToggleRunner()
    app.active_runner = runner
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        row = ToolCallRow(call_id="c1", tool_name="subagent_code_reviewer",
                          args_summary="task=анализ")
        scr.query_one("#trace", VerticalScroll).mount(row)
        await pilot.pause()
        scr._active_calls["c1"] = row

        scr._poll_active()                       # run жив — строка тикает
        await pilot.pause()
        # running: глиф ▸ в prefix, content несёт elapsed «· Nс» (без слова «running»)
        assert "▸" in str(row._prefix_static.render())
        assert "running" not in str(row._content_static.render())

        runner.live = False                      # стоп
        scr._poll_active()
        await pilot.pause()
        rendered = str(row._content_static.render())
        assert "прервано" in rendered
        assert "running" not in rendered
        assert scr._active_calls == {}
