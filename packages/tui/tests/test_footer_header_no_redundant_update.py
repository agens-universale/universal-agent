"""Заголовок и футер не перекладывают экран, пока содержимое не менялось.

Опрос зовёт `_refresh_header` / `_refresh_footer` каждые 250 мс; повторная
запись равного содержимого заставляла Textual пересчитывать раскладку. Тесты
фиксируют: без изменений объекты содержимого те же, а любое реальное
изменение (модель, метр контекста, бейдж, подсказка, прогон, голос) доходит.
"""

import json
from types import SimpleNamespace

import pytest
from gc_core.runtime_notices import clear_runtime_notices, push_runtime_notice
from gc_core.startup_notices import Notice
from gc_testkit.tui_support import tui_settings
from gc_tui.services.notices_ui import WARN_GLYPH
from rich.text import Text

_FIELDS = ("#header-left", "#header-right", "#footer-left", "#footer-right")


def _plain(scr, sel):
    content = scr.query_one(sel).content
    return content.plain if hasattr(content, "plain") else str(content)


def _refresh(scr):
    scr._refresh_header()
    scr._refresh_footer()


def _write_meta(tmp_path, tid, chars):
    meta = {
        "thread_id": tid,
        "system_id": "cli",
        "runtime": {"live_history_chars": chars, "live_msg_count": 1},
    }
    (tmp_path / f"meta_cli_{tid}.json").write_text(
        json.dumps(meta), encoding="utf-8"
    )
    (tmp_path / f"data_cli_{tid}.jsonl").write_text("", encoding="utf-8")


@pytest.mark.asyncio
async def test_repeat_refresh_keeps_same_objects(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path / "t"),
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        _refresh(scr)
        before = [scr.query_one(sel).content for sel in _FIELDS]
        _refresh(scr)
        after = [scr.query_one(sel).content for sel in _FIELDS]
        for old, new in zip(before, after):
            assert new is old


@pytest.mark.asyncio
async def test_model_change_reaches_footer(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    app = TuiApp(threads_dir=str(tmp_path / "t"))
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        app._settings = tui_settings({"agent.model": "m1"})
        _refresh(scr)
        assert "m1" in _plain(scr, "#footer-right")
        app._settings = tui_settings({"agent.model": "m2"})
        _refresh(scr)
        assert "m2" in _plain(scr, "#footer-right")


@pytest.mark.asyncio
async def test_context_meter_change_reaches_footer(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    tid = "ctxm0000000000000000000000000009a"
    _write_meta(tmp_path, tid, 500)
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings(
            {
                "agent.model": "M",
                "backend.api_mode": "chat_completions",
                "summary": {
                    "chat_completions_summary_trigger_chars": 1000,
                    "chat_completions_summary_turn_end_trigger_chars": 1,
                },
            }
        ),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        _refresh(scr)
        first = _plain(scr, "#footer-right")
        assert "50%" in first
        _write_meta(tmp_path, tid, 800)
        _refresh(scr)
        assert _plain(scr, "#footer-right") != first


@pytest.mark.asyncio
async def test_notice_badge_reaches_footer(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    clear_runtime_notices()
    app = TuiApp(
        threads_dir=str(tmp_path / "t"),
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        _refresh(scr)
        assert WARN_GLYPH not in _plain(scr, "#footer-left")
        push_runtime_notice(Notice("warning", "w1", "тест"))
        _refresh(scr)
        assert f"{WARN_GLYPH} 1" in _plain(scr, "#footer-left")
    clear_runtime_notices()


@pytest.mark.asyncio
async def test_hint_expiry_restores_commands(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path / "t"),
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        _refresh(scr)
        before = _plain(scr, "#footer-left")
        scr._show_footer_hint("ПОДСКАЗКА", seconds=60)
        assert _plain(scr, "#footer-left") == "ПОДСКАЗКА"
        scr._clear_footer_hint()
        assert _plain(scr, "#footer-left") == before


@pytest.mark.asyncio
async def test_running_toggles_stop_hint(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    flag = [True]
    app = TuiApp(
        threads_dir=str(tmp_path / "t"),
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        app.tui_state.active_task_id = "t1"
        scr._active_task_id = "t1"
        app.active_runner = SimpleNamespace(is_running=lambda: flag[0])
        _refresh(scr)
        assert "Ctrl+C/stop" in _plain(scr, "#footer-left")
        flag[0] = False
        _refresh(scr)
        assert "Ctrl+C/stop" not in _plain(scr, "#footer-left")


@pytest.mark.asyncio
async def test_refusal_then_run_shows_stop(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path / "t"),
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._show_footer_refusal("✗ не сохранено: причина")
        assert "не сохранено" in _plain(scr, "#footer-left")
        app.tui_state.active_task_id = "t2"
        app.active_runner = SimpleNamespace(is_running=lambda: True)
        _refresh(scr)
        left = _plain(scr, "#footer-left")
        assert "не сохранено" not in left
        assert "Ctrl+C/stop" in left


@pytest.mark.asyncio
async def test_voice_stage_then_idle_restores_commands(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path / "t"),
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        monkeypatch.setattr(scr, "_voice_footer_render", lambda: Text("REC"))
        scr._voice = SimpleNamespace(state="recording")
        _refresh(scr)
        assert "REC" in _plain(scr, "#footer-left")
        scr._voice = SimpleNamespace(state="idle")
        _refresh(scr)
        assert "REC" not in _plain(scr, "#footer-left")
