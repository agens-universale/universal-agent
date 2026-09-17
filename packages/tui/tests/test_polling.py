"""Polling: tail_events дозагружает новые записи; stream_reset → перерендер."""
import json
import pytest


def _write_message(threads_dir, system, tid, seq, role, content,
                   updated="2026-05-20T10:00:00+00:00",
                   extra_meta=None):
    data = threads_dir / f"data_{system}_{tid}.jsonl"
    with open(data, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "type": "message", "seq": seq,
            "payload": {"id": f"m{seq}", "role": role, "content": content},
        }) + "\n")
    meta_d = {
        "thread_id": tid, "system_id": system,
        "updated_at": updated, "messages_count": seq,
    }
    if extra_meta:
        meta_d.update(extra_meta)
    (threads_dir / f"meta_{system}_{tid}.json").write_text(json.dumps(meta_d))


@pytest.mark.asyncio
async def test_polling_appends_new_lines(tmp_path):
    pytest.importorskip("textual")
    _write_message(tmp_path, "cli", "t1", 1, "user", "первое")
    from gc_tui.app import TuiApp
    app = TuiApp(threads_dir=str(tmp_path), system="cli")
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task("t1")
        await pilot.pause(0.05)

        # Append new record on disk
        _write_message(tmp_path, "cli", "t1", 2, "assistant", "ответ")
        await pilot.pause(0.4)  # > 250 ms polling interval

        trace = pilot.app.query_one("#trace")
        text = "\n".join(str(c.render()) for c in trace.walk_children())
        assert "первое" in text
        assert "ответ" in text


@pytest.mark.asyncio
async def test_polling_stream_reset_reloads_full(tmp_path):
    pytest.importorskip("textual")
    _write_message(tmp_path, "cli", "t2", 1, "user", "пред-суммаризация")
    from gc_tui.app import TuiApp
    app = TuiApp(threads_dir=str(tmp_path), system="cli")
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task("t2")
        await pilot.pause(0.05)

        # Simulate compaction: rewrite file from scratch + new epoch
        data = tmp_path / "data_cli_t2.jsonl"
        data.write_text(json.dumps({
            "type": "message", "seq": 1,
            "payload": {"id": "s1", "role": "assistant",
                         "content": "после-сжатия"},
        }) + "\n")
        meta = tmp_path / "meta_cli_t2.json"
        meta.write_text(json.dumps({
            "thread_id": "t2",
            "updated_at": "2026-05-20T11:00:00+00:00",
            "messages_count": 1,
            "last_summary_archive_suffix": "2026-05-20T11-00",
        }))
        await pilot.pause(0.4)

        trace = pilot.app.query_one("#trace")
        text = "\n".join(str(c.render()) for c in trace.walk_children())
        assert "после-сжатия" in text
        assert "пред-суммаризация" not in text


@pytest.mark.asyncio
async def test_polling_stream_reset_keeps_brand_header(tmp_path):
    """После stream_reset (компакция) бренд-шапка остаётся первым ребёнком #trace."""
    pytest.importorskip("textual")
    _write_message(tmp_path, "cli", "t3", 1, "user", "пред-суммаризация")
    from gc_tui.app import TuiApp
    app = TuiApp(threads_dir=str(tmp_path), system="cli")
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task("t3")
        await pilot.pause(0.05)

        # Simulate compaction: rewrite file from scratch + new epoch
        data = tmp_path / "data_cli_t3.jsonl"
        data.write_text(json.dumps({
            "type": "message", "seq": 1,
            "payload": {"id": "s1", "role": "assistant",
                        "content": "после-сжатия"},
        }) + "\n")
        meta = tmp_path / "meta_cli_t3.json"
        meta.write_text(json.dumps({
            "thread_id": "t3",
            "updated_at": "2026-06-10T11:00:00+00:00",
            "messages_count": 1,
            "last_summary_archive_suffix": "2026-06-10T11-00",
        }))
        await pilot.pause(0.4)  # > 250 ms polling interval

        trace = pilot.app.query_one("#trace")
        children = list(trace.children)
        assert children and children[0].id == "brand-block"
        text = "\n".join(str(c.render()) for c in trace.walk_children())
        assert "после-сжатия" in text
