"""TUI: при ≥2 сжатиях сырьём только 2 последних сегмента + маркеры #K для всех сжатий."""
import json

import pytest

_TID = "segtui00000000000000000000000a"


def _sum(text):
    return f'<technical type="dialog_summary"><summary>{text}</summary></technical>'


def _write(d, name, payloads):
    with open(d / name, "w", encoding="utf-8") as f:
        for i, p in enumerate(payloads, 1):
            f.write(json.dumps({"type": "message", "thread_id": _TID, "system": "cli",
                                "seq": i, "ts": "2026-06-03T10:00:00+00:00",
                                "payload": p}, ensure_ascii=False) + "\n")


@pytest.mark.asyncio
async def test_two_segments_raw_plus_markers(tmp_path):
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.widgets.trace_row import SummaryMarker
    from tui.widgets.user_reply import UserReplyContainer
    threads = tmp_path / "threads"; threads.mkdir()
    _write(threads, f"data_cli_{_TID}__pre_summary__20260603_100000.jsonl",
           [{"role": "user", "content": "сег0"}])
    _write(threads, f"data_cli_{_TID}__pre_summary__20260603_110000.jsonl",
           [{"role": "user", "content": _sum("С1")}, {"role": "user", "content": "сег1"}])
    _write(threads, f"data_cli_{_TID}.jsonl",
           [{"role": "user", "content": _sum("С2")}, {"role": "user", "content": "сег2"}])

    app = TuiApp(threads_dir=str(threads), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        screen = app.screen
        screen.attach_active_task(_TID)
        await pilot.pause()
        # 2 маркера сжатий (#1, #2).
        markers = [w for w in screen.query(SummaryMarker) if w.has_class("summary-marker")]
        assert len(markers) == 2
        # сырьём — только сег1 и сег2 (сег0 свёрнут, не показан).
        texts = [r._text for r in screen.query(UserReplyContainer)]
        joined = " ".join(texts)
        assert "сег1" in joined and "сег2" in joined
        assert "сег0" not in joined
