import json
import threading
from pathlib import Path

import pytest
from gc_testkit import as_coro
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


@pytest.mark.asyncio
async def test_stream_signal_delegates_to_active_screen(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    tid = "sig1"
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
        calls = []
        monkeypatch.setattr(
            scr, "_refresh_stream_content", lambda: calls.append(1)
        )
        pilot.app._stream_signal()
        assert calls == [1]


@pytest.mark.asyncio
async def test_post_stream_signal_returns_without_waiting_ui(
    tmp_path, monkeypatch
):
    """Поток агента ставит сигнал в очередь UI и сразу идёт дальше:
    перерисовка выполняется циклом UI позже, не под ожиданием потока."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    tid = "sig2"
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
        calls = []
        monkeypatch.setattr(
            scr, "_refresh_stream_content", lambda: calls.append(1)
        )
        thread = threading.Thread(
            target=pilot.app._post_stream_signal, daemon=True
        )
        thread.start()
        thread.join(timeout=1)
        assert not thread.is_alive()
        await pilot.pause()
        assert calls == [1]


def test_stream_pump_wired_to_post_signal(tmp_path, monkeypatch):
    from gc_core.runtime_notices import unregister_runtime_notices_listener
    from gc_tui.app import TuiApp

    class _Ctx:
        # Двойник RunContext — как в test_trace_note_sink.py.
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

    monkeypatch.setattr(
        "gc_core.config_modules.auth.build_auth_connector", lambda cfg: None
    )
    monkeypatch.setattr(
        "gc_core.run_context.acreate_run_context",
        as_coro(lambda *a, **k: _Ctx()),
    )
    monkeypatch.setattr("gc_core.agent_executor.AgentExecutor", _Ex)
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    app._build_real_executor()
    unregister_runtime_notices_listener(app._on_runtime_notice)
    assert app._stream_pump._signal == app._post_stream_signal
