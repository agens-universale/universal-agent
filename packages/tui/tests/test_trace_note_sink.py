"""Пометка «изображение не просмотрено»: очередь пометок, проводка on_trace_note,
серая строка [изображение] на тике опроса (дизайн картинок §10, визуальный контракт §4)."""

import json
import threading
from types import SimpleNamespace

import pytest

from gc_testkit import as_coro
from gc_tui.services.trace_note_sink import TraceNoteSink


def test_drain_returns_the_thread_notes_in_order_and_drops_the_rest():
    """Пометка только живая и только своей ленты: пометки чужого треда (ход, начатый до
    смены сессии) при снятии выбрасываются — ни сейчас, ни после возврата их нет."""
    sink = TraceNoteSink()
    sink.push("A", "первая")
    sink.push("B", "чужая")
    sink.push("A", "вторая")
    assert sink.drain("A") == ["первая", "вторая"]
    assert sink.drain("B") == []
    assert sink.drain("A") == []


def test_push_from_many_threads_loses_nothing():
    sink = TraceNoteSink()
    workers = [
        threading.Thread(
            target=lambda i=i: [sink.push("A", f"{i}-{k}") for k in range(100)]
        )
        for i in range(8)
    ]
    for w in workers:
        w.start()
    for w in workers:
        w.join()
    assert sorted(sink.drain("A")) == sorted(
        f"{i}-{k}" for i in range(8) for k in range(100)
    )


def test_the_hook_tags_a_note_with_the_thread_of_its_run():
    """Хук контекста помечает пометку тредом прогона на момент вызова: контекст один на
    приложение, треды у ходов разные."""
    sink = TraceNoteSink()
    ctx = SimpleNamespace(system_id="cli", thread_id="A")
    hook = sink.hook_for(ctx)
    hook("в A")
    ctx.thread_id = "B"
    hook("в B")
    assert sink.drain("B") == ["в B"]


def test_executor_build_wires_on_trace_note_to_the_app_sink(
    tmp_path, monkeypatch
):
    from gc_tui.app import TuiApp

    class _Ctx:
        # Двойник RunContext: TuiApp после сборки включает diff-журнал и снимает
        # снапшот MCP — им нужны эти поля (как в test_mcp_boot_snapshot.py).
        os_backend = None
        tools_data: dict = {}
        mcp_status: dict = {}
        logging_path = str(tmp_path / "run.log")
        system_id = "cli"
        thread_id = "t1"

    class _Ex:
        def __init__(self, ctx):
            pass

        def stop_run(self):
            pass

    ctx = _Ctx()
    monkeypatch.setattr(
        "gc_core.config.build_auth_connector", lambda cfg: None
    )
    monkeypatch.setattr(
        "gc_core.run_context.acreate_run_context", as_coro(lambda *a, **k: ctx)
    )
    monkeypatch.setattr("gc_core.agent_executor.AgentExecutor", _Ex)
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    app._build_real_executor()
    ctx.on_trace_note("не просмотрено")
    assert app._trace_note_sink.drain("t1") == ["не просмотрено"]


@pytest.mark.asyncio
async def test_poll_tick_mounts_the_gray_line_below_the_reply_and_only_live(
    tmp_path,
):
    pytest.importorskip("textual")
    from gc_core.vision.notes import NOTE_MAIN_BLIND
    from gc_tui.app import TuiApp
    from gc_tui.widgets.trace_row import TraceRow
    from gc_tui.widgets.user_reply import UserReplyContainer

    threads = tmp_path / "threads"
    threads.mkdir()
    tid = "a" * 32
    (threads / f"data_cli_{tid}.jsonl").write_text(
        json.dumps(
            {
                "type": "message",
                "seq": 1,
                "ts": "2026-09-24T10:00:00+00:00",
                "payload": {"id": "u1", "role": "user", "content": "глянь"},
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    (threads / f"meta_cli_{tid}.json").write_text(
        json.dumps({"thread_id": tid, "tui": {}}), encoding="utf-8"
    )
    note = NOTE_MAIN_BLIND.format(alias="glm")
    async with TuiApp(
        threads_dir=str(threads), system="cli", config={}
    ).run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        await pilot.pause()
        pilot.app._trace_note_sink.push("b" * 32, "из другой сессии")
        pilot.app._trace_note_sink.push(tid, note)
        await pilot.pause(0.7)  # тик опроса — 250 мс
        rows = [
            w for w in scr.query(TraceRow) if w._prefix == "[изображение] "
        ]
        assert [(r._content, r.has_class("line-meta")) for r in rows] == [
            (note, True)
        ]
        trace = list(scr.query_one("#trace").children)
        assert trace.index(rows[0]) > trace.index(
            scr.query_one(UserReplyContainer)
        )
        assert pilot.app._trace_note_sink.drain(tid) == []
        # Открыли сессию заново — пометки нет: она только живая.
        scr.attach_active_task(tid, is_resume=True)
        await pilot.pause()
        assert [
            w for w in scr.query(TraceRow) if w._prefix == "[изображение] "
        ] == []
