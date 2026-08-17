"""Таймстемп события берётся из record['ts'] (не из несуществующего 'ts_utc')."""
import json, pytest


@pytest.mark.asyncio
async def test_event_ts_populated_from_record_ts(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    tid = "tts"
    rec = {"type": "message", "seq": 1, "ts": "2026-06-04T10:00:00Z",
           "payload": {"id": "u1", "role": "user", "content": "hi"}}
    (tmp_path / f"data_cli_{tid}.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")
    (tmp_path / f"meta_cli_{tid}.json").write_text(
        json.dumps({"thread_id": tid, "updated_at": "x", "messages_count": 1}), encoding="utf-8")
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        await pilot.pause(0.2)
        ev = scr._task_events[0]
        assert ev.event_ts_utc_iso == "2026-06-04T10:00:00Z"   # не ""
