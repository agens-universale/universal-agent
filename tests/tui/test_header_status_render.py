"""Шапка: цветной бейдж статуса + индикатор контекста (rich.Text)."""
import json
import pytest


def _write_task(tmp_path, tid, status_tui=None):
    meta = {"thread_id": tid, "system_id": "cli"}
    if status_tui is not None:
        meta["tui"] = {"status": status_tui}
    (tmp_path / f"meta_cli_{tid}.json").write_text(json.dumps(meta), encoding="utf-8")
    (tmp_path / f"data_cli_{tid}.jsonl").write_text("", encoding="utf-8")


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
async def test_header_left_is_colored_text(tmp_path):
    pytest.importorskip("textual")
    from rich.text import Text
    from tui.app import TuiApp
    from tui.palette import OK
    tid = "hdr00000000000000000000000000002a"
    _write_task(tmp_path, tid, status_tui="completed")
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        left, _right = scr._compose_header_segments()
        assert isinstance(left, Text)
        assert "[completed]" in left.plain
        # есть стилизованный спан с цветом OK на бейдже
        styles = " ".join(str(s.style) for s in left.spans)
        assert OK.lower() in styles.lower()


@pytest.mark.asyncio
async def test_meter_hidden_in_responses_mode(tmp_path):
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
        assert "контекст" not in left.plain  # индикатор скрыт вне chat_completions
