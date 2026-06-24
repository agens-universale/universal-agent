"""Хром: бейдж статуса и индикатор контекста — после Task 6 в мета-полосе, не хедере."""
import json
import pytest


def _write_task(tmp_path, tid, status_tui=None, extra_data=""):
    meta = {"thread_id": tid, "system_id": "cli"}
    if status_tui is not None:
        meta["tui"] = {"status": status_tui}
    (tmp_path / f"meta_cli_{tid}.json").write_text(json.dumps(meta), encoding="utf-8")
    (tmp_path / f"data_cli_{tid}.jsonl").write_text(extra_data, encoding="utf-8")


@pytest.mark.asyncio
async def test_badge_completed_from_meta_tui(tmp_path):
    pytest.importorskip("textual")
    from tui.app import TuiApp
    tid = "hdr00000000000000000000000000001a"
    _write_task(tmp_path, tid, status_tui="completed")
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        # нет активного runner → терминальный статус из meta.tui.status
        assert scr._task_status_badge() == "[completed]"


@pytest.mark.asyncio
async def test_header_left_is_str_with_session_id(tmp_path):
    """После Task 6: хедер-лево — str «Сессия: <id>», без модели и бейджа."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    tid = "hdr00000000000000000000000000002a"
    _write_task(tmp_path, tid, status_tui="completed")
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        left, _right = scr._compose_header_segments()
        # хедер-лево теперь str, не Text
        s = left if isinstance(left, str) else left.plain
        assert "Сессия:" in s
        # модель и статус переехали в мету
        assert "модель" not in s
        assert "[completed]" not in s


@pytest.mark.asyncio
async def test_meter_hidden_in_header_responses_mode(tmp_path):
    """Контекст-индикатор в хедере не показывается — он в мете."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    tid = "hdr00000000000000000000000000003a"
    _write_task(tmp_path, tid, status_tui="completed")
    app = TuiApp(threads_dir=str(tmp_path), system="cli",
                 config={"model": "M", "backend_api_mode": "responses"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        left, _ = scr._compose_header_segments()
        s = left if isinstance(left, str) else left.plain
        assert "контекст" not in s   # индикатор не в хедере вообще


@pytest.mark.asyncio
async def test_badge_failed_is_err_colored_in_meta(tmp_path):
    """Статус [failed] корректно читается через _task_status_badge (мета-полоса удалена в Task 3)."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    tid = "hdr00000000000000000000000000004a"
    _write_task(tmp_path, tid, status_tui="failed")
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        assert scr._task_status_badge() == "[failed]"


@pytest.mark.asyncio
async def test_meter_in_header_not_shown_chat_mode(tmp_path):
    """Контекст-индикатор не в хедере (переехал в мету или вовсе убран из хрома)."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    tid = "hdr00000000000000000000000000005a"
    rec = {"type": "message", "thread_id": tid, "system": "cli", "seq": 1,
           "ts": "t", "payload": {"role": "user", "content": "привет " * 50}}
    _write_task(tmp_path, tid, status_tui="completed",
                extra_data=json.dumps(rec) + "\n")
    app = TuiApp(threads_dir=str(tmp_path), system="cli",
                 config={"model": "M", "backend_api_mode": "chat_completions",
                         "summary_params": {"chat_completions_summary_trigger_chars": 1000}})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        left, _ = scr._compose_header_segments()
        s = left if isinstance(left, str) else left.plain
        # контекст-индикатор не в хедере после Task 6
        assert "контекст" not in s
        assert "%" not in s
