"""Poller записывает meta.tui.last_compact_msg_count на завершении сжатия
(спадающий фронт runtime.summarizing True→False) — baseline для антиспама /compact."""
import json
import pytest


def _write_task(tmp_path, tid, n_messages):
    # Baseline после compact = снимок meta.runtime.live_msg_count (не scan data_*).
    (tmp_path / f"meta_cli_{tid}.json").write_text(
        json.dumps({
            "thread_id": tid,
            "system_id": "cli",
            "runtime": {
                "live_msg_count": n_messages,
                "live_history_chars": n_messages,
            },
        }),
        encoding="utf-8",
    )
    lines = [json.dumps({"type": "message", "thread_id": tid, "system": "cli",
                         "seq": i, "ts": "t", "payload": {"role": "user", "content": f"m{i}"}})
             for i in range(1, n_messages + 1)]
    (tmp_path / f"data_cli_{tid}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.mark.asyncio
async def test_summarizing_falling_edge_records_baseline(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    tid = "base0000000000000000000000000001a"
    _write_task(tmp_path, tid, n_messages=2)   # после сжатия в live 2 сообщения
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        scr._was_summarizing = True             # сжатие шло на прошлом тике
        # runtime.summarizing отсутствует → summarizing=False → спадающий фронт
        scr._poll_active()
        await pilot.pause()
        meta = scr._store.get_task_meta(tid)
        assert (meta.get("tui") or {}).get("last_compact_msg_count") == 2


@pytest.mark.asyncio
async def test_no_record_without_falling_edge(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    tid = "base0000000000000000000000000002a"
    _write_task(tmp_path, tid, n_messages=2)
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        scr._was_summarizing = False            # сжатия не было → нет фронта
        scr._poll_active()
        await pilot.pause()
        meta = scr._store.get_task_meta(tid)
        assert (meta.get("tui") or {}).get("last_compact_msg_count") is None
